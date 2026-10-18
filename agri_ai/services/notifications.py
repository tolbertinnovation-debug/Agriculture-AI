"""Outbound messaging (SMS gateways) and the farmer alert feed.

Every outbound message is written to ``messages`` with its SMS segment count so
partners can be billed and audited per message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Alert, Farmer, Message, Organization

log = logging.getLogger("agri_ai.sms")

# GSM 03.38 basic character set (plus the extension table, which costs 2 septets).
_GSM_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM_EXT = set("^{}\\[~]|€")


def sms_segments(text: str) -> int:
    """Number of SMS segments the text will be billed as."""
    if all(c in _GSM_BASIC or c in _GSM_EXT for c in text):
        length = sum(2 if c in _GSM_EXT else 1 for c in text)
        return 1 if length <= 160 else -(-length // 153)
    length = len(text.encode("utf-16-le")) // 2
    return 1 if length <= 70 else -(-length // 67)


@dataclass
class SendResult:
    ok: bool
    provider_ref: str | None = None
    error: str | None = None


class SmsGateway(Protocol):
    name: str

    def send(self, phone: str, body: str, sender: str | None = None) -> SendResult: ...


class ConsoleGateway:
    """Development gateway: logs messages and keeps them in memory (used by tests)."""

    name = "console"

    def __init__(self) -> None:
        self.outbox: list[tuple[str, str]] = []

    def send(self, phone: str, body: str, sender: str | None = None) -> SendResult:
        self.outbox.append((phone, body))
        log.info("SMS to %s: %s", phone, body)
        return SendResult(ok=True, provider_ref=f"console-{len(self.outbox)}")


class AfricasTalkingGateway:
    name = "africastalking"

    def __init__(self, username: str, api_key: str, sender_id: str | None = None) -> None:
        self.username, self.api_key, self.sender_id = username, api_key, sender_id
        host = "api.sandbox.africastalking.com" if username == "sandbox" else "api.africastalking.com"
        self.url = f"https://{host}/version1/messaging"

    def send(self, phone: str, body: str, sender: str | None = None) -> SendResult:
        data = {"username": self.username, "to": phone, "message": body}
        if sender or self.sender_id:
            data["from"] = sender or self.sender_id
        try:
            resp = httpx.post(
                self.url,
                data=data,
                headers={"apiKey": self.api_key, "Accept": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
            recipients = resp.json().get("SMSMessageData", {}).get("Recipients", [])
            if recipients and recipients[0].get("status") == "Success":
                return SendResult(ok=True, provider_ref=recipients[0].get("messageId"))
            return SendResult(ok=False, error=str(recipients or resp.text)[:500])
        except (httpx.HTTPError, ValueError) as exc:
            return SendResult(ok=False, error=str(exc)[:500])


class TwilioGateway:
    name = "twilio"

    def __init__(self, sid: str, token: str, from_number: str) -> None:
        self.sid, self.token, self.from_number = sid, token, from_number

    def send(self, phone: str, body: str, sender: str | None = None) -> SendResult:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json"
        try:
            resp = httpx.post(
                url, data={"To": phone, "From": sender or self.from_number, "Body": body}, auth=(self.sid, self.token), timeout=15
            )
            resp.raise_for_status()
            return SendResult(ok=True, provider_ref=resp.json().get("sid"))
        except (httpx.HTTPError, ValueError) as exc:
            return SendResult(ok=False, error=str(exc)[:500])


_gateway: SmsGateway | None = None


def get_gateway() -> SmsGateway:
    global _gateway
    if _gateway is None:
        s = get_settings()
        if s.sms_gateway == "africastalking" and s.africastalking_api_key:
            _gateway = AfricasTalkingGateway(s.africastalking_username, s.africastalking_api_key, s.africastalking_sender_id)
        elif s.sms_gateway == "twilio" and s.twilio_account_sid and s.twilio_auth_token and s.twilio_from_number:
            _gateway = TwilioGateway(s.twilio_account_sid, s.twilio_auth_token, s.twilio_from_number)
        else:
            _gateway = ConsoleGateway()
    return _gateway


def set_gateway(gateway: SmsGateway | None) -> None:
    global _gateway
    _gateway = gateway


def log_inbound(db: Session, *, phone: str, body: str, channel: str, farmer: Farmer | None, org_id: int | None) -> None:
    db.add(
        Message(
            org_id=org_id or (farmer.org_id if farmer else None),
            farmer_id=farmer.id if farmer else None,
            phone=phone,
            direction="in",
            channel=channel,
            body=body[:2000],
            segments=sms_segments(body) if channel == "sms" else 0,
            status="received",
        )
    )


def send_sms(
    db: Session,
    phone: str,
    body: str,
    *,
    farmer: Farmer | None = None,
    org: Organization | None = None,
    transactional: bool = False,
) -> Message | None:
    """Send an SMS and record it.

    Marketing/alert traffic (``transactional=False``) is suppressed for farmers who
    replied STOP. Direct replies, OTPs and deal confirmations are transactional.
    """
    if farmer is not None and farmer.opted_out and not transactional:
        return None
    org_id = org.id if org else (farmer.org_id if farmer else None)
    result = get_gateway().send(phone, body)
    msg = Message(
        org_id=org_id,
        farmer_id=farmer.id if farmer else None,
        phone=phone,
        direction="out",
        channel="sms",
        body=body,
        segments=sms_segments(body),
        status="sent" if result.ok else "failed",
        provider_ref=result.provider_ref,
        error=result.error,
    )
    db.add(msg)
    return msg


def create_alert(
    db: Session,
    farmer: Farmer,
    *,
    kind: str,
    title: str,
    body: str,
    dedupe_key: str,
    severity: str = "info",
    data: dict | None = None,
    sms: bool = True,
) -> Alert | None:
    """Create a farmer alert once per ``dedupe_key`` and deliver it by SMS.

    Returns ``None`` when an alert with the same key already exists, which makes
    every alert job safe to re-run.
    """
    exists = db.scalar(select(Alert.id).where(Alert.farmer_id == farmer.id, Alert.dedupe_key == dedupe_key))
    if exists:
        return None
    alert = Alert(
        org_id=farmer.org_id,
        farmer_id=farmer.id,
        kind=kind,
        severity=severity,
        title=title,
        body=body,
        dedupe_key=dedupe_key,
        data=data or {},
    )
    try:
        with db.begin_nested():
            db.add(alert)
    except IntegrityError:
        return None
    if sms:
        alert.delivered = send_sms(db, farmer.phone, body, farmer=farmer) is not None
    return alert
