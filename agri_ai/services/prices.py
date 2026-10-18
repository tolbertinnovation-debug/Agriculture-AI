"""Market prices: ingestion, lookup, trends and price alerts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import i18n
from ..knowledge import get_kb
from ..models import Farmer, MarketPrice, PriceAlertSubscription, utcnow
from .geo import haversine_km
from .notifications import create_alert

TREND_DAYS = 7
SIGNIFICANT_MOVE_PCT = 10.0
NEARBY_MARKET_KM = 60.0


@dataclass
class PriceQuote:
    crop: str
    market: str
    price_per_kg: float
    currency: str
    observed_on: date
    previous_price: float | None
    distance_km: float | None

    @property
    def change_pct(self) -> float | None:
        if not self.previous_price:
            return None
        return round((self.price_per_kg - self.previous_price) / self.previous_price * 100, 1)

    @property
    def trend(self) -> str:
        pct = self.change_pct
        if pct is None or abs(pct) < 3:
            return "stable"
        return "up" if pct > 0 else "down"

    def to_dict(self) -> dict:
        return {
            "crop": self.crop,
            "market": self.market,
            "price_per_kg": self.price_per_kg,
            "currency": self.currency,
            "observed_on": self.observed_on.isoformat(),
            "previous_price": self.previous_price,
            "change_pct": self.change_pct,
            "trend": self.trend,
            "distance_km": round(self.distance_km, 1) if self.distance_km is not None else None,
        }


def latest_quotes(
    db: Session,
    crop: str,
    *,
    lat: float | None = None,
    lon: float | None = None,
    country: str | None = None,
    max_age_days: int = 30,
    limit: int = 5,
) -> list[PriceQuote]:
    """Latest price per market for a crop, nearest markets first when a location is given."""
    since = date.today() - timedelta(days=max_age_days)
    q = select(MarketPrice).where(MarketPrice.crop == crop, MarketPrice.observed_on >= since)
    if country:
        q = q.where(MarketPrice.country == country)
    rows = db.scalars(q.order_by(MarketPrice.observed_on.desc(), MarketPrice.id.desc())).all()

    latest: dict[str, MarketPrice] = {}
    for r in rows:
        latest.setdefault(r.market, r)

    quotes = []
    for market, row in latest.items():
        prev = _price_around(rows, market, row.observed_on - timedelta(days=TREND_DAYS))
        dist = None
        if lat is not None and lon is not None and row.lat is not None and row.lon is not None:
            dist = haversine_km(lat, lon, row.lat, row.lon)
        quotes.append(PriceQuote(crop, market, row.price_per_kg, row.currency, row.observed_on, prev, dist))
    quotes.sort(key=lambda q: (q.distance_km if q.distance_km is not None else 1e9, -q.observed_on.toordinal()))
    return quotes[:limit]


def _price_around(rows: list[MarketPrice], market: str, target: date) -> float | None:
    """Price observed closest to (and not after) ``target`` - used as the trend baseline."""
    candidates = [r for r in rows if r.market == market and r.observed_on <= target]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (r.observed_on, r.id)).price_per_kg


def format_quotes_sms(quotes: list[PriceQuote], lang: str) -> str:
    kb = get_kb()
    lines = []
    for q in quotes[:3]:
        trend = i18n.word(q.trend, lang)
        if q.change_pct is not None and q.trend != "stable":
            trend = f"{trend} {abs(q.change_pct):g}%"
        crop_name = kb.crops[q.crop].name(lang) if q.crop in kb.crops else q.crop
        lines.append(
            i18n.t("price_line", lang, crop=crop_name, market=q.market, price=f"{q.price_per_kg:g}", currency=q.currency, trend=trend)
        )
    return "; ".join(lines)


def record_price(db: Session, **fields) -> MarketPrice:
    row = MarketPrice(**fields)
    db.add(row)
    return row


def market_summary(db: Session, country: str | None = None) -> list[dict]:
    """Average latest price per crop, for dashboards."""
    since = date.today() - timedelta(days=TREND_DAYS)
    q = select(MarketPrice.crop, MarketPrice.currency, func.avg(MarketPrice.price_per_kg), func.count()).where(
        MarketPrice.observed_on >= since
    )
    if country:
        q = q.where(MarketPrice.country == country)
    q = q.group_by(MarketPrice.crop, MarketPrice.currency)
    return [{"crop": c, "currency": cur, "avg_price_per_kg": round(avg, 2), "observations": n} for c, cur, avg, n in db.execute(q)]


# --------------------------------------------------------------------------- alerts


def run_price_alerts(db: Session, farmers: list[Farmer]) -> int:
    """Threshold subscriptions plus automatic alerts on big weekly moves for crops the farmer grows."""
    kb = get_kb()
    sent = 0
    week = date.today().isocalendar()
    for farmer in farmers:
        lang = farmer.language
        subs = db.scalars(
            select(PriceAlertSubscription).where(
                PriceAlertSubscription.farmer_id == farmer.id, PriceAlertSubscription.active.is_(True)
            )
        ).all()
        for sub in subs:
            quotes = latest_quotes(db, sub.crop, lat=farmer.lat, lon=farmer.lon, country=farmer.org.country)
            if sub.market:
                quotes = [q for q in quotes if q.market.lower() == sub.market.lower()]
            if not quotes:
                continue
            q = quotes[0]
            hit = q.price_per_kg >= sub.threshold_per_kg if sub.direction == "above" else q.price_per_kg <= sub.threshold_per_kg
            if not hit:
                continue
            body = i18n.t(
                "price_alert",
                lang,
                crop=kb.crops[sub.crop].name(lang) if sub.crop in kb.crops else sub.crop,
                market=q.market,
                price=f"{q.price_per_kg:g}",
                currency=q.currency,
                direction=i18n.word(sub.direction, lang),
                threshold=f"{sub.threshold_per_kg:g}",
            )
            alert = create_alert(
                db,
                farmer,
                kind="price",
                title=f"{sub.crop} price alert",
                body=body,
                dedupe_key=f"price:sub{sub.id}:{q.market}:{q.observed_on}",
                severity="info",
                data=q.to_dict(),
            )
            if alert:
                sub.last_triggered_at = utcnow()
                sent += 1

        for crop in farmer.crops:
            quotes = latest_quotes(db, crop, lat=farmer.lat, lon=farmer.lon, country=farmer.org.country, limit=1)
            if not quotes:
                continue
            q = quotes[0]
            if q.distance_km is not None and q.distance_km > NEARBY_MARKET_KM:
                continue
            if q.change_pct is None or abs(q.change_pct) < SIGNIFICANT_MOVE_PCT:
                continue
            body = i18n.t(
                "price_move",
                lang,
                crop=kb.crops[crop].name(lang) if crop in kb.crops else crop,
                market=q.market,
                move=i18n.word("up" if q.change_pct > 0 else "down", lang),
                pct=f"{abs(q.change_pct):g}",
                price=f"{q.price_per_kg:g}",
                currency=q.currency,
            )
            if create_alert(
                db,
                farmer,
                kind="price",
                title=f"{crop} price {'up' if q.change_pct > 0 else 'down'} {abs(q.change_pct):g}%",
                body=body,
                dedupe_key=f"price:move:{crop}:{q.market}:{week[0]}w{week[1]}",
                data=q.to_dict(),
            ):
                sent += 1
    return sent
