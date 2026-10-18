"""Farmer registration and lookup shared by all channels."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..i18n import lang_or_default
from ..knowledge import get_kb
from ..models import Farmer, Listing, ListingStatus, Organization, Plot, utcnow
from .geo import normalize_phone


def org_for_inbound(db: Session, *, shortcode: str | None = None, ussd_code: str | None = None) -> Organization | None:
    if ussd_code:
        org = db.scalar(select(Organization).where(Organization.ussd_code == ussd_code))
        if org:
            return org
    if shortcode:
        org = db.scalar(select(Organization).where(Organization.sms_shortcode == shortcode))
        if org:
            return org
    # Single-tenant deployments: route everything to the only farmer-facing org.
    orgs = db.scalars(select(Organization).where(Organization.kind != "buyer").limit(2)).all()
    return orgs[0] if len(orgs) == 1 else None


def find_farmer(db: Session, phone: str) -> Farmer | None:
    return db.scalar(select(Farmer).where(Farmer.phone == phone))


def get_or_register(db: Session, raw_phone: str, org: Organization | None) -> tuple[Farmer | None, bool]:
    """Return ``(farmer, created)``. Unknown numbers self-register with the channel's org."""
    country = org.country if org else "KE"
    phone = normalize_phone(raw_phone, country)
    farmer = find_farmer(db, phone)
    if farmer:
        farmer.last_active_at = utcnow()
        return farmer, False
    if org is None:
        return None, False
    farmer = Farmer(org_id=org.id, phone=phone, language=lang_or_default(org.default_language), last_active_at=utcnow())
    db.add(farmer)
    db.flush()
    return farmer, True


def add_plot(db: Session, farmer: Farmer, crop: str, **fields) -> Plot:
    crop_obj = get_kb().resolve_crop(crop)
    if crop_obj is None:
        raise ValueError(f"unknown crop: {crop}")
    plot = Plot(farmer_id=farmer.id, crop=crop_obj.id, **fields)
    db.add(plot)
    farmer.plots.append(plot)
    db.flush()
    return plot


BAG_KG = {"maize": 90, "beans": 90, "sorghum": 90, "rice": 50, "groundnut": 50, "potato": 50, "coffee": 60}


def create_listing(
    db: Session,
    farmer: Farmer,
    crop: str,
    quantity_kg: float,
    *,
    min_price_per_kg: float | None = None,
    grade: str = "B",
    available_from: date | None = None,
    available_until: date | None = None,
) -> Listing:
    crop_obj = get_kb().resolve_crop(crop)
    if crop_obj is None:
        raise ValueError(f"unknown crop: {crop}")
    if quantity_kg <= 0:
        raise ValueError("quantity must be positive")
    start = available_from or date.today()
    listing = Listing(
        farmer_id=farmer.id,
        crop=crop_obj.id,
        quantity_kg=quantity_kg,
        remaining_kg=quantity_kg,
        min_price_per_kg=min_price_per_kg,
        grade=grade if grade in {"A", "B", "C"} else "B",
        currency=farmer.org.currency,
        available_from=start,
        available_until=available_until or start + timedelta(days=30),
        lat=farmer.lat,
        lon=farmer.lon,
        status=ListingStatus.open.value,
    )
    db.add(listing)
    db.flush()
    return listing
