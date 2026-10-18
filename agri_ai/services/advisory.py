"""Crop advice: personalised answers to farmers' questions and stage-based tips."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import i18n
from ..config import get_settings
from ..knowledge import get_kb, normalize
from ..models import AdviceLog, Diagnosis, Farmer, Outbreak
from . import llm as llm_mod
from .geo import haversine_km
from .notifications import create_alert
from .prices import latest_quotes
from .weather import farmer_crop_contexts, farmer_location, get_forecast

log = logging.getLogger("agri_ai.advisory")

SMS_ANSWER_CHARS = 450  # three SMS segments
APP_ANSWER_CHARS = 1500


def ai_calls_today(db: Session, farmer: Farmer) -> int:
    start = datetime.combine(date.today(), datetime.min.time())
    advice = db.scalar(
        select(func.count()).select_from(AdviceLog).where(
            AdviceLog.farmer_id == farmer.id, AdviceLog.engine == "llm", AdviceLog.created_at >= start
        )
    )
    diagnoses = db.scalar(
        select(func.count()).select_from(Diagnosis).where(
            Diagnosis.farmer_id == farmer.id, Diagnosis.engine == "llm", Diagnosis.created_at >= start
        )
    )
    return int(advice or 0) + int(diagnoses or 0)


def ai_allowed(db: Session, farmer: Farmer) -> bool:
    return ai_calls_today(db, farmer) < get_settings().ai_daily_limit


def farmer_context(db: Session, farmer: Farmer, *, include_weather: bool = True) -> str:
    """Compact, factual description of the farm that grounds every model call."""
    kb = get_kb()
    lines = [f"Country: {farmer.org.country}. Region: {farmer.region or 'unknown'}. Village: {farmer.village or 'unknown'}."]
    loc = farmer_location(farmer)
    if loc:
        lines.append(f"Location: {loc[0]:.3f}, {loc[1]:.3f}.")
    ctxs = farmer_crop_contexts(farmer)
    if ctxs:
        for plot, c in zip([p for p in farmer.plots if p.crop in kb.crops], ctxs):
            stage = f"{c.stage_name}, day {c.days_since_planting}" if c.stage_name else "planting date unknown"
            area = f", {plot.area_ha} ha" if plot.area_ha else ""
            irrig = ", irrigated" if plot.irrigated else ", rain-fed"
            lines.append(f"Crop: {c.crop.id}{area}{irrig} ({stage}).")
    else:
        lines.append("Crops: not registered yet.")

    if include_weather and loc:
        fc = get_forecast(db, *loc, days=5)
        if fc:
            lines.append(
                "Forecast: "
                + "; ".join(f"{d.date.isoformat()} {d.tmin:.0f}-{d.tmax:.0f}C rain {d.rain_mm:.0f}mm wind {d.wind_kmh:.0f}km/h" for d in fc)
                + "."
            )

    for crop in farmer.crops[:3]:
        quotes = latest_quotes(db, crop, lat=loc[0] if loc else None, lon=loc[1] if loc else None, country=farmer.org.country, limit=2)
        for q in quotes:
            lines.append(f"Price: {crop} at {q.market} {q.price_per_kg:g} {q.currency}/kg on {q.observed_on} (trend {q.trend}).")

    recent = db.scalars(
        select(Diagnosis)
        .where(Diagnosis.farmer_id == farmer.id, Diagnosis.created_at >= datetime.now() - timedelta(days=30))
        .order_by(Diagnosis.created_at.desc())
        .limit(3)
    ).all()
    for d in recent:
        lines.append(f"Recent diagnosis: {d.label or d.pest_id} on {d.crop} ({d.created_at.date()}, status {d.status}).")

    if loc:
        for ob in db.scalars(select(Outbreak).where(Outbreak.active.is_(True))).all():
            if haversine_km(loc[0], loc[1], ob.lat, ob.lon) <= ob.radius_km * 2:
                lines.append(f"Active outbreak nearby: {ob.pest_id} ({ob.report_count} reports).")
    return "\n".join(lines)


@dataclass
class AdviceResult:
    answer: str
    engine: str  # llm | rules
    log_id: int | None = None


def answer_question(db: Session, farmer: Farmer, question: str, *, channel: str = "app") -> AdviceResult:
    question = question.strip()[:1000]
    max_chars = SMS_ANSWER_CHARS if channel in {"sms", "ussd"} else APP_ANSWER_CHARS
    lang = farmer.language
    answer: str | None = None
    engine = "rules"
    model = llm_mod.get_llm()
    if model is not None and ai_allowed(db, farmer):
        try:
            answer = model.answer(question, context=farmer_context(db, farmer), lang=lang, max_chars=max_chars)
            engine = "llm"
        except llm_mod.LLMUnavailable as exc:
            log.warning("advice LLM unavailable, using rules: %s", exc)
    if answer is None:
        answer = rule_based_answer(db, farmer, question)
    answer = fit_length(answer, max_chars)
    entry = AdviceLog(org_id=farmer.org_id, farmer_id=farmer.id, channel=channel, question=question, answer=answer, engine=engine)
    db.add(entry)
    db.flush()
    return AdviceResult(answer, engine, entry.id)


def fit_length(text: str, max_chars: int) -> str:
    text = re.sub(r"[*#_`]+", "", text).strip()  # SMS cannot render markdown
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > max_chars * 0.5 else cut[: max_chars - 1].rstrip() + "…"


_TOPICS = {
    "pest": ["pest", "insect", "worm", "disease", "spots", "holes", "wilting", "yellow", "dying", "rot", "mould", "wadudu", "ugonjwa", "ravageur", "maladie"],
    "fertilizer": ["fertiliser", "fertilizer", "manure", "dap", "can", "urea", "npk", "mbolea", "engrais", "compost"],
    "spacing": ["spacing", "space", "seed rate", "how many seeds", "nafasi", "espacement", "plant how"],
    "price": ["price", "sell", "market", "bei", "soko", "prix", "marche"],
    "weather": ["rain", "weather", "mvua", "hali", "pluie", "meteo", "dry", "drought"],
}


def _topic(question: str) -> str | None:
    norm = f" {normalize(question)} "
    for topic, words in _TOPICS.items():
        if any(f" {normalize(w)} " in norm for w in words):
            return topic
    return None


def rule_based_answer(db: Session, farmer: Farmer, question: str) -> str:
    """Deterministic answer from the knowledge base; used offline and as fallback."""
    kb = get_kb()
    lang = farmer.language
    crop = kb.resolve_crop(question)
    if crop is None and farmer.crops:
        crop = kb.crops.get(farmer.crops[0])
    topic = _topic(question)

    if topic == "pest":
        matches = kb.match_symptoms(question, crop.id if crop else None)
        if matches:
            pest, score = matches[0]
            return i18n.t(
                "diagnosis", lang, label=pest.name(lang), confidence=round(score * 100), actions="; ".join(pest.actions(3))
            ) + (" Report to your extension officer." if pest.notifiable else "")
    if crop and topic == "fertilizer":
        return f"{crop.name(lang).capitalize()}: {crop.fertilizer}"
    if crop and topic == "spacing":
        return f"{crop.name(lang).capitalize()}: {crop.spacing}"
    if topic == "price" and crop:
        loc = farmer_location(farmer)
        quotes = latest_quotes(db, crop.id, lat=loc[0] if loc else None, lon=loc[1] if loc else None, country=farmer.org.country)
        if quotes:
            from .prices import format_quotes_sms

            return format_quotes_sms(quotes, lang)
        return i18n.t("no_price", lang, crop=crop.name(lang))
    if topic == "weather":
        loc = farmer_location(farmer)
        if loc and (fc := get_forecast(db, *loc)):
            from .weather import format_forecast_sms

            return format_forecast_sms(fc, lang, farmer.village)

    # Default: what to do now for the farmer's crops.
    tips = stage_tips(farmer)
    if tips:
        return " ".join(tips[:2])
    if crop:
        return f"{crop.name(lang).capitalize()}: {crop.spacing}. {crop.fertilizer}"
    return i18n.t("advice_unavailable", lang)


def stage_tips(farmer: Farmer, today: date | None = None) -> list[str]:
    tips = []
    for plot in farmer.plots:
        crop = get_kb().crops.get(plot.crop)
        stage, _ = crop.stage_on(plot.planting_date, today) if crop else (None, None)
        if stage:
            tips.append(
                i18n.t("stage_tip", farmer.language, crop=crop.name(farmer.language).capitalize(), stage=stage.name, tasks="; ".join(stage.tasks))
            )
    return tips


def run_stage_tips(db: Session, farmers: list[Farmer], today: date | None = None) -> int:
    """Proactive tip when a plot enters a new growth stage (one SMS per plot per stage)."""
    sent = 0
    for farmer in farmers:
        for plot in farmer.plots:
            crop = get_kb().crops.get(plot.crop)
            if crop is None or plot.planting_date is None:
                continue
            stage, days = crop.stage_on(plot.planting_date, today)
            if stage is None or days is None:
                continue
            cycle = days // crop.season_days if crop.perennial else 0
            body = i18n.t(
                "stage_tip", farmer.language, crop=crop.name(farmer.language).capitalize(), stage=stage.name, tasks="; ".join(stage.tasks)
            )
            if create_alert(
                db,
                farmer,
                kind="advice",
                title=f"{crop.names['en'].capitalize()}: {stage.name}",
                body=body,
                dedupe_key=f"stage:{plot.id}:{cycle}:{stage.id}",
            ):
                sent += 1
    return sent
