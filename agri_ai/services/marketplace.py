"""Farmer-to-buyer matching.

A buyer posts demand (crop, quantity, price, location, grade). Smallholder lots are
usually far smaller than a buyer's order, so the matcher *aggregates*: it ranks
eligible lots and fills the order from several farms, the way a co-operative
bulks produce. Farmers accept or decline each offer by SMS ("YES ABC12").
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import i18n
from ..knowledge import get_kb
from ..models import (
    BuyRequest,
    BuyRequestStatus,
    Farmer,
    Listing,
    ListingStatus,
    Match,
    MatchStatus,
    Organization,
    utcnow,
)
from .geo import haversine_km
from .notifications import send_sms
from .prices import record_price
from .weather import farmer_location

GRADE_RANK = {"A": 3, "B": 2, "C": 1}
OFFER_TTL_HOURS = 48
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L confusion on SMS

# Live statuses hold quantity against a buy request.
_LIVE = (MatchStatus.proposed.value, MatchStatus.accepted.value, MatchStatus.completed.value)


@dataclass
class Candidate:
    listing: Listing
    distance_km: float
    score: float
    reasons: dict


def new_code(db: Session) -> str:
    while True:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(5))
        if not db.scalar(select(Match.id).where(Match.code == code)):
            return code


def farmer_reliability(db: Session, farmer_id: int) -> float:
    """Share of accepted deals the farmer completed, with a neutral prior for newcomers."""
    rows = db.execute(
        select(Match.status, func.count())
        .join(Listing, Listing.id == Match.listing_id)
        .where(Listing.farmer_id == farmer_id, Match.status.in_([MatchStatus.completed.value, MatchStatus.cancelled.value]))
        .group_by(Match.status)
    ).all()
    counts = dict(rows)
    done, failed = counts.get(MatchStatus.completed.value, 0), counts.get(MatchStatus.cancelled.value, 0)
    prior_good, prior_n = 3.5, 5  # equivalent to 70% over five deals
    return (done + prior_good) / (done + failed + prior_n)


def committed_kg(db: Session, request: BuyRequest) -> float:
    return float(
        db.scalar(
            select(func.coalesce(func.sum(Match.quantity_kg), 0.0)).where(
                Match.buy_request_id == request.id, Match.status.in_(_LIVE)
            )
        )
        or 0.0
    )


def listing_held_kg(db: Session, listing: Listing) -> float:
    """Quantity of a listing tied up in offers the farmer has not answered yet."""
    return float(
        db.scalar(
            select(func.coalesce(func.sum(Match.quantity_kg), 0.0)).where(
                Match.listing_id == listing.id, Match.status == MatchStatus.proposed.value
            )
        )
        or 0.0
    )


def candidates(db: Session, request: BuyRequest, today: date | None = None) -> list[Candidate]:
    today = today or date.today()
    q = (
        select(Listing)
        .join(Farmer, Farmer.id == Listing.farmer_id)
        .join(Organization, Organization.id == Farmer.org_id)
        .where(
            Listing.crop == request.crop,
            Listing.currency == request.currency,
            Listing.status == ListingStatus.open.value,
            Listing.remaining_kg > 0,
            Farmer.consent_marketplace.is_(True),
            Farmer.opted_out.is_(False),
            Organization.marketplace_enabled.is_(True),
        )
    )
    already = set(db.scalars(select(Match.listing_id).where(Match.buy_request_id == request.id)).all())
    out: list[Candidate] = []
    for listing in db.scalars(q).all():
        if listing.id in already:
            continue
        if GRADE_RANK.get(listing.grade, 1) < GRADE_RANK.get(request.min_grade, 1):
            continue
        if listing.min_price_per_kg is not None and listing.min_price_per_kg > request.price_per_kg:
            continue
        if listing.available_until is not None and listing.available_until < today:
            continue
        if request.needed_by is not None and listing.available_from > request.needed_by:
            continue
        free = listing.remaining_kg - listing_held_kg(db, listing)
        if free <= 0 or free < request.min_lot_kg:
            continue
        lat, lon = listing.lat, listing.lon
        if lat is None or lon is None:
            loc = farmer_location(listing.farmer)
            if loc is None:
                continue
            lat, lon = loc
        dist = haversine_km(lat, lon, request.lat, request.lon)
        if dist > request.radius_km:
            continue

        need = max(request.quantity_kg - committed_kg(db, request), 1.0)
        distance_s = 1 - dist / request.radius_km
        price_s = 1.0 if listing.min_price_per_kg is None else min(1.0, 0.5 + (request.price_per_kg - listing.min_price_per_kg) / request.price_per_kg * 2)
        volume_s = min(1.0, free / min(need, max(request.min_lot_kg, need * 0.25)))  # bigger lots = fewer pickups
        grade_s = (GRADE_RANK.get(listing.grade, 1) - 1) / 2
        reliability_s = farmer_reliability(db, listing.farmer_id)
        score = 0.35 * distance_s + 0.15 * price_s + 0.2 * volume_s + 0.1 * grade_s + 0.2 * reliability_s
        out.append(
            Candidate(
                listing,
                dist,
                round(score, 4),
                {
                    "distance": round(distance_s, 2),
                    "price": round(price_s, 2),
                    "volume": round(volume_s, 2),
                    "grade": round(grade_s, 2),
                    "reliability": round(reliability_s, 2),
                    "available_kg": free,
                },
            )
        )
    out.sort(key=lambda c: (-c.score, c.distance_km))
    return out


def run_matching(db: Session, request: BuyRequest, *, notify: bool = True) -> list[Match]:
    """Propose offers to the best lots until the buyer's open quantity is covered."""
    if request.status != BuyRequestStatus.open.value:
        return []
    need = request.quantity_kg - committed_kg(db, request)
    created: list[Match] = []
    for cand in candidates(db, request):
        if need <= 0:
            break
        free = cand.reasons["available_kg"]
        qty = min(free, need)
        if qty < request.min_lot_kg:
            continue
        match = Match(
            code=new_code(db),
            buy_request_id=request.id,
            listing_id=cand.listing.id,
            quantity_kg=round(qty, 1),
            price_per_kg=request.price_per_kg,
            score=cand.score,
            distance_km=round(cand.distance_km, 1),
        )
        db.add(match)
        db.flush()
        created.append(match)
        need -= qty
        if notify:
            _offer_sms(db, match)
    return created


