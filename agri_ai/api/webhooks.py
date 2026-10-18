"""Inbound channel webhooks: SMS, USSD and WhatsApp.

Compatible with Africa's Talking (form fields ``from``/``to``/``text`` and the USSD
``sessionId``/``serviceCode``/``phoneNumber``/``text`` protocol) and Twilio
(``From``/``To``/``Body``/``MediaUrl0``). Gateways authenticate with the shared
secret in the ``X-Webhook-Secret`` header or a ``?secret=`` query parameter.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from .. import i18n
from ..channels.sms import handle_text
from ..channels.ussd import handle_ussd
from ..config import get_settings
from ..db import session_factory
from ..models import Farmer
from ..security import constant_time_equals
from ..services import advisory, diagnosis
from ..services.farmers import get_or_register, org_for_inbound
from ..services.notifications import log_inbound, send_sms

log = logging.getLogger("agri_ai.webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def verify_secret(x_webhook_secret: str | None = Header(default=None), secret: str | None = Query(default=None)) -> None:
    if not constant_time_equals(x_webhook_secret or secret, get_settings().webhook_secret):
        raise HTTPException(401, "invalid webhook secret")


def _field(form, *names: str) -> str:
    for n in names:
        v = form.get(n)
        if v:
            return str(v)
    return ""


def _strip_channel_prefix(phone: str) -> str:
    return phone.split(":", 1)[1] if phone.startswith(("whatsapp:", "tel:")) else phone


def process_message(phone: str, to: str, text: str, channel: str, media_url: str | None = None, media_type: str | None = None) -> None:
    """Handle one inbound message end-to-end in its own DB session (runs after the HTTP response)."""
    with session_factory()() as db:
        org = org_for_inbound(db, shortcode=to)
        try:
            farmer, created = get_or_register(db, phone, org)
        except ValueError:
            log.warning("ignoring message from invalid number %r", phone)
            return
        if farmer is None:
            log.warning("no organisation for inbound %s to %s", phone, to)
            return
        log_inbound(db, phone=farmer.phone, body=text or (media_url or ""), channel=channel, farmer=farmer, org_id=farmer.org_id)
        replies: list[str] = []
        if created:
            replies.append(i18n.t("welcome", farmer.language, brand=farmer.org.display_name))
        if media_url:
            image = _download_media(media_url)
            if image is None:
                replies.append(i18n.t("retake_photo", farmer.language, reason="download failed"))
            else:
                outcome = diagnosis.diagnose_photo(db, farmer, image, note=text or None, channel=channel)
                replies.append(outcome.message)
        elif text.strip() and not (created and text.strip().lower() in {"hi", "hello", "join", "jambo", "bonjour"}):
            replies.append(handle_text(db, farmer, text, channel=channel))
        for reply in replies:
            send_sms(db, farmer.phone, reply, farmer=farmer, transactional=True)
        db.commit()


def _download_media(url: str) -> bytes | None:
    """Fetch a WhatsApp/MMS image, only from allow-listed provider hosts (SSRF guard)."""
    s = get_settings()
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "") not in s.media_host_allowlist:
        log.warning("refusing media from non-allow-listed URL %s", parsed.hostname)
        return None
    auth = (s.twilio_account_sid, s.twilio_auth_token) if s.twilio_account_sid and s.twilio_auth_token else None
    try:
        with httpx.Client(timeout=20, follow_redirects=True, auth=auth) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > s.max_upload_bytes:
                        return None
                    chunks.append(chunk)
        return b"".join(chunks)
    except httpx.HTTPError as exc:
        log.warning("media download failed: %s", exc)
        return None


@router.post("/sms", dependencies=[Depends(verify_secret)])
async def inbound_sms(request: Request, background: BackgroundTasks):
    form = await request.form()
    phone = _strip_channel_prefix(_field(form, "from", "From", "phoneNumber"))
    to = _strip_channel_prefix(_field(form, "to", "To", "shortCode"))
    text = _field(form, "text", "Body", "message")
    if not phone:
        raise HTTPException(422, "missing sender")
    media = _field(form, "MediaUrl0") or None
    background.add_task(process_message, phone, to, text, "sms", media, _field(form, "MediaContentType0") or None)
    # Twilio expects TwiML; Africa's Talking ignores the body. Replies go out via the gateway API.
    return Response(EMPTY_TWIML, media_type="application/xml")


@router.post("/whatsapp", dependencies=[Depends(verify_secret)])
async def inbound_whatsapp(request: Request, background: BackgroundTasks):
    form = await request.form()
    phone = _strip_channel_prefix(_field(form, "From", "from"))
    to = _strip_channel_prefix(_field(form, "To", "to"))
    if not phone:
        raise HTTPException(422, "missing sender")
    background.add_task(
        process_message, phone, to, _field(form, "Body", "text"), "whatsapp", _field(form, "MediaUrl0") or None, _field(form, "MediaContentType0") or None
    )
    return Response(EMPTY_TWIML, media_type="application/xml")


def _answer_by_sms(farmer_id: int, question: str) -> None:
    with session_factory()() as db:
        farmer = db.get(Farmer, farmer_id)
        if farmer is None:
            return
        result = advisory.answer_question(db, farmer, question, channel="ussd")
        send_sms(db, farmer.phone, result.answer, farmer=farmer, transactional=True)
        db.commit()


@router.post("/ussd", dependencies=[Depends(verify_secret)], response_class=PlainTextResponse)
async def inbound_ussd(request: Request, background: BackgroundTasks):
    form = await request.form()
    phone = _field(form, "phoneNumber", "msisdn", "From")
    service_code = _field(form, "serviceCode", "ussdCode")
    text = _field(form, "text")
    with session_factory()() as db:
        org = org_for_inbound(db, ussd_code=service_code)
        try:
            farmer, _created = get_or_register(db, phone, org)
        except ValueError:
            return PlainTextResponse("END Invalid number")
        if farmer is None:
            return PlainTextResponse("END Service not available")
        if not text:
            log_inbound(db, phone=farmer.phone, body=f"USSD {service_code}", channel="ussd", farmer=farmer, org_id=farmer.org_id)
        screen = handle_ussd(db, farmer, text, defer=lambda f, q: background.add_task(_answer_by_sms, f.id, q))
        db.commit()
    return PlainTextResponse(screen)
