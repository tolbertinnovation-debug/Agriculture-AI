"""Buyer API: processors, traders, exporters and institutional buyers source from smallholders."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import BuyRequest, BuyRequestStatus, Match, MatchStatus
from ..services import marketplace
from . import schemas as s
from .deps import Principal, officer, viewer
from .serializers import match_out

router = APIRouter(prefix="/api/v1/buyer", tags=["buyer"])


def _out(db: Session, req: BuyRequest) -> s.BuyRequestOut:
    out = s.BuyRequestOut.model_validate(req)
    out.committed_kg = marketplace.committed_kg(db, req)
    out.accepted_kg = float(
        db.scalar(
            select(func.coalesce(func.sum(Match.quantity_kg), 0.0)).where(
                Match.buy_request_id == req.id, Match.status.in_([MatchStatus.accepted.value, MatchStatus.completed.value])
            )
        )
        or 0
    )
    return out


def _own(db: Session, p: Principal, request_id: int) -> BuyRequest:
    req = db.get(BuyRequest, request_id)
    if req is None or req.buyer_org_id != p.org.id:
        raise HTTPException(404, "buy request not found")
    return req


def _own_match(db: Session, p: Principal, match_id: int) -> Match:
    m = db.get(Match, match_id)
    if m is None or m.buy_request.buyer_org_id != p.org.id:
        raise HTTPException(404, "match not found")
    return m


@router.get("/supply")
def supply(p: Principal = Depends(viewer), db: Session = Depends(get_db)):
    """Open smallholder supply aggregated by crop and region (no individual farmer data)."""
    return marketplace.supply_overview(db, p.org.country)


@router.post("/requests", response_model=s.BuyRequestOut, status_code=201)
def create_request(body: s.BuyRequestIn, p: Principal = Depends(officer), db: Session = Depends(get_db)):
    req = BuyRequest(buyer_org_id=p.org.id, currency=p.org.currency, **body.model_dump())
    db.add(req)
    db.flush()
    marketplace.run_matching(db, req)
    db.commit()
    return _out(db, req)


@router.get("/requests", response_model=list[s.BuyRequestOut])
def list_requests(status: str | None = None, p: Principal = Depends(viewer), db: Session = Depends(get_db)):
    q = select(BuyRequest).where(BuyRequest.buyer_org_id == p.org.id)
    if status:
        q = q.where(BuyRequest.status == status)
    return [_out(db, r) for r in db.scalars(q.order_by(BuyRequest.id.desc()))]


@router.get("/requests/{request_id}", response_model=s.BuyRequestOut)
def get_request(request_id: int, p: Principal = Depends(viewer), db: Session = Depends(get_db)):
    return _out(db, _own(db, p, request_id))


@router.post("/requests/{request_id}/close", response_model=s.BuyRequestOut)
def close_request(request_id: int, p: Principal = Depends(officer), db: Session = Depends(get_db)):
    req = _own(db, p, request_id)
    req.status = BuyRequestStatus.closed.value
    for m in db.scalars(select(Match).where(Match.buy_request_id == req.id, Match.status == MatchStatus.proposed.value)):
        m.status = MatchStatus.cancelled.value
    db.commit()
    return _out(db, req)


@router.post("/requests/{request_id}/rematch", response_model=list[s.MatchOut])
def rematch(request_id: int, p: Principal = Depends(officer), db: Session = Depends(get_db)):
    req = _own(db, p, request_id)
    created = marketplace.run_matching(db, req)
    db.commit()
    return [match_out(m, for_buyer=True) for m in created]


@router.get("/requests/{request_id}/candidates")
def preview_candidates(request_id: int, p: Principal = Depends(viewer), db: Session = Depends(get_db)):
    """Ranked eligible lots and their score breakdown (anonymised)."""
    req = _own(db, p, request_id)
    return [
        {"listing_id": c.listing.id, "region": c.listing.farmer.region, "grade": c.listing.grade, "distance_km": round(c.distance_km, 1), "score": c.score, "factors": c.reasons}
        for c in marketplace.candidates(db, req)
    ]


@router.get("/requests/{request_id}/matches", response_model=list[s.MatchOut])
def request_matches(request_id: int, p: Principal = Depends(viewer), db: Session = Depends(get_db)):
    req = _own(db, p, request_id)
    rows = db.scalars(select(Match).where(Match.buy_request_id == req.id).order_by(Match.score.desc()))
    return [match_out(m, for_buyer=True) for m in rows]


@router.post("/matches/{match_id}/complete", response_model=s.MatchOut)
def complete(match_id: int, p: Principal = Depends(officer), db: Session = Depends(get_db)):
    m = _own_match(db, p, match_id)
    try:
        marketplace.complete_match(db, m)
    except marketplace.MatchError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return match_out(m, for_buyer=True)


@router.post("/matches/{match_id}/cancel", response_model=s.MatchOut)
def cancel(match_id: int, p: Principal = Depends(officer), db: Session = Depends(get_db)):
    m = _own_match(db, p, match_id)
    try:
        marketplace.cancel_match(db, m)
    except marketplace.MatchError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.flush()
    marketplace.run_matching(db, m.buy_request)
    db.commit()
    return match_out(m, for_buyer=True)