def match_for_new_listing(db: Session, listing: Listing) -> list[Match]:
    """A new lot may complete open orders that were short of supply."""
    out: list[Match] = []
    for req in db.scalars(
        select(BuyRequest).where(
            BuyRequest.crop == listing.crop,
            BuyRequest.status == BuyRequestStatus.open.value,
            BuyRequest.currency == listing.currency,
        ).order_by(BuyRequest.created_at)
    ).all():
        out.extend(run_matching(db, req))
    return out


def _offer_sms(db: Session, match: Match) -> None:
    farmer = match.listing.farmer
    lang = farmer.language
    crop = get_kb().crops.get(match.listing.crop)
    send_sms(
        db,
        farmer.phone,
        i18n.t(
            "match_offer",
            lang,
            buyer=match.buy_request.buyer.display_name,
            qty=f"{match.quantity_kg:g}",
            crop=crop.name(lang) if crop else match.listing.crop,
            price=f"{match.price_per_kg:g}",
            currency=match.buy_request.currency,
            distance=round(match.distance_km or 0),
            code=match.code,
        ),
        farmer=farmer,
        transactional=True,
    )


class MatchError(Exception):
    pass


def farmer_respond(db: Session, farmer: Farmer, code: str, accept: bool) -> tuple[Match | None, str]:
    """Handle a farmer's YES/NO to an offer. Returns the match and the reply text."""
    lang = farmer.language
    code = code.strip().upper()
    match = db.scalar(
        select(Match)
        .join(Listing, Listing.id == Match.listing_id)
        .where(Match.code == code, Listing.farmer_id == farmer.id, Match.status == MatchStatus.proposed.value)
    )
    if match is None:
        return None, i18n.t("match_not_found", lang, code=code)
    listing, request = match.listing, match.buy_request
    crop = get_kb().crops.get(listing.crop)
    crop_name = crop.name(lang) if crop else listing.crop

    if not accept:
        match.status = MatchStatus.declined.value
        db.flush()
        run_matching(db, request)  # try to fill the gap with the next best lot
        return match, i18n.t("match_declined", lang, code=code)

    if listing.remaining_kg < match.quantity_kg:
        match.status = MatchStatus.cancelled.value
        return match, i18n.t("match_not_found", lang, code=code)
    match.status = MatchStatus.accepted.value
    listing.remaining_kg = round(listing.remaining_kg - match.quantity_kg, 1)
    if listing.remaining_kg <= 0:
        listing.status = ListingStatus.sold.value
    accepted = float(
        db.scalar(
            select(func.coalesce(func.sum(Match.quantity_kg), 0.0)).where(
                Match.buy_request_id == request.id, Match.status.in_([MatchStatus.accepted.value, MatchStatus.completed.value])
            )
        )
        or 0
    )
    if accepted >= request.quantity_kg - 1e-6:
        request.status = BuyRequestStatus.filled.value
        # Withdraw offers nobody needs any more.
        for other in db.scalars(select(Match).where(Match.buy_request_id == request.id, Match.status == MatchStatus.proposed.value)):
            other.status = MatchStatus.cancelled.value
    buyer = request.buyer
    reply = i18n.t(
        "match_accepted",
        lang,
        code=code,
        qty=f"{match.quantity_kg:g}",
        crop=crop_name,
        price=f"{match.price_per_kg:g}",
        currency=request.currency,
        buyer=buyer.display_name,
        buyer_contact=buyer.contact_phone or "-",
    )
    return match, reply


