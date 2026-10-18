"""Scheduled jobs. Run from cron, e.g. every 3 hours::

    python -m agri_ai.jobs            # all jobs
    python -m agri_ai.jobs weather    # one job

Every job is idempotent (alerts are de-duplicated), so overlapping or repeated
runs never double-message farmers.
"""

from __future__ import annotations

import logging
import sys
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .db import create_all, session_factory
from .knowledge import get_kb
from .models import Diagnosis, DiagnosisStatus, Farmer, Outbreak, utcnow
from .services import advisory, marketplace, outbreaks, prices
from .services.notifications import create_alert
from .services.weather import evaluate_forecast, farmer_crop_contexts, farmer_location, get_forecast

log = logging.getLogger("agri_ai.jobs")

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
MAX_WEATHER_ALERTS_PER_RUN = 2  # respect farmers' attention (and partners' SMS bills)


def _active_farmers(db: Session, org_id: int | None = None) -> list[Farmer]:
    q = select(Farmer).options(selectinload(Farmer.plots), selectinload(Farmer.org)).where(Farmer.opted_out.is_(False))
    if org_id is not None:
        q = q.where(Farmer.org_id == org_id)
    return list(db.scalars(q))


def run_weather_alerts(db: Session, farmers: list[Farmer]) -> int:
    kb = get_kb()
    sent = 0
    since = utcnow() - timedelta(days=14)
    for farmer in farmers:
        loc = farmer_location(farmer)
        if loc is None:
            continue
        fc = get_forecast(db, *loc)
        if not fc:
            continue
        recent = db.scalars(
            select(Diagnosis.pest_id).where(
                Diagnosis.farmer_id == farmer.id,
                Diagnosis.created_at >= since,
                Diagnosis.status.in_([DiagnosisStatus.auto.value, DiagnosisStatus.confirmed.value]),
            )
        ).all()
        needs_spraying = any(kb.pests.get(p) and (kb.pests[p].chemical or kb.pests[p].biological) for p in recent)
        events = evaluate_forecast(fc, farmer_crop_contexts(farmer), farmer.language, needs_spraying=needs_spraying)
        events.sort(key=lambda e: SEVERITY_ORDER.get(e.severity, 3))
        for ev in events[:MAX_WEATHER_ALERTS_PER_RUN]:
            if create_alert(
                db,
                farmer,
                kind="weather",
                title=ev.kind.replace("_", " ").capitalize(),
                body=ev.message,
                dedupe_key=f"wx:{ev.kind}:{ev.day.isoformat()}",
                severity=ev.severity,
                data={"event": ev.kind, "day": ev.day.isoformat()},
            ):
                sent += 1
    return sent


def run_outbreaks(db: Session, farmers: list[Farmer]) -> dict:
    touched = outbreaks.detect_outbreaks(db)
    alerts = 0
    for ob in db.scalars(select(Outbreak).where(Outbreak.active.is_(True))):
        alerts += outbreaks.alert_outbreak(db, ob, farmers)
    return {"outbreaks_updated": len(touched), "outbreak_alerts": alerts}


JOBS = ("weather", "prices", "stages", "outbreaks", "marketplace")


def run_jobs(db: Session, which: tuple[str, ...] = JOBS, org_id: int | None = None) -> dict:
    farmers = _active_farmers(db, org_id)
    summary: dict = {"farmers": len(farmers)}
    if "weather" in which:
        summary["weather_alerts"] = run_weather_alerts(db, farmers)
    if "prices" in which:
        summary["price_alerts"] = prices.run_price_alerts(db, farmers)
    if "stages" in which:
        summary["stage_tips"] = advisory.run_stage_tips(db, farmers)
    if "outbreaks" in which:
        # Outbreak alerts cross partner boundaries, so they always consider every farmer.
        summary.update(run_outbreaks(db, farmers if org_id is None else _active_farmers(db)))
    if "marketplace" in which:
        summary["offers_expired"] = marketplace.expire_stale(db)
    db.commit()
    return summary


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO)
    create_all()
    which = tuple(a for a in argv if a in JOBS) or JOBS
    with session_factory()() as db:
        print(run_jobs(db, which))


if __name__ == "__main__":
    main(sys.argv[1:])
