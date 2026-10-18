"""Outbreak detection: turn individual diagnoses into early warnings for neighbours.

This is the network effect of the platform: every farmer who sends a photo makes
the alerts better for everyone nearby, across all partner organisations.
Only the pest, a rounded location and a count leave the reporting farmer's org.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import i18n
from ..config import get_settings
from ..knowledge import get_kb
from ..models import Diagnosis, DiagnosisStatus, Farmer, Outbreak, utcnow
from .geo import haversine_km
from .notifications import create_alert
from .weather import farmer_location

# Only trusted reports count: officer-confirmed, or confident automatic photo diagnoses.
AUTO_MIN_CONFIDENCE = 0.75


def _trusted_reports(db: Session, since) -> list[Diagnosis]:
    rows = db.scalars(
        select(Diagnosis).where(
            Diagnosis.created_at >= since,
            Diagnosis.lat.is_not(None),
            Diagnosis.pest_id.is_not(None),
            Diagnosis.pest_id.not_in(["none", "other"]),
            Diagnosis.status.in_([DiagnosisStatus.auto.value, DiagnosisStatus.confirmed.value]),
        )
    ).all()
    return [d for d in rows if d.status == DiagnosisStatus.confirmed.value or (d.source == "photo" and d.confidence >= AUTO_MIN_CONFIDENCE)]


def detect_outbreaks(db: Session) -> list[Outbreak]:
    """Greedy density clustering per pest; creates or refreshes ``Outbreak`` rows."""
    s = get_settings()
    now = utcnow()
    reports = _trusted_reports(db, now - timedelta(days=s.outbreak_window_days))
    by_pest: dict[str, list[Diagnosis]] = {}
    for d in reports:
        by_pest.setdefault(d.pest_id, []).append(d)

    touched: list[Outbreak] = []
    for pest_id, items in by_pest.items():
        unassigned = list(items)
        while unassigned:
            seed = max(unassigned, key=lambda d: sum(haversine_km(d.lat, d.lon, o.lat, o.lon) <= s.outbreak_radius_km for o in unassigned))
            cluster = [d for d in unassigned if haversine_km(seed.lat, seed.lon, d.lat, d.lon) <= s.outbreak_radius_km]
            # Count distinct farms, not repeated photos from one farmer.
            farms = {d.farmer_id for d in cluster}
            unassigned = [d for d in unassigned if d not in cluster]
            if len(farms) < s.outbreak_min_reports:
                continue
            lat = sum(d.lat for d in cluster) / len(cluster)
            lon = sum(d.lon for d in cluster) / len(cluster)
            existing = next(
                (
                    o
                    for o in db.scalars(select(Outbreak).where(Outbreak.pest_id == pest_id, Outbreak.active.is_(True))).all()
                    if haversine_km(o.lat, o.lon, lat, lon) <= s.outbreak_radius_km
                ),
                None,
            )
            crops = [d.crop for d in cluster if d.crop]
            crop = max(set(crops), key=crops.count) if crops else None
            if existing:
                existing.lat, existing.lon = lat, lon
                existing.report_count = len(farms)
                existing.last_seen = max(d.created_at for d in cluster)
                touched.append(existing)
            else:
                ob = Outbreak(
                    pest_id=pest_id,
                    crop=crop,
                    lat=round(lat, 3),
                    lon=round(lon, 3),
                    radius_km=s.outbreak_radius_km,
                    report_count=len(farms),
                    first_seen=min(d.created_at for d in cluster),
                    last_seen=max(d.created_at for d in cluster),
                )
                db.add(ob)
                touched.append(ob)

    # Close outbreaks with no new reports in the window.
    for ob in db.scalars(select(Outbreak).where(Outbreak.active.is_(True))).all():
        if ob.last_seen < now - timedelta(days=s.outbreak_window_days):
            ob.active = False
    db.flush()
    return touched


def alert_outbreak(db: Session, outbreak: Outbreak, farmers: list[Farmer]) -> int:
    """Warn farmers who grow an affected crop within 1.5x the outbreak radius."""
    pest = get_kb().pests.get(outbreak.pest_id)
    if pest is None:
        return 0
    sent = 0
    for farmer in farmers:
        loc = farmer_location(farmer)
        if loc is None:
            continue
        grown = [c for c in farmer.crops if c in pest.crops]
        if not grown:
            continue
        if haversine_km(loc[0], loc[1], outbreak.lat, outbreak.lon) > outbreak.radius_km * 1.5:
            continue
        lang = farmer.language
        crop = get_kb().crops[grown[0]]
        body = i18n.t(
            "outbreak",
            lang,
            pest=pest.name(lang),
            count=outbreak.report_count,
            radius=round(outbreak.radius_km * 1.5),
            crop=crop.name(lang),
            action=pest.actions(1)[0] if pest.actions(1) else "",
        )
        if create_alert(
            db,
            farmer,
            kind="outbreak",
            title=f"{pest.names['en']} outbreak nearby",
            body=body,
            dedupe_key=f"outbreak:{outbreak.id}",
            severity="critical" if pest.severity == "critical" else "warning",
            data={"outbreak_id": outbreak.id, "pest_id": pest.id},
        ):
            sent += 1
    return sent