def complete_match(db: Session, match: Match) -> Match:
    """Buyer confirms collection and payment. The deal price becomes a market price point."""
    if match.status != MatchStatus.accepted.value:
        raise MatchError("only accepted deals can be completed")
    match.status = MatchStatus.completed.value
    request = match.buy_request
    farmer = match.listing.farmer
    record_price(
        db,
        crop=match.listing.crop,
        market=f"{request.buyer.display_name} (deal)",
        region=farmer.region,
        country=farmer.org.country,
        lat=request.lat,
        lon=request.lon,
        price_per_kg=match.price_per_kg,
        currency=request.currency,
        source="deal",
        observed_on=date.today(),
    )
    return match


def cancel_match(db: Session, match: Match) -> Match:
    if match.status not in (MatchStatus.proposed.value, MatchStatus.accepted.value):
        raise MatchError("match cannot be cancelled")
    if match.status == MatchStatus.accepted.value:
        match.listing.remaining_kg = round(match.listing.remaining_kg + match.quantity_kg, 1)
        if match.listing.status == ListingStatus.sold.value:
            match.listing.status = ListingStatus.open.value
        if match.buy_request.status == BuyRequestStatus.filled.value:
            match.buy_request.status = BuyRequestStatus.open.value
    match.status = MatchStatus.cancelled.value
    return match


def expire_stale(db: Session) -> int:
    """Withdraw offers farmers did not answer in time, then re-match the gap; expire old listings."""
    cutoff = utcnow() - timedelta(hours=OFFER_TTL_HOURS)
    stale = db.scalars(select(Match).where(Match.status == MatchStatus.proposed.value, Match.created_at < cutoff)).all()
    requests = set()
    for m in stale:
        m.status = MatchStatus.cancelled.value
        requests.add(m.buy_request)
    for lst in db.scalars(
        select(Listing).where(Listing.status == ListingStatus.open.value, Listing.available_until.is_not(None), Listing.available_until < date.today())
    ):
        lst.status = ListingStatus.expired.value
    db.flush()
    for req in requests:
        run_matching(db, req)
    return len(stale)


def supply_overview(db: Session, country: str | None = None) -> list[dict]:
    """Aggregated open supply by crop and region (no farmer identities) for buyers."""
    q = (
        select(Listing.crop, Farmer.region, Listing.currency, func.count(), func.sum(Listing.remaining_kg))
        .join(Farmer, Farmer.id == Listing.farmer_id)
        .join(Organization, Organization.id == Farmer.org_id)
        .where(
            Listing.status == ListingStatus.open.value,
            Farmer.consent_marketplace.is_(True),
            Organization.marketplace_enabled.is_(True),
        )
        .group_by(Listing.crop, Farmer.region, Listing.currency)
    )
    if country:
        q = q.where(Organization.country == country)
    return [
        {"crop": c, "region": r, "currency": cur, "lots": n, "total_kg": round(kg or 0, 1)} for c, r, cur, n, kg in db.execute(q)
    ]
