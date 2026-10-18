from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..i18n import SUPPORTED_LANGUAGES
from ..knowledge import get_kb


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _crop_id(v: str) -> str:
    crop = get_kb().resolve_crop(v)
    if crop is None:
        raise ValueError(f"unknown crop '{v}'; see GET /api/v1/crops")
    return crop.id


Lang = Literal["en", "sw", "fr"]
Grade = Literal["A", "B", "C"]


# ---------------------------------------------------------------- organisations


class OrgCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    kind: Literal["telco", "cooperative", "ngo", "agribusiness", "government", "buyer"] = "cooperative"
    country: str = Field(default="KE", min_length=2, max_length=2)
    currency: str = Field(default="KES", min_length=3, max_length=3)
    default_language: Lang = "en"
    brand_name: str | None = Field(default=None, max_length=60)
    sms_shortcode: str | None = None
    ussd_code: str | None = None
    contact_phone: str | None = None
    marketplace_enabled: bool = True


class OrgUpdate(BaseModel):
    name: str | None = None
    default_language: Lang | None = None
    brand_name: str | None = Field(default=None, max_length=60)
    sms_shortcode: str | None = None
    ussd_code: str | None = None
    contact_phone: str | None = None
    marketplace_enabled: bool | None = None


class OrgOut(ORM):
    id: int
    name: str
    kind: str
    country: str
    currency: str
    default_language: str
    brand_name: str | None
    sms_shortcode: str | None
    ussd_code: str | None
    contact_phone: str | None
    marketplace_enabled: bool
    created_at: datetime


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: Literal["admin", "officer", "viewer"] = "officer"


class ApiKeyOut(ORM):
    id: int
    name: str
    prefix: str
    role: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="Shown once. Store it securely.")


class OrgCreated(BaseModel):
    org: OrgOut
    api_key: str


# ---------------------------------------------------------------- farmers


class PlotIn(BaseModel):
    crop: str
    variety: str | None = None
    area_ha: float | None = Field(default=None, gt=0, le=1000)
    planting_date: date | None = None
    irrigated: bool = False
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)

    _crop = field_validator("crop")(_crop_id)


class PlotOut(ORM):
    id: int
    crop: str
    variety: str | None
    area_ha: float | None
    planting_date: date | None
    irrigated: bool
    stage: str | None = None


class FarmerCreate(BaseModel):
    phone: str
    name: str | None = Field(default=None, max_length=120)
    language: Lang | None = None
    gender: Literal["female", "male", "other"] | None = None
    region: str | None = None
    village: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    consent_marketplace: bool = False
    plots: list[PlotIn] = []
    send_welcome: bool = True


class FarmerUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    language: Lang | None = None
    gender: Literal["female", "male", "other"] | None = None
    region: str | None = None
    village: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    consent_marketplace: bool | None = None


class FarmerOut(ORM):
    id: int
    phone: str
    name: str | None
    language: str
    gender: str | None
    region: str | None
    village: str | None
    lat: float | None
    lon: float | None
    consent_marketplace: bool
    opted_out: bool
    created_at: datetime
    last_active_at: datetime | None
    plots: list[PlotOut] = []


class FarmerPage(BaseModel):
    total: int
    items: list[FarmerOut]


class ImportResult(BaseModel):
    created: int
    updated: int
    errors: list[str]


class BroadcastIn(BaseModel):
    message: str = Field(min_length=1, max_length=480)
    crop: str | None = None
    region: str | None = None
    language: Lang | None = None
    dry_run: bool = False


# ---------------------------------------------------------------- advice & diagnosis


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


class AdviceOut(BaseModel):
    answer: str
    engine: str
    id: int | None


class FeedbackIn(BaseModel):
    helpful: bool


class DiagnosisOut(ORM):
    id: int
    farmer_id: int
    crop: str | None
    channel: str
    source: str
    engine: str
    status: str
    pest_id: str | None
    label: str | None
    confidence: float
    severity: str | None
    result: dict
    symptoms_text: str | None
    reviewer_notes: str | None
    created_at: datetime
    has_image: bool = False


