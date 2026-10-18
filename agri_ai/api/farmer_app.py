"""Farmer API used by the smartphone web app (PWA). Login is phone number + SMS code."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import i18n
from ..config import get_settings
from ..db import get_db
from ..knowledge import get_kb
from ..models import (
    AdviceLog,
    Alert,
    Diagnosis,
    Farmer,
    Listing,
    ListingStatus,
    Match,
    OtpCode,
    PriceAlertSubscription,
    utcnow,
)
from ..security import generate_otp, hash_secret, sign_token
from ..services import advisory, diagnosis, marketplace
from ..services.farmers import add_plot, create_listing, find_farmer
from ..services.geo import normalize_phone
from ..services.notifications import send_sms
from ..services.prices import latest_quotes
from ..services.weather import evaluate_forecast, farmer_crop_contexts, farmer_location, get_forecast
from . import schemas as s
from .deps import current_farmer
from .serializers import diagnosis_out, farmer_out, match_out

router = APIRouter(prefix="/api/v1/farmer", tags=["farmer"])

OTP_TTL = timedelta(minutes=10)
OTP_MAX_ATTEMPTS = 5
OTP_MAX_PER_WINDOW = 3


# ------------------------------------------------------------------ auth


@router.post("/auth/request-code")
def request_code(body: s.OtpRequest, db: Session = Depends(get_db)):
    """Send a 6-digit login code. Only farmers registered by a partner (or via SMS/USSD) can log in."""
    try:
        phone = normalize_phone(body.phone, body.country)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    farmer = find_farmer(db, phone)
    # Same response whether or not the number exists, to avoid enumerating farmers.
    response: dict = {"sent": True}
    if farmer is None:
        return response
    recent = db.scalar(
        select(func.count()).select_from(OtpCode).where(OtpCode.phone == phone, OtpCode.created_at >= utcnow() - OTP_TTL)
    )
    if (recent or 0) >= OTP_MAX_PER_WINDOW:
        raise HTTPException(429, "too many codes requested; try again in 10 minutes")
    code = generate_otp()
    db.add(OtpCode(phone=phone, code_hash=hash_secret(f"{phone}:{code}"), expires_at=utcnow() + OTP_TTL))
    send_sms(db, phone, i18n.t("otp", farmer.language, brand=farmer.org.display_name, code=code), farmer=farmer, transactional=True)
    db.commit()
    if get_settings().dev_expose_otp:
        response["dev_code"] = code
    return response


@router.post("/auth/verify", response_model=s.TokenOut)
def verify_code(body: s.OtpVerify, db: Session = Depends(get_db)):
    try:
        phone = normalize_phone(body.phone, body.country)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    otp = db.scalar(
        select(OtpCode)
        .where(OtpCode.phone == phone, OtpCode.used.is_(False), OtpCode.expires_at >= utcnow())
        .order_by(OtpCode.id.desc())
    )
    if otp is None or otp.attempts >= OTP_MAX_ATTEMPTS:
        raise HTTPException(401, "code expired; request a new one")
    otp.attempts += 1
    if otp.code_hash != hash_secret(f"{phone}:{body.code}"):
        db.commit()
        raise HTTPException(401, "wrong code")
    otp.used = True
    farmer = find_farmer(db, phone)
    if farmer is None:
        raise HTTPException(401, "unknown farmer")
    farmer.last_active_at = utcnow()
    db.commit()
    token = sign_token({"typ": "farmer", "fid": farmer.id}, ttl_seconds=get_settings().farmer_token_days * 86400)
    return s.TokenOut(token=token, farmer=farmer_out(farmer))


# ------------------------------------------------------------------ profile


@router.get("/me", response_model=s.FarmerOut)
def me(farmer: Farmer = Depends(current_farmer)):
    return farmer_out(farmer)


@router.patch("/me", response_model=s.FarmerOut)
def update_me(body: s.FarmerSelfUpdate, farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(farmer, k, v)
    db.commit()
    return farmer_out(farmer)


@router.post("/me/plots", response_model=s.FarmerOut, status_code=201)
def add_my_plot(body: s.PlotIn, farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    add_plot(db, farmer, **body.model_dump())
    db.commit()
    return farmer_out(farmer)


@router.get("/home")
def home(farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    """Everything the app's home screen needs in one round trip (slow networks)."""
    lang = farmer.language
    loc = farmer_location(farmer)
    forecast = get_forecast(db, *loc) if loc else None
    events = evaluate_forecast(forecast, farmer_crop_contexts(farmer), lang) if forecast else []
    kb = get_kb()
    prices = {}
    for crop in farmer.crops[:4]:
        prices[crop] = [q.to_dict() for q in latest_quotes(db, crop, lat=loc[0] if loc else None, lon=loc[1] if loc else None, country=farmer.org.country, limit=3)]
    unread = db.scalar(select(func.count()).select_from(Alert).where(Alert.farmer_id == farmer.id, Alert.read.is_(False)))
    db.commit()  # persist the forecast cache
    return {
        "farmer": farmer_out(farmer).model_dump(mode="json"),
        "brand": farmer.org.display_name,
        "currency": farmer.org.currency,
        "forecast": [d.to_dict() for d in forecast] if forecast else None,
        "weather_warnings": [{"kind": e.kind, "severity": e.severity, "message": e.message, "day": e.day.isoformat()} for e in events],
        "tips": advisory.stage_tips(farmer),
        "prices": prices,
        "unread_alerts": unread or 0,
        "crops": [{"id": c.id, "name": c.name(lang)} for c in kb.crops.values()],
    }


