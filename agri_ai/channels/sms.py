"""SMS / WhatsApp text command interpreter.

Keywords work in English, Swahili and French. Anything that is not a command
but looks like a sentence is treated as a question for the advisor - farmers
rarely remember keywords, and "my maize leaves have holes" should just work.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import i18n
from ..knowledge import get_kb, normalize
from ..models import Farmer, PriceAlertSubscription
from ..services import advisory, diagnosis, marketplace
from ..services.farmers import BAG_KG, create_listing
from ..services.prices import format_quotes_sms, latest_quotes
from ..services.weather import farmer_location, format_forecast_sms, get_forecast

COMMANDS = {
    "help": {"help", "msaada", "aide", "menu", "info", "?"},
    "weather": {"weather", "hali", "hewa", "meteo", "temps"},
    "price": {"price", "prices", "bei", "prix"},
    "sell": {"sell", "uza", "vendre", "vends"},
    "ask": {"ask", "uliza", "swali", "question", "q"},
    "pest": {"pest", "pests", "disease", "wadudu", "ugonjwa", "ravageur", "maladie"},
    "yes": {"yes", "y", "ndiyo", "ndio", "oui", "accept"},
    "no": {"no", "n", "hapana", "non", "decline"},
    "stop": {"stop", "acha", "unsubscribe", "arret", "stopall"},
    "start": {"start", "anza", "unstop", "reprendre"},
    "alert": {"alert", "arifu", "alerte"},
    "lang": {"lang", "language", "lugha", "langue"},
    "join": {"join", "register", "jiunge", "jisajili", "inscrire"},
}
_KEYWORD = {kw: cmd for cmd, kws in COMMANDS.items() for kw in kws}
_NUMBER = re.compile(r"^(\d+(?:[.,]\d+)?)(kg|kgs|bags?|gunia|magunia|sacs?)?$", re.I)
_BAG_WORDS = {"bag", "bags", "gunia", "magunia", "sac", "sacs"}


def crop_examples(lang: str) -> str:
    return ", ".join(c.name(lang) for c in list(get_kb().crops.values())[:4])


def handle_text(db: Session, farmer: Farmer, text: str, *, channel: str = "sms") -> str:
    """Interpret one inbound message and return the reply text."""
    lang = farmer.language
    brand = farmer.org.display_name
    words = text.strip().split()
    if not words:
        return i18n.t("help", lang, brand=brand)
    cmd = _KEYWORD.get(normalize(words[0]))
    args = words[1:]

    if cmd == "help":
        return i18n.t("help", lang, brand=brand)
    if cmd == "stop":
        farmer.opted_out = True
        return i18n.t("stopped", lang, brand=brand)
    if cmd == "start":
        farmer.opted_out = False
        return i18n.t("started", lang, brand=brand)
    if cmd == "lang":
        code = normalize(args[0])[:2] if args else ""
        code = {"ki": "sw", "sw": "sw", "fr": "fr", "en": "en"}.get(code, "")
        if code:
            farmer.language = code
            return i18n.t("help", code, brand=brand)
        return "LANG EN | LANG SW | LANG FR"
    if cmd == "join":
        return _join(db, farmer, args)
    if cmd == "weather":
        return _weather(db, farmer)
    if cmd == "price":
        return _price(db, farmer, " ".join(args))
    if cmd == "sell":
        return _sell(db, farmer, args)
    if cmd in {"yes", "no"}:
        if not args:
            return i18n.t("help", lang, brand=brand)
        _, reply = marketplace.farmer_respond(db, farmer, args[0], accept=cmd == "yes")
        return reply
    if cmd == "alert":
        return _alert(db, farmer, args)
    if cmd == "pest":
        if not args:
            return i18n.t("help", lang, brand=brand)
        crop = get_kb().resolve_crop(args[0])
        symptoms = " ".join(args[1:] if crop else args)
        return diagnosis.diagnose_text(db, farmer, symptoms, crop=crop.id if crop else None, channel=channel).message
    if cmd == "ask":
        if not args:
            return i18n.t("help", lang, brand=brand)
        return advisory.answer_question(db, farmer, " ".join(args), channel=channel).answer

    # Free text: answer it if it reads like a question, otherwise show help.
    if len(words) >= 3:
        return advisory.answer_question(db, farmer, text, channel=channel).answer
    return i18n.t("unknown", lang)


def _join(db: Session, farmer: Farmer, args: list[str]) -> str:
    from ..services.farmers import add_plot

    kb = get_kb()
    name_parts, crops = [], []
    for a in args:
        crop = kb.resolve_crop(a)
        (crops if crop else name_parts).append(crop.id if crop else a)
    if name_parts:
        farmer.name = " ".join(name_parts)[:120]
    for c in crops:
        if c not in farmer.crops:
            add_plot(db, farmer, c)
    return i18n.t("welcome", farmer.language, brand=farmer.org.display_name)


def _weather(db: Session, farmer: Farmer) -> str:
    loc = farmer_location(farmer)
    if loc is None:
        return i18n.t("need_location", farmer.language)
    fc = get_forecast(db, *loc)
    if not fc:
        return i18n.t("weather_unavailable", farmer.language)
    return format_forecast_sms(fc, farmer.language, farmer.village)


def _price(db: Session, farmer: Farmer, crop_text: str) -> str:
    lang = farmer.language
    kb = get_kb()
    crops = [kb.resolve_crop(crop_text)] if crop_text else [kb.crops[c] for c in farmer.crops[:2]]
    crops = [c for c in crops if c]
    if not crops:
        return i18n.t("unknown_crop", lang, crop=crop_text or "?", examples=crop_examples(lang))
    loc = farmer_location(farmer)
    parts = []
    for crop in crops:
        quotes = latest_quotes(db, crop.id, lat=loc[0] if loc else None, lon=loc[1] if loc else None, country=farmer.org.country, limit=2)
        parts.append(format_quotes_sms(quotes, lang) if quotes else i18n.t("no_price", lang, crop=crop.name(lang)))
    return " | ".join(parts)


def _parse_quantity(args: list[str], crop_id: str) -> tuple[float | None, float | None]:
    """Extract (quantity_kg, price_per_kg) from tokens like ``500kg 35`` or ``5 bags 40``."""
    numbers: list[tuple[float, bool]] = []
    for i, a in enumerate(args):
        m = _NUMBER.match(a)
        if not m:
            continue
        value = float(m.group(1).replace(",", "."))
        unit = (m.group(2) or "").lower()
        nxt = args[i + 1].lower() if i + 1 < len(args) else ""
        is_bags = unit in _BAG_WORDS or unit.startswith("bag") or nxt in _BAG_WORDS
        numbers.append((value * BAG_KG.get(crop_id, 50) if is_bags else value, is_bags))
    qty = numbers[0][0] if numbers else None
    price = numbers[1][0] if len(numbers) > 1 else None
    return qty, price


def _sell(db: Session, farmer: Farmer, args: list[str]) -> str:
    lang = farmer.language
    kb = get_kb()
    words = [a for a in args if not _NUMBER.match(a) and a.lower() not in _BAG_WORDS]
    crop = kb.resolve_crop(" ".join(words)) if words else (kb.crops.get(farmer.crops[0]) if len(farmer.crops) == 1 else None)
    if crop is None:
        return i18n.t("unknown_crop", lang, crop=" ".join(words) or "?", examples=crop_examples(lang))
    qty, price = _parse_quantity(args, crop.id)
    if not qty:
        return i18n.t("help", lang, brand=farmer.org.display_name)
    if not farmer.consent_marketplace:
        # Listing produce is an explicit opt-in to share the offer with buyers.
        farmer.consent_marketplace = True
    listing = create_listing(db, farmer, crop.id, qty, min_price_per_kg=price)
    marketplace.match_for_new_listing(db, listing)
    price_txt = f" @ {price:g} {listing.currency}/kg" if price else ""
    return i18n.t("listing_created", lang, qty=f"{qty:g}", crop=crop.name(lang), price=price_txt, id=listing.id)


def _alert(db: Session, farmer: Farmer, args: list[str]) -> str:
    lang = farmer.language
    kb = get_kb()
    direction = "above"
    crop, threshold = None, None
    for a in args:
        n = normalize(a)
        if n in {"above", "over", "juu", "plus", "dessus"}:
            direction = "above"
        elif n in {"below", "under", "chini", "moins", "dessous"}:
            direction = "below"
        elif _NUMBER.match(a):
            threshold = float(_NUMBER.match(a).group(1).replace(",", "."))
        elif crop is None:
            crop = kb.resolve_crop(a)
    if crop is None or threshold is None:
        return "ALERT <crop> ABOVE|BELOW <price>"
    existing = db.scalar(
        select(PriceAlertSubscription).where(
            PriceAlertSubscription.farmer_id == farmer.id,
            PriceAlertSubscription.crop == crop.id,
            PriceAlertSubscription.direction == direction,
        )
    )
    if existing:
        existing.threshold_per_kg, existing.active = threshold, True
    else:
        db.add(PriceAlertSubscription(farmer_id=farmer.id, crop=crop.id, direction=direction, threshold_per_kg=threshold))
    return i18n.t(
        "subscribed", lang, crop=crop.name(lang), direction=i18n.word(direction, lang), threshold=f"{threshold:g}", currency=farmer.org.currency
    )