class DiagnoseResponse(BaseModel):
    message: str
    retake: bool
    diagnosis: DiagnosisOut | None


class SymptomsIn(BaseModel):
    symptoms: str = Field(min_length=3, max_length=1000)
    crop: str | None = None


class ReviewIn(BaseModel):
    status: Literal["confirmed", "rejected"]
    pest_id: str | None = None
    notes: str | None = Field(default=None, max_length=300)
    notify_farmer: bool = True


# ---------------------------------------------------------------- prices


class PriceIn(BaseModel):
    crop: str
    market: str = Field(min_length=1, max_length=120)
    price_per_kg: float = Field(gt=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    region: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    observed_on: date | None = None

    _crop = field_validator("crop")(_crop_id)


class PriceBatch(BaseModel):
    prices: list[PriceIn] = Field(min_length=1, max_length=5000)


class PriceAlertIn(BaseModel):
    crop: str
    direction: Literal["above", "below"] = "above"
    threshold_per_kg: float = Field(gt=0)
    market: str | None = None

    _crop = field_validator("crop")(_crop_id)


class PriceAlertOut(ORM):
    id: int
    crop: str
    direction: str
    threshold_per_kg: float
    market: str | None
    active: bool


# ---------------------------------------------------------------- marketplace


class ListingIn(BaseModel):
    crop: str
    quantity_kg: float = Field(gt=0, le=1_000_000)
    min_price_per_kg: float | None = Field(default=None, gt=0)
    grade: Grade = "B"
    available_from: date | None = None
    available_until: date | None = None

    _crop = field_validator("crop")(_crop_id)


class ListingOut(ORM):
    id: int
    crop: str
    quantity_kg: float
    remaining_kg: float
    grade: str
    min_price_per_kg: float | None
    currency: str
    available_from: date
    available_until: date | None
    status: str
    created_at: datetime


class BuyRequestIn(BaseModel):
    crop: str
    quantity_kg: float = Field(gt=0, le=10_000_000)
    price_per_kg: float = Field(gt=0)
    min_lot_kg: float = Field(default=0, ge=0)
    min_grade: Grade = "C"
    needed_by: date | None = None
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_km: float = Field(default=50, gt=0, le=1000)
    notes: str | None = Field(default=None, max_length=1000)

    _crop = field_validator("crop")(_crop_id)


class BuyRequestOut(ORM):
    id: int
    crop: str
    quantity_kg: float
    price_per_kg: float
    currency: str
    min_lot_kg: float
    min_grade: str
    needed_by: date | None
    lat: float
    lon: float
    radius_km: float
    notes: str | None
    status: str
    created_at: datetime
    committed_kg: float = 0
    accepted_kg: float = 0


class MatchOut(BaseModel):
    id: int
    code: str
    status: str
    crop: str
    quantity_kg: float
    price_per_kg: float
    currency: str
    distance_km: float | None
    score: float
    created_at: datetime
    # Farmer identity is revealed to the buyer only after the farmer accepts.
    farmer_name: str | None = None
    farmer_phone: str | None = None
    farmer_region: str | None = None
    buyer_name: str | None = None


class AlertOut(ORM):
    id: int
    kind: str
    severity: str
    title: str
    body: str
    data: dict
    read: bool
    created_at: datetime


# ---------------------------------------------------------------- farmer auth


class OtpRequest(BaseModel):
    phone: str
    country: str = Field(default="KE", min_length=2, max_length=2, description="For numbers typed in local format")


class OtpVerify(BaseModel):
    phone: str
    country: str = Field(default="KE", min_length=2, max_length=2)
    code: str = Field(min_length=6, max_length=6)


class TokenOut(BaseModel):
    token: str
    farmer: FarmerOut


class FarmerSelfUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    language: Lang | None = None
    region: str | None = None
    village: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    consent_marketplace: bool | None = None


assert set(SUPPORTED_LANGUAGES) == {"en", "sw", "fr"}
