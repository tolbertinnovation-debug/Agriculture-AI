"""Partner API: telcos, co-operatives and NGOs manage their farmers and see impact."""

from __future__ import annotations

import csv
import io
import os
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import i18n
from ..db import get_db
from ..jobs import JOBS, run_jobs
from ..knowledge import get_kb
from ..models import (
    AdviceLog,
    Alert,
    ApiKey,
    Diagnosis,
    Farmer,
    Listing,
    Match,
    Message,
    Outbreak,
    Plot,
    utcnow,
)
from ..security import generate_api_key
from ..services import diagnosis as diagnosis_svc
from ..services.farmers import add_plot
from ..services.geo import normalize_phone
from ..services.notifications import create_alert, send_sms, sms_segments
from ..services.prices import latest_quotes, market_summary, record_price
from . import schemas as s
from .deps import Principal, admin, officer, org_farmer, viewer
from .serializers import diagnosis_out, farmer_out

router = APIRouter(prefix="/api/v1/partner", tags=["partner"])


# ------------------------------------------------------------------ organisation & keys


@router.get("/org", response_model=s.OrgOut)
def get_org(p: Principal = Depends(viewer)):
    return p.org


@router.patch("/org", response_model=s.OrgOut)
def update_org(body: s.OrgUpdate, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    org = p.org
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(org, k, v)
    db.commit()
    return org


@router.get("/keys", response_model=list[s.ApiKeyOut])
def list_keys(p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return db.scalars(select(ApiKey).where(ApiKey.org_id == p.org.id).order_by(ApiKey.id)).all()


@router.post("/keys", response_model=s.ApiKeyCreated, status_code=201)
def create_key(body: s.ApiKeyCreate, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    plaintext, prefix, digest = generate_api_key()
    key = ApiKey(org_id=p.org.id, name=body.name, role=body.role, prefix=prefix, key_hash=digest)
    db.add(key)
    db.commit()
    return s.ApiKeyCreated(**s.ApiKeyOut.model_validate(key).model_dump(), key=plaintext)


@router.delete("/keys/{key_id}", status_code=204)
def revoke_key(key_id: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    key = db.get(ApiKey, key_id)
    if key is None or key.org_id != p.org.id:
        raise HTTPException(404, "key not found")
    if key.id == p.key.id:
        raise HTTPException(400, "cannot revoke the key you are using")
    key.revoked_at = utcnow()
    db.commit()


# ------------------------------------------------------------------ farmers


@router.get("/farmers", response_model=s.FarmerPage)
def list_farmers(
    q: str | None = None,
    crop: str | None = None,
    region: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    p: Principal = Depends(viewer),
    db: Session = Depends(get_db),
):
    query = select(Farmer).where(Farmer.org_id == p.org.id)
    if q:
        like = f"%{q}%"
        query = query.where(or_(Farmer.name.ilike(like), Farmer.phone.ilike(like), Farmer.village.ilike(like)))
    if region:
        query = query.where(Farmer.region.ilike(region))
    if crop:
        c = get_kb().resolve_crop(crop)
        query = query.where(Farmer.plots.any(Plot.crop == (c.id if c else crop)))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.options(selectinload(Farmer.plots)).order_by(Farmer.id.desc()).offset(offset).limit(limit)).all()
    return s.FarmerPage(total=total or 0, items=[farmer_out(f) for f in rows])


def _upsert_farmer(db: Session, p: Principal, body: s.FarmerCreate) -> tuple[Farmer, bool]:
    phone = normalize_phone(body.phone, p.org.country)
    farmer = db.scalar(select(Farmer).where(Farmer.phone == phone))
    if farmer is not None and farmer.org_id != p.org.id:
        raise HTTPException(409, "this phone number is registered with another organisation")
    created = farmer is None
    if created:
        farmer = Farmer(org_id=p.org.id, phone=phone, language=body.language or p.org.default_language)
        db.add(farmer)
    fields = body.model_dump(exclude={"phone", "plots", "send_welcome"}, exclude_unset=True)
    for k, v in fields.items():
        if v is not None:
            setattr(farmer, k, v)
    db.flush()
    existing = {pl.crop for pl in farmer.plots}
    for plot in body.plots:
        if plot.crop not in existing:
            add_plot(db, farmer, **plot.model_dump())
    return farmer, created


@router.post("/farmers", response_model=s.FarmerOut, status_code=201)
def create_farmer(body: s.FarmerCreate, p: Principal = Depends(officer), db: Session = Depends(get_db)):
    try:
        farmer, created = _upsert_farmer(db, p, body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if created and body.send_welcome:
        send_sms(db, farmer.phone, i18n.t("welcome", farmer.language, brand=p.org.display_name), farmer=farmer, transactional=True)
    db.commit()
    db.refresh(farmer)
    return farmer_out(farmer)


@router.post("/farmers/import", response_model=s.ImportResult)
async def import_farmers(
    file: UploadFile = File(..., description="CSV: phone,name,language,gender,region,village,lat,lon,crops,planting_date,consent_marketplace"),
    send_welcome: bool = False,
    p: Principal = Depends(officer),
    db: Session = Depends(get_db),
):
    """Bulk onboarding from a co-operative's member register. ``crops`` is ``;``-separated."""
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    created = updated = 0
    errors: list[str] = []
    for i, row in enumerate(csv.DictReader(io.StringIO(raw)), start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        try:
            planting = date.fromisoformat(row["planting_date"]) if row.get("planting_date") else None
            plots = [s.PlotIn(crop=c.strip(), planting_date=planting) for c in row.get("crops", "").split(";") if c.strip()]
            body = s.FarmerCreate(
                phone=row["phone"],
                name=row.get("name") or None,
                language=row.get("language") or None,
                gender=row.get("gender") or None,
                region=row.get("region") or None,
                village=row.get("village") or None,
                lat=float(row["lat"]) if row.get("lat") else None,
                lon=float(row["lon"]) if row.get("lon") else None,
                consent_marketplace=row.get("consent_marketplace", "").lower() in {"1", "yes", "true", "y"},
                plots=plots,
            )
            with db.begin_nested():
                farmer, was_created = _upsert_farmer(db, p, body)
            if was_created:
                created += 1
                if send_welcome:
                    send_sms(db, farmer.phone, i18n.t("welcome", farmer.language, brand=p.org.display_name), farmer=farmer, transactional=True)
            else:
                updated += 1
        except (KeyError, ValueError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            errors.append(f"row {i}: {detail}"[:300])
    db.commit()
    return s.ImportResult(created=created, updated=updated, errors=errors[:200])


@router.get("/farmers/{farmer_id}", response_model=s.FarmerOut)
def get_farmer(farmer_id: int, p: Principal = Depends(viewer), db: Session = Depends(get_db)):
    return farmer_out(org_farmer(db, p.org, farmer_id))


@router.patch("/farmers/{farmer_id}", response_model=s.FarmerOut)
def update_farmer(farmer_id: int, body: s.FarmerUpdate, p: Principal = Depends(officer), db: Session = Depends(get_db)):
    farmer = org_farmer(db, p.org, farmer_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(farmer, k, v)
    db.commit()
    return farmer_out(farmer)


@router.delete("/farmers/{farmer_id}", status_code=204)
def delete_farmer(farmer_id: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    """Right to erasure: removes the farmer, plots, diagnoses, listings and alerts.

    Message logs are kept for billing but detached from the person and the phone number is redacted.
    """
    farmer = org_farmer(db, p.org, farmer_id)
    for msg in db.scalars(select(Message).where(Message.farmer_id == farmer.id)):
        msg.farmer_id, msg.phone, msg.body = None, "redacted", "[erased]"
    for d in db.scalars(select(Diagnosis).where(Diagnosis.farmer_id == farmer.id)):
        if d.image_path:
            try:
                os.remove(d.image_path)
            except OSError:
                pass
        db.delete(d)
    for model in (Alert, AdviceLog):
        for row in db.scalars(select(model).where(model.farmer_id == farmer.id)):
            db.delete(row)
    for lst in db.scalars(select(Listing).where(Listing.farmer_id == farmer.id)):
        for m in db.scalars(select(Match).where(Match.listing_id == lst.id)):
            db.delete(m)
        db.delete(lst)
    db.delete(farmer)
    db.commit()


@router.post("/farmers/{farmer_id}/plots", response_model=s.FarmerOut, status_code=201)
def add_farmer_plot(farmer_id: int, body: s.PlotIn, p: Principal = Depends(officer), db: Session = Depends(get_db)):
    farmer = org_farmer(db, p.org, farmer_id)
    add_plot(db, farmer, **body.model_dump())
    db.commit()
    return farmer_out(farmer)


@router.delete("/farmers/{farmer_id}/plots/{plot_id}", status_code=204)
def delete_farmer_plot(farmer_id: int, plot_id: int, p: Principal = Depends(officer), db: Session = Depends(get_db)):
    farmer = org_farmer(db, p.org, farmer_id)
    plot = db.get(Plot, plot_id)
    if plot is None or plot.farmer_id != farmer.id:
        raise HTTPException(404, "plot not found")
    db.delete(plot)
    db.commit()


# ------------------------------------------------------------------ broadcast


@router.post("/broadcast")
def broadcast(body: s.BroadcastIn, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    """Send an announcement (training day, input subsidy, vaccination campaign) to a segment."""
    q = select(Farmer).where(Farmer.org_id == p.org.id, Farmer.opted_out.is_(False))
    if body.region:
        q = q.where(Farmer.region.ilike(body.region))
    if body.language:
        q = q.where(Farmer.language == body.language)
    if body.crop:
        c = get_kb().resolve_crop(body.crop)
        if c is None:
            raise HTTPException(422, "unknown crop")
        q = q.where(Farmer.plots.any(Plot.crop == c.id))
    farmers = db.scalars(q).all()
    segments = sms_segments(body.message)
    if body.dry_run:
        return {"recipients": len(farmers), "segments_per_message": segments, "total_segments": segments * len(farmers), "sent": 0}
    key = f"broadcast:{utcnow().isoformat(timespec='seconds')}:{hash(body.message) & 0xFFFFFFFF:x}"
    sent = sum(
        1
        for f in farmers
        if create_alert(db, f, kind="broadcast", title=p.org.display_name, body=body.message, dedupe_key=key)
    )
    db.commit()
    return {"recipients": len(farmers), "segments_per_message": segments, "total_segments": segments * sent, "sent": sent}


# ------------------------------------------------------------------ diagnoses (extension officer queue)


@router.get("/diagnoses", response_model=list[s.DiagnosisOut])
def list_diagnoses(
    status: str | None = Query(None, description="auto | needs_review | confirmed | rejected"),
    limit: int = Query(50, ge=1, le=500),
    p: Principal = Depends(viewer),
    db: Session = Depends(get_db),
):
    q = select(Diagnosis).where(Diagnosis.org_id == p.org.id)
    if status:
        q = q.where(Diagnosis.status == status)
    return [diagnosis_out(d) for d in db.scalars(q.order_by(Diagnosis.created_at.desc()).limit(limit))]


def _org_diagnosis(db: Session, p: Principal, diagnosis_id: int) -> Diagnosis:
    d = db.get(Diagnosis, diagnosis_id)
    if d is None or d.org_id != p.org.id:
        raise HTTPException(404, "diagnosis not found")
    return d


@router.get("/diagnoses/{diagnosis_id}/image")
def diagnosis_image(diagnosis_id: int, p: Principal = Depends(viewer), db: Session = Depends(get_db)):
    d = _org_diagnosis(db, p, diagnosis_id)
    if not d.image_path:
        raise HTTPException(404, "no image")
    return FileResponse(d.image_path, media_type="image/jpeg")


@router.post("/diagnoses/{diagnosis_id}/review", response_model=s.DiagnosisOut)
def review_diagnosis(diagnosis_id: int, body: s.ReviewIn, p: Principal = Depends(officer), db: Session = Depends(get_db)):
    d = _org_diagnosis(db, p, diagnosis_id)
    try:
        diagnosis_svc.review(db, d, pest_id=body.pest_id, status=body.status, notes=body.notes, notify=body.notify_farmer)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return diagnosis_out(d)


# ------------------------------------------------------------------ prices


@router.post("/prices", status_code=201)
def upload_prices(body: s.PriceBatch, p: Principal = Depends(officer), db: Session = Depends(get_db)):
    """Market price observations from the partner's enumerators or a market-information system."""
    for row in body.prices:
        record_price(
            db,
            crop=row.crop,
            market=row.market,
            region=row.region,
            country=p.org.country,
            lat=row.lat,
            lon=row.lon,
            price_per_kg=row.price_per_kg,
            currency=row.currency or p.org.currency,
            source=f"org:{p.org.id}",
            org_id=p.org.id,
            observed_on=row.observed_on or date.today(),
        )
    db.commit()
    return {"recorded": len(body.prices)}


@router.get("/prices")
def get_prices(crop: str | None = None, p: Principal = Depends(viewer), db: Session = Depends(get_db)):
    if crop:
        c = get_kb().resolve_crop(crop)
        if c is None:
            raise HTTPException(422, "unknown crop")
        return [q.to_dict() for q in latest_quotes(db, c.id, country=p.org.country, limit=50)]
    return market_summary(db, p.org.country)


# ------------------------------------------------------------------ outbreaks, analytics, messages, jobs


@router.get("/outbreaks")
def list_outbreaks(active: bool = True, p: Principal = Depends(viewer), db: Session = Depends(get_db)):
    kb = get_kb()
    q = select(Outbreak).order_by(Outbreak.last_seen.desc())
    if active:
        q = q.where(Outbreak.active.is_(True))
    return [
        {
            "id": o.id,
            "pest_id": o.pest_id,
            "pest": kb.pests[o.pest_id].names["en"] if o.pest_id in kb.pests else o.pest_id,
            "crop": o.crop,
            "lat": o.lat,
            "lon": o.lon,
            "radius_km": o.radius_km,
            "report_count": o.report_count,
            "first_seen": o.first_seen,
            "last_seen": o.last_seen,
            "active": o.active,
        }
        for o in db.scalars(q.limit(200))
    ]


@router.get("/analytics")
def analytics(days: int = Query(30, ge=1, le=365), p: Principal = Depends(viewer), db: Session = Depends(get_db)):
    """Reach, engagement, AI usage, cost drivers and market impact - the numbers partners report to funders."""
    org_id = p.org.id
    since = datetime.combine(date.today() - timedelta(days=days), datetime.min.time())

    def count(q) -> int:
        return int(db.scalar(q) or 0)

    farmers_total = count(select(func.count()).select_from(Farmer).where(Farmer.org_id == org_id))
    active = count(select(func.count()).select_from(Farmer).where(Farmer.org_id == org_id, Farmer.last_active_at >= since))
    opted_out = count(select(func.count()).select_from(Farmer).where(Farmer.org_id == org_id, Farmer.opted_out.is_(True)))
    by_gender = dict(db.execute(select(Farmer.gender, func.count()).where(Farmer.org_id == org_id).group_by(Farmer.gender)).all())
    by_crop = dict(
        db.execute(
            select(Plot.crop, func.count(func.distinct(Plot.farmer_id)))
            .join(Farmer, Farmer.id == Plot.farmer_id)
            .where(Farmer.org_id == org_id)
            .group_by(Plot.crop)
        ).all()
    )
    msgs = db.execute(
        select(Message.direction, Message.channel, func.count(), func.coalesce(func.sum(Message.segments), 0))
        .where(Message.org_id == org_id, Message.created_at >= since)
        .group_by(Message.direction, Message.channel)
    ).all()
    advice = dict(
        db.execute(
            select(AdviceLog.engine, func.count()).where(AdviceLog.org_id == org_id, AdviceLog.created_at >= since).group_by(AdviceLog.engine)
        ).all()
    )
    helpful = db.execute(
        select(func.count(), func.sum(case((AdviceLog.helpful.is_(True), 1), else_=0)))
        .where(AdviceLog.org_id == org_id, AdviceLog.helpful.is_not(None), AdviceLog.created_at >= since)
    ).one()
    diag_status = dict(
        db.execute(
            select(Diagnosis.status, func.count()).where(Diagnosis.org_id == org_id, Diagnosis.created_at >= since).group_by(Diagnosis.status)
        ).all()
    )
    top_pests = db.execute(
        select(Diagnosis.pest_id, func.count())
        .where(Diagnosis.org_id == org_id, Diagnosis.created_at >= since, Diagnosis.pest_id.is_not(None))
        .group_by(Diagnosis.pest_id)
        .order_by(func.count().desc())
        .limit(5)
    ).all()
    alerts = dict(
        db.execute(select(Alert.kind, func.count()).where(Alert.org_id == org_id, Alert.created_at >= since).group_by(Alert.kind)).all()
    )
    deals = db.execute(
        select(Match.status, func.count(), func.coalesce(func.sum(Match.quantity_kg), 0), func.coalesce(func.sum(Match.quantity_kg * Match.price_per_kg), 0))
        .join(Listing, Listing.id == Match.listing_id)
        .join(Farmer, Farmer.id == Listing.farmer_id)
        .where(Farmer.org_id == org_id, Match.created_at >= since)
        .group_by(Match.status)
    ).all()
    return {
        "period_days": days,
        "farmers": {
            "total": farmers_total,
            "active": active,
            "opted_out": opted_out,
            "by_gender": {k or "unknown": v for k, v in by_gender.items()},
            "by_crop": by_crop,
        },
        "messages": [
            {"direction": d, "channel": c, "count": n, "segments": int(seg)} for d, c, n, seg in msgs
        ],
        "advice": {"by_engine": advice, "rated": helpful[0] or 0, "helpful": int(helpful[1] or 0)},
        "diagnoses": {"by_status": diag_status, "top_pests": [{"pest_id": pid, "count": n} for pid, n in top_pests]},
        "alerts": alerts,
        "marketplace": {
            s_: {"deals": n, "kg": round(kg, 1), "value": round(val, 2)} for s_, n, kg, val in deals
        },
        "currency": p.org.currency,
    }


@router.get("/messages")
def list_messages(limit: int = Query(100, ge=1, le=1000), p: Principal = Depends(viewer), db: Session = Depends(get_db)):
    rows = db.scalars(select(Message).where(Message.org_id == p.org.id).order_by(Message.id.desc()).limit(limit))
    return [
        {
            "id": m.id,
            "farmer_id": m.farmer_id,
            "phone": m.phone,
            "direction": m.direction,
            "channel": m.channel,
            "body": m.body,
            "segments": m.segments,
            "status": m.status,
            "created_at": m.created_at,
        }
        for m in rows
    ]


@router.post("/jobs/run")
def run_org_jobs(jobs: list[str] = Query(list(JOBS)), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    unknown = set(jobs) - set(JOBS)
    if unknown:
        raise HTTPException(422, f"unknown jobs: {sorted(unknown)}")
    return run_jobs(db, tuple(jobs), org_id=p.org.id)

