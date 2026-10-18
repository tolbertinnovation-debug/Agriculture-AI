"""Weather forecasts and crop-aware weather alerts."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import i18n
from ..config import get_settings
from ..knowledge import Crop, get_kb
from ..models import Farmer, Plot, WeatherCache, utcnow

log = logging.getLogger("agri_ai.weather")


@dataclass
class DayForecast:
    date: date
    tmin: float
    tmax: float
    rain_mm: float
    rain_prob: float  # 0-100
    wind_kmh: float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> DayForecast:
        return cls(
            date=date.fromisoformat(d["date"]),
            tmin=d["tmin"],
            tmax=d["tmax"],
            rain_mm=d["rain_mm"],
            rain_prob=d["rain_prob"],
            wind_kmh=d["wind_kmh"],
        )


class WeatherProvider(Protocol):
    name: str

    def forecast(self, lat: float, lon: float, days: int = 7) -> list[DayForecast]: ...


class OpenMeteoProvider:
    """Free forecast API, no key required (https://open-meteo.com)."""

    name = "open-meteo"
    url = "https://api.open-meteo.com/v1/forecast"

    def forecast(self, lat: float, lon: float, days: int = 7) -> list[DayForecast]:
        params = {
            "latitude": round(lat, 3),
            "longitude": round(lon, 3),
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max",
            "timezone": "auto",
            "forecast_days": days,
        }
        resp = httpx.get(self.url, params=params, timeout=15)
        resp.raise_for_status()
        daily = resp.json()["daily"]
        out = []
        for i, day in enumerate(daily["time"]):
            out.append(
                DayForecast(
                    date=date.fromisoformat(day),
                    tmin=_num(daily["temperature_2m_min"][i]),
                    tmax=_num(daily["temperature_2m_max"][i]),
                    rain_mm=_num(daily["precipitation_sum"][i]),
                    rain_prob=_num(daily["precipitation_probability_max"][i]),
                    wind_kmh=_num(daily["wind_speed_10m_max"][i]),
                )
            )
        return out


class StaticProvider:
    """Deterministic synthetic forecast for offline development and demos."""

    name = "static"

    def forecast(self, lat: float, lon: float, days: int = 7) -> list[DayForecast]:
        today = date.today()
        out = []
        for i in range(days):
            d = today + timedelta(days=i)
            h = int(hashlib.sha256(f"{lat:.1f}{lon:.1f}{d}".encode()).hexdigest(), 16)
            rain = (h % 40) if h % 3 == 0 else (h % 4)
            out.append(DayForecast(d, 14 + h % 5, 25 + h % 8, float(rain), float(h % 100), float(5 + h % 25)))
        return out


def _num(v) -> float:
    return float(v) if v is not None else 0.0


_provider: WeatherProvider | None = None


def get_provider() -> WeatherProvider:
    global _provider
    if _provider is None:
        _provider = StaticProvider() if get_settings().weather_provider == "static" else OpenMeteoProvider()
    return _provider


def set_provider(provider: WeatherProvider | None) -> None:
    global _provider
    _provider = provider


def get_forecast(db: Session, lat: float, lon: float, days: int = 7) -> list[DayForecast] | None:
    """Forecast for a location, cached per ~11 km grid cell.

    Thousands of farmers in the same area share one upstream call. Returns
    ``None`` when the provider is unreachable and there is no cached copy.
    """
    provider = get_provider()
    key = f"{provider.name}:{lat:.1f}:{lon:.1f}"
    cached = db.get(WeatherCache, key)
    max_age = timedelta(minutes=get_settings().weather_cache_minutes)
    if cached and utcnow() - cached.fetched_at < max_age:
        return [DayForecast.from_dict(d) for d in cached.payload["days"]][:days]
    try:
        days_fc = provider.forecast(round(lat, 1), round(lon, 1), max(days, 7))
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("weather provider failed for %s: %s", key, exc)
        if cached:  # stale data beats no data for a farmer deciding whether to spray
            return [DayForecast.from_dict(d) for d in cached.payload["days"]][:days]
        return None
    payload = {"days": [d.to_dict() for d in days_fc]}
    if cached:
        cached.payload, cached.fetched_at = payload, utcnow()
    else:
        try:
            with db.begin_nested():  # another request may have cached this cell concurrently
                db.add(WeatherCache(key=key, payload=payload, fetched_at=utcnow()))
        except IntegrityError:
            pass
    db.flush()
    return days_fc[:days]


def format_forecast_sms(days: list[DayForecast], lang: str, place: str | None = None, n: int = 3) -> str:
    today = date.today()
    parts = [i18n.t("weather_header", lang, place=place or "").replace("  ", " ")]
    for d in days[:n]:
        label = i18n.day_label((d.date - today).days, d.date.weekday(), lang)
        parts.append(i18n.t("weather_day", lang, day=label, tmin=round(d.tmin), tmax=round(d.tmax), rain=round(d.rain_mm)))
    return " ".join(parts[:1]) + " " + "; ".join(parts[1:])


# --------------------------------------------------------------------------- alerts

HEAVY_RAIN_MM = 30.0
VERY_HEAVY_RAIN_MM = 50.0
FROST_C = 2.0
STRONG_WIND_KMH = 40.0
DRY_SPELL_TOTAL_MM = 5.0


@dataclass
class WeatherEvent:
    kind: str  # heavy_rain | dry_spell | heat | frost | strong_wind | spray_window | planting_rains
    day: date
    severity: str
    message: str


@dataclass
class CropContext:
    crop: Crop
    stage_id: str | None
    stage_name: str | None
    days_since_planting: int | None

    @classmethod
    def from_plot(cls, plot: Plot, today: date | None = None) -> CropContext | None:
        crop = get_kb().crops.get(plot.crop)
        if crop is None:
            return None
        stage, days = crop.stage_on(plot.planting_date, today)
        return cls(crop, stage.id if stage else None, stage.name if stage else None, days)

    @property
    def in_ground(self) -> bool:
        if self.crop.perennial:
            return True
        if self.days_since_planting is None:
            return True  # unknown planting date: assume the crop is standing
        return 0 <= self.days_since_planting <= self.crop.stages[-1].end


def evaluate_forecast(
    days: list[DayForecast],
    crops: list[CropContext],
    lang: str = "en",
    *,
    needs_spraying: bool = False,
    today: date | None = None,
) -> list[WeatherEvent]:
    """Turn a forecast into the few alerts that actually matter for this farm."""
    today = today or date.today()
    events: list[WeatherEvent] = []
    standing = [c for c in crops if c.in_ground]
    name = lambda c: c.crop.name(lang)  # noqa: E731

    def label(d: DayForecast) -> str:
        return i18n.day_label((d.date - today).days, d.date.weekday(), lang)

    near = days[:3]

    # Heavy rain: the first day in the next 3 above threshold.
    for d in near:
        if d.rain_mm >= HEAVY_RAIN_MM:
            harvest_ready = [c for c in standing if c.stage_id in {"maturity", "harvest"}]
            if harvest_ready:
                advice = i18n.t("adv_harvest_before_rain", lang, crops=", ".join(name(c) for c in harvest_ready))
            else:
                advice = i18n.t("adv_drainage", lang)
            events.append(
                WeatherEvent(
                    "heavy_rain",
                    d.date,
                    "critical" if d.rain_mm >= VERY_HEAVY_RAIN_MM else "warning",
                    i18n.t("wx_heavy_rain", lang, day=label(d), rain=round(d.rain_mm), advice=advice),
                )
            )
            break

    # Dry spell over the full forecast window, prioritised for crops in a water-critical stage.
    window = days[:7]
    if standing and len(window) >= 5 and sum(d.rain_mm for d in window) < DRY_SPELL_TOTAL_MM:
        critical = [c for c in standing if c.stage_id in c.crop.critical_water_stages]
        if critical:
            c = critical[0]
            advice = i18n.t("adv_critical_water", lang, crop=name(c), stage=(c.stage_name or "").lower())
            severity = "warning"
        else:
            advice = i18n.t("adv_conserve_moisture", lang)
            severity = "info"
        events.append(
            WeatherEvent("dry_spell", window[0].date, severity, i18n.t("wx_dry_spell", lang, days=len(window), advice=advice))
        )

    # Heat stress against each crop's own threshold.
    for d in near:
        stressed = [c for c in standing if d.tmax >= c.crop.heat_stress_c]
        if stressed:
            flowering = [c for c in stressed if c.stage_id in c.crop.critical_water_stages]
            advice = i18n.t("adv_heat_flowering", lang, crop=name(flowering[0])) if flowering else i18n.t("adv_heat", lang)
            events.append(
                WeatherEvent(
                    "heat", d.date, "warning", i18n.t("wx_heat", lang, day=label(d), tmax=round(d.tmax), advice=advice)
                )
            )
            break

    # Frost (highland areas).
    for d in near:
        if d.tmin <= FROST_C and any(c.crop.frost_sensitive for c in standing):
            advice = i18n.t("adv_frost", lang)
            events.append(
                WeatherEvent("frost", d.date, "critical", i18n.t("wx_frost", lang, day=label(d), tmin=round(d.tmin), advice=advice))
            )
            break

    for d in near:
        if d.wind_kmh >= STRONG_WIND_KMH:
            tall = [c for c in standing if c.crop.id in {"banana", "maize", "tomato", "sorghum"}]
            advice = i18n.t("adv_wind_tall" if tall else "adv_wind", lang)
            events.append(
                WeatherEvent(
                    "strong_wind", d.date, "warning", i18n.t("wx_strong_wind", lang, day=label(d), wind=round(d.wind_kmh), advice=advice)
                )
            )
            break

    # Spray window only when the farmer has something to spray (recent diagnosis).
    if needs_spraying:
        for i, d in enumerate(near):
            nxt = days[i + 1] if i + 1 < len(days) else None
            if d.rain_mm < 1 and d.rain_prob < 40 and d.wind_kmh < 15 and (nxt is None or nxt.rain_mm < 5):
                advice = i18n.t("adv_spray", lang)
                events.append(WeatherEvent("spray_window", d.date, "info", i18n.t("wx_spray_window", lang, day=label(d), advice=advice)))
                break

    # Planting rains for farmers with nothing currently in the ground.
    if not standing:
        total = sum(d.rain_mm for d in near)
        if total >= 20 and all(d.rain_mm >= 3 for d in near[:2]):
            events.append(
                WeatherEvent("planting_rains", near[0].date, "info", i18n.t("wx_planting_rains", lang, rain=round(total), day=label(near[0])))
            )
    return events


def farmer_crop_contexts(farmer: Farmer, today: date | None = None) -> list[CropContext]:
    return [c for p in farmer.plots if (c := CropContext.from_plot(p, today)) is not None]


def farmer_location(farmer: Farmer) -> tuple[float, float] | None:
    if farmer.lat is not None and farmer.lon is not None:
        return farmer.lat, farmer.lon
    for p in farmer.plots:
        if p.lat is not None and p.lon is not None:
            return p.lat, p.lon
    return None


def stamp(dt: datetime | date) -> str:
    return dt.isoformat()[:10]