# ------------------------------------------------------------------ advice & diagnosis


@router.post("/advice", response_model=s.AdviceOut)
def ask(body: s.AskIn, farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    result = advisory.answer_question(db, farmer, body.question, channel="app")
    db.commit()
    return s.AdviceOut(answer=result.answer, engine=result.engine, id=result.log_id)


@router.post("/advice/{advice_id}/feedback", status_code=204)
def advice_feedback(advice_id: int, body: s.FeedbackIn, farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    entry = db.get(AdviceLog, advice_id)
    if entry is None or entry.farmer_id != farmer.id:
        raise HTTPException(404, "not found")
    entry.helpful = body.helpful
    db.commit()


@router.post("/diagnose", response_model=s.DiagnoseResponse)
async def diagnose(
    image: UploadFile = File(...),
    crop: str | None = Form(None),
    note: str | None = Form(None),
    farmer: Farmer = Depends(current_farmer),
    db: Session = Depends(get_db),
):
    data = await image.read(get_settings().max_upload_bytes + 1)
    if len(data) > get_settings().max_upload_bytes:
        raise HTTPException(413, "image too large")
    outcome = diagnosis.diagnose_photo(db, farmer, data, crop=crop, note=note, channel="app")
    db.commit()
    return s.DiagnoseResponse(
        message=outcome.message, retake=outcome.retake, diagnosis=diagnosis_out(outcome.diagnosis) if outcome.diagnosis else None
    )


@router.post("/diagnose/text", response_model=s.DiagnoseResponse)
def diagnose_text(body: s.SymptomsIn, farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    outcome = diagnosis.diagnose_text(db, farmer, body.symptoms, crop=body.crop, channel="app")
    db.commit()
    return s.DiagnoseResponse(message=outcome.message, retake=False, diagnosis=diagnosis_out(outcome.diagnosis) if outcome.diagnosis else None)


@router.get("/diagnoses", response_model=list[s.DiagnosisOut])
def my_diagnoses(farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    rows = db.scalars(select(Diagnosis).where(Diagnosis.farmer_id == farmer.id).order_by(Diagnosis.created_at.desc()).limit(50))
    return [diagnosis_out(d) for d in rows]


# ------------------------------------------------------------------ weather & prices


@router.get("/weather")
def weather(farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    loc = farmer_location(farmer)
    if loc is None:
        raise HTTPException(409, "set your farm location first")
    fc = get_forecast(db, *loc)
    if fc is None:
        raise HTTPException(503, "forecast unavailable")
    db.commit()
    events = evaluate_forecast(fc, farmer_crop_contexts(farmer), farmer.language)
    return {"forecast": [d.to_dict() for d in fc], "warnings": [{"kind": e.kind, "severity": e.severity, "message": e.message} for e in events]}


@router.get("/prices")
def prices(crop: str, farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    c = get_kb().resolve_crop(crop)
    if c is None:
        raise HTTPException(422, "unknown crop")
    loc = farmer_location(farmer)
    return [q.to_dict() for q in latest_quotes(db, c.id, lat=loc[0] if loc else None, lon=loc[1] if loc else None, country=farmer.org.country, limit=10)]


@router.get("/price-alerts", response_model=list[s.PriceAlertOut])
def list_price_alerts(farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    return db.scalars(select(PriceAlertSubscription).where(PriceAlertSubscription.farmer_id == farmer.id)).all()


@router.post("/price-alerts", response_model=s.PriceAlertOut, status_code=201)
def add_price_alert(body: s.PriceAlertIn, farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    sub = PriceAlertSubscription(farmer_id=farmer.id, **body.model_dump())
    db.add(sub)
    db.commit()
    return sub


@router.delete("/price-alerts/{sub_id}", status_code=204)
def delete_price_alert(sub_id: int, farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    sub = db.get(PriceAlertSubscription, sub_id)
    if sub is None or sub.farmer_id != farmer.id:
        raise HTTPException(404, "not found")
    db.delete(sub)
    db.commit()


# ------------------------------------------------------------------ alerts


@router.get("/alerts", response_model=list[s.AlertOut])
def alerts(farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    return db.scalars(select(Alert).where(Alert.farmer_id == farmer.id).order_by(Alert.created_at.desc()).limit(100)).all()


@router.post("/alerts/read", status_code=204)
def mark_alerts_read(farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    for a in db.scalars(select(Alert).where(Alert.farmer_id == farmer.id, Alert.read.is_(False))):
        a.read = True
    db.commit()


# ------------------------------------------------------------------ marketplace


@router.get("/listings", response_model=list[s.ListingOut])
def my_listings(farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    return db.scalars(select(Listing).where(Listing.farmer_id == farmer.id).order_by(Listing.id.desc())).all()


@router.post("/listings", response_model=s.ListingOut, status_code=201)
def new_listing(body: s.ListingIn, farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    if not farmer.consent_marketplace:
        raise HTTPException(409, "accept marketplace sharing in your profile first")
    listing = create_listing(db, farmer, **body.model_dump())
    marketplace.match_for_new_listing(db, listing)
    db.commit()
    return listing


@router.post("/listings/{listing_id}/withdraw", response_model=s.ListingOut)
def withdraw_listing(listing_id: int, farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    listing = db.get(Listing, listing_id)
    if listing is None or listing.farmer_id != farmer.id:
        raise HTTPException(404, "not found")
    listing.status = ListingStatus.withdrawn.value
    for m in db.scalars(select(Match).where(Match.listing_id == listing.id, Match.status == "proposed")):
        marketplace.cancel_match(db, m)
        db.flush()
        marketplace.run_matching(db, m.buy_request)
    db.commit()
    return listing


@router.get("/offers", response_model=list[s.MatchOut])
def offers(farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Match).join(Listing, Listing.id == Match.listing_id).where(Listing.farmer_id == farmer.id).order_by(Match.created_at.desc()).limit(50)
    )
    return [match_out(m, for_buyer=False) for m in rows]


@router.post("/offers/{code}/{decision}")
def respond_offer(code: str, decision: str, farmer: Farmer = Depends(current_farmer), db: Session = Depends(get_db)):
    if decision not in {"accept", "decline"}:
        raise HTTPException(422, "decision must be accept or decline")
    match, reply = marketplace.farmer_respond(db, farmer, code, accept=decision == "accept")
    if match is None:
        raise HTTPException(404, reply)
    db.commit()
    return {"status": match.status, "message": reply}
