"""Database models.

Tenancy: every distribution partner (telco, co-operative, NGO, agribusiness) is an
``Organization``. Farmers belong to exactly one partner organisation, and every
partner-facing query is scoped by ``org_id``. Buyers are organisations too
(``kind="buyer"``); the marketplace is the one place data crosses tenants, and
only for farmers who consented to it, in organisations that enabled it.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OrgKind(str, enum.Enum):
    telco = "telco"
    cooperative = "cooperative"
    ngo = "ngo"
    agribusiness = "agribusiness"
    government = "government"
    buyer = "buyer"


class Role(str, enum.Enum):
    admin = "admin"  # manage keys, settings, broadcasts
    officer = "officer"  # extension officer: farmers, diagnoses review
    viewer = "viewer"  # read-only analytics


class DiagnosisStatus(str, enum.Enum):
    auto = "auto"  # confident AI result delivered to farmer
    needs_review = "needs_review"  # queued for an extension officer
    confirmed = "confirmed"  # officer confirmed / corrected
    rejected = "rejected"  # officer says image unusable / not a problem


class ListingStatus(str, enum.Enum):
    open = "open"
    sold = "sold"
    withdrawn = "withdrawn"
    expired = "expired"


class BuyRequestStatus(str, enum.Enum):
    open = "open"
    filled = "filled"
    closed = "closed"


class MatchStatus(str, enum.Enum):
    proposed = "proposed"  # offered to the farmer, awaiting their answer
    accepted = "accepted"  # farmer accepted; buyer sees contact details
    declined = "declined"
    completed = "completed"  # buyer confirmed collection/payment
    cancelled = "cancelled"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32), default=OrgKind.cooperative.value)
    country: Mapped[str] = mapped_column(String(2), default="KE")
    currency: Mapped[str] = mapped_column(String(3), default="KES")
    default_language: Mapped[str] = mapped_column(String(8), default="en")
    # White-label name shown in SMS signatures and the farmer app.
    brand_name: Mapped[str | None] = mapped_column(String(60))
    # Inbound SMS to this shortcode / USSD code is attributed to this organisation.
    sms_shortcode: Mapped[str | None] = mapped_column(String(32), unique=True)
    ussd_code: Mapped[str | None] = mapped_column(String(32), unique=True)
    marketplace_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Shared with farmers once they accept a buyer's offer.
    contact_phone: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="org", cascade="all, delete-orphan")
    farmers: Mapped[list[Farmer]] = relationship(back_populates="org")

    @property
    def display_name(self) -> str:
        return self.brand_name or self.name


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    role: Mapped[str] = mapped_column(String(16), default=Role.admin.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)

    org: Mapped[Organization] = relationship(back_populates="api_keys")


class Farmer(Base):
    __tablename__ = "farmers"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True)  # E.164
    name: Mapped[str | None] = mapped_column(String(120))
    language: Mapped[str] = mapped_column(String(8), default="en")
    gender: Mapped[str | None] = mapped_column(String(16))  # for partners' disaggregated impact reporting
    region: Mapped[str | None] = mapped_column(String(120))
    village: Mapped[str | None] = mapped_column(String(120))
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    # Explicit consent to show produce listings to buyers outside the partner org.
    consent_marketplace: Mapped[bool] = mapped_column(Boolean, default=False)
    # Farmer replied STOP: no outbound messages except direct replies.
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime)

    org: Mapped[Organization] = relationship(back_populates="farmers")
    plots: Mapped[list[Plot]] = relationship(back_populates="farmer", cascade="all, delete-orphan")

    @property
    def crops(self) -> list[str]:
        return sorted({p.crop for p in self.plots})


class Plot(Base):
    __tablename__ = "plots"

    id: Mapped[int] = mapped_column(primary_key=True)
    farmer_id: Mapped[int] = mapped_column(ForeignKey("farmers.id", ondelete="CASCADE"), index=True)
    crop: Mapped[str] = mapped_column(String(40))  # knowledge-base crop id
    variety: Mapped[str | None] = mapped_column(String(80))
    area_ha: Mapped[float | None] = mapped_column(Float)
    planting_date: Mapped[date | None] = mapped_column(Date)
    irrigated: Mapped[bool] = mapped_column(Boolean, default=False)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)

    farmer: Mapped[Farmer] = relationship(back_populates="plots")


class OtpCode(Base):
    __tablename__ = "otp_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), index=True)
    code_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Diagnosis(Base):
    __tablename__ = "diagnoses"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    farmer_id: Mapped[int] = mapped_column(ForeignKey("farmers.id", ondelete="CASCADE"), index=True)
    crop: Mapped[str | None] = mapped_column(String(40))
    channel: Mapped[str] = mapped_column(String(16), default="app")
    source: Mapped[str] = mapped_column(String(16), default="photo")  # photo | text
    engine: Mapped[str] = mapped_column(String(16), default="rules")  # llm | rules | officer
    image_path: Mapped[str | None] = mapped_column(String(300))
    symptoms_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=DiagnosisStatus.auto.value, index=True)
    pest_id: Mapped[str | None] = mapped_column(String(60), index=True)
    label: Mapped[str | None] = mapped_column(String(160))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    severity: Mapped[str | None] = mapped_column(String(16))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    reviewer_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    farmer: Mapped[Farmer] = relationship()


class Outbreak(Base):
    """A geographic cluster of confirmed reports of the same pest or disease."""

    __tablename__ = "outbreaks"

    id: Mapped[int] = mapped_column(primary_key=True)
    pest_id: Mapped[str] = mapped_column(String(60), index=True)
    crop: Mapped[str | None] = mapped_column(String(40))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    radius_km: Mapped[float] = mapped_column(Float)
    report_count: Mapped[int] = mapped_column(Integer)
    first_seen: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MarketPrice(Base):
    __tablename__ = "market_prices"
    __table_args__ = (Index("ix_price_crop_market_day", "crop", "market", "observed_on"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    crop: Mapped[str] = mapped_column(String(40))
    market: Mapped[str] = mapped_column(String(120))
    region: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str] = mapped_column(String(2), default="KE")
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    price_per_kg: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="KES")
    source: Mapped[str] = mapped_column(String(60), default="partner")
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"))
    observed_on: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PriceAlertSubscription(Base):
    __tablename__ = "price_alert_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    farmer_id: Mapped[int] = mapped_column(ForeignKey("farmers.id", ondelete="CASCADE"), index=True)
    crop: Mapped[str] = mapped_column(String(40))
    market: Mapped[str | None] = mapped_column(String(120))  # None = nearest market
    direction: Mapped[str] = mapped_column(String(8), default="above")  # above | below
    threshold_per_kg: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WeatherCache(Base):
    __tablename__ = "weather_cache"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("farmer_id", "dedupe_key", name="uq_alert_dedupe"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    farmer_id: Mapped[int] = mapped_column(ForeignKey("farmers.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # weather | price | outbreak | market | broadcast
    severity: Mapped[str] = mapped_column(String(16), default="info")  # info | warning | critical
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(160))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Message(Base):
    """Every inbound and outbound message: audit trail and the basis for partner billing."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), index=True)
    farmer_id: Mapped[int | None] = mapped_column(ForeignKey("farmers.id", ondelete="SET NULL"), index=True)
    phone: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str] = mapped_column(String(4))  # in | out
    channel: Mapped[str] = mapped_column(String(16))  # sms | ussd | whatsapp | app
    body: Mapped[str] = mapped_column(Text)
    segments: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="sent")
    provider_ref: Mapped[str | None] = mapped_column(String(120))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AdviceLog(Base):
    __tablename__ = "advice_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    farmer_id: Mapped[int] = mapped_column(ForeignKey("farmers.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(16), default="app")
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    engine: Mapped[str] = mapped_column(String(16))  # llm | rules
    helpful: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Listing(Base):
    """Produce a farmer offers for sale."""

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    farmer_id: Mapped[int] = mapped_column(ForeignKey("farmers.id", ondelete="CASCADE"), index=True)
    crop: Mapped[str] = mapped_column(String(40), index=True)
    quantity_kg: Mapped[float] = mapped_column(Float)
    remaining_kg: Mapped[float] = mapped_column(Float)
    grade: Mapped[str] = mapped_column(String(8), default="B")  # A (best) | B | C
    min_price_per_kg: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="KES")
    available_from: Mapped[date] = mapped_column(Date)
    available_until: Mapped[date | None] = mapped_column(Date)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default=ListingStatus.open.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    farmer: Mapped[Farmer] = relationship()


class BuyRequest(Base):
    """Demand posted by a buyer organisation (processor, exporter, trader, school-feeding programme...)."""

    __tablename__ = "buy_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    buyer_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    crop: Mapped[str] = mapped_column(String(40), index=True)
    quantity_kg: Mapped[float] = mapped_column(Float)
    min_lot_kg: Mapped[float] = mapped_column(Float, default=0)
    price_per_kg: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="KES")
    min_grade: Mapped[str] = mapped_column(String(8), default="C")
    needed_by: Mapped[date | None] = mapped_column(Date)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    radius_km: Mapped[float] = mapped_column(Float, default=50)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=BuyRequestStatus.open.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    buyer: Mapped[Organization] = relationship()


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("buy_request_id", "listing_id", name="uq_match_pair"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True)  # farmer replies "YES <code>"
    buy_request_id: Mapped[int] = mapped_column(ForeignKey("buy_requests.id", ondelete="CASCADE"), index=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    quantity_kg: Mapped[float] = mapped_column(Float)
    price_per_kg: Mapped[float] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float)
    distance_km: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default=MatchStatus.proposed.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    buy_request: Mapped[BuyRequest] = relationship()
    listing: Mapped[Listing] = relationship()
