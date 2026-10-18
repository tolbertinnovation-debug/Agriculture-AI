"""USSD menu for feature phones (Africa's Talking-style protocol).

The gateway sends the whole input path each step (``text="3*2*500"``), so the
menu is a pure function of that path: no session storage needed, and a dropped
session can simply be redialled. Replies start with ``CON`` (expect input) or
``END`` (close). Screens are kept under ~160 characters for small handsets.
Slow work (AI questions) is answered by SMS after the session closes.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import i18n
from ..knowledge import get_kb
from ..models import Farmer, Listing, Match, MatchStatus
from ..services import diagnosis, marketplace
from ..services.farmers import create_listing
from .sms import _price, _weather

MENU = {
    "main": {
        "en": "{brand}\n1. Weather\n2. Market prices\n3. Sell produce\n4. Pest/disease help\n5. Ask an expert\n6. My offers\n7. Language",
        "sw": "{brand}\n1. Hali ya hewa\n2. Bei za soko\n3. Uza mazao\n4. Wadudu/magonjwa\n5. Uliza mtaalamu\n6. Ofa zangu\n7. Lugha",
        "fr": "{brand}\n1. Météo\n2. Prix du marché\n3. Vendre\n4. Ravageurs/maladies\n5. Poser une question\n6. Mes offres\n7. Langue",
    },
    "choose_crop": {"en": "Choose crop:", "sw": "Chagua zao:", "fr": "Choisissez la culture :"},
    "enter_kg": {"en": "Quantity in kg:", "sw": "Kiasi kwa kilo:", "fr": "Quantité en kg :"},
    "enter_price": {
        "en": "Lowest price per kg you accept (0 = any):",
        "sw": "Bei ya chini kwa kilo unayokubali (0 = yoyote):",
        "fr": "Prix minimum par kg accepté (0 = tout prix) :",
    },
    "describe": {
        "en": "Describe what you see (e.g. holes in leaves, yellow spots):",
        "sw": "Eleza unachoona (mfano: matundu kwenye majani, madoa ya njano):",
        "fr": "Décrivez ce que vous voyez (ex. trous dans les feuilles, taches jaunes) :",
    },
    "question": {"en": "Type your question:", "sw": "Andika swali lako:", "fr": "Écrivez votre question :"},
    "question_ack": {
        "en": "Thank you. The answer will come by SMS shortly.",
        "sw": "Asante. Jibu litakuja kwa SMS hivi punde.",
        "fr": "Merci. La réponse arrivera par SMS sous peu.",
    },
    "no_offers": {"en": "You have no open offers.", "sw": "Huna ofa zilizo wazi.", "fr": "Vous n'avez aucune offre ouverte."},
    "offer_action": {"en": "1. Accept\n2. Decline", "sw": "1. Kubali\n2. Kataa", "fr": "1. Accepter\n2. Refuser"},
    "invalid": {"en": "Invalid choice.", "sw": "Chaguo si sahihi.", "fr": "Choix invalide."},
    "languages": {"en": "1. English\n2. Kiswahili\n3. Français", "sw": "1. English\n2. Kiswahili\n3. Français", "fr": "1. English\n2. Kiswahili\n3. Français"},
    "lang_set": {"en": "Language set to English.", "sw": "Lugha imewekwa Kiswahili.", "fr": "Langue réglée sur le français."},
}


def m(key: str, lang: str, **kw) -> str:
    return MENU[key].get(lang, MENU[key]["en"]).format(**kw)


def con(text: str) -> str:
    return "CON " + text


def end(text: str) -> str:
    return "END " + text


def _crop_options(farmer: Farmer) -> list[str]:
    """Farmer's own crops first, then the rest - at most 8 to fit one screen."""
    kb = get_kb()
    ids = list(dict.fromkeys([*farmer.crops, *kb.crops.keys()]))
    return ids[:8]


def _crop_menu(farmer: Farmer) -> str:
    kb = get_kb()
    lines = [m("choose_crop", farmer.language)]
    lines += [f"{i}. {kb.crops[c].name(farmer.language).capitalize()}" for i, c in enumerate(_crop_options(farmer), 1)]
    return "\n".join(lines)


def _pick_crop(farmer: Farmer, choice: str) -> str | None:
    opts = _crop_options(farmer)
    if choice.isdigit() and 1 <= int(choice) <= len(opts):
        return opts[int(choice) - 1]
    return None


def _number(value: str) -> float | None:
    try:
        v = float(value.replace(",", "."))
    except ValueError:
        return None
    return v if v >= 0 else None


def _open_offers(db: Session, farmer: Farmer) -> list[Match]:
    return list(
        db.scalars(
            select(Match)
            .join(Listing, Listing.id == Match.listing_id)
            .where(Listing.farmer_id == farmer.id, Match.status == MatchStatus.proposed.value)
            .order_by(Match.created_at)
            .limit(5)
        )
    )


def handle_ussd(db: Session, farmer: Farmer, text: str, *, defer: Callable[[Farmer, str], None]) -> str:
    """Return the USSD screen for input path ``text``.

    ``defer(farmer, question)`` schedules a slow AI answer to be sent by SMS.
    """
    lang = farmer.language
    steps = [s.strip() for s in text.split("*")] if text else []
    if not steps:
        return con(m("main", lang, brand=farmer.org.display_name))
    top, rest = steps[0], steps[1:]

    if top == "1":
        return end(_weather(db, farmer))

    if top == "2":
        if not rest:
            return con(_crop_menu(farmer))
        crop = _pick_crop(farmer, rest[0])
        return end(_price(db, farmer, crop) if crop else m("invalid", lang))

    if top == "3":
        if not rest:
            return con(_crop_menu(farmer))
        crop = _pick_crop(farmer, rest[0])
        if crop is None:
            return end(m("invalid", lang))
        if len(rest) == 1:
            return con(m("enter_kg", lang))
        qty = _number(rest[1])
        if not qty:
            return end(m("invalid", lang))
        if len(rest) == 2:
            return con(m("enter_price", lang))
        price = _number(rest[2])
        if price is None:
            return end(m("invalid", lang))
        farmer.consent_marketplace = True
        listing = create_listing(db, farmer, crop, qty, min_price_per_kg=price or None)
        marketplace.match_for_new_listing(db, listing)
        price_txt = f" @ {price:g} {listing.currency}/kg" if price else ""
        return end(i18n.t("listing_created", lang, qty=f"{qty:g}", crop=get_kb().crops[crop].name(lang), price=price_txt, id=listing.id))

    if top == "4":
        if not rest:
            return con(_crop_menu(farmer))
        crop = _pick_crop(farmer, rest[0])
        if crop is None:
            return end(m("invalid", lang))
        if len(rest) == 1:
            return con(m("describe", lang))
        outcome = diagnosis.diagnose_text(db, farmer, "*".join(rest[1:]), crop=crop, channel="ussd")
        return end(outcome.message[:300])

    if top == "5":
        if not rest:
            return con(m("question", lang))
        defer(farmer, "*".join(rest))
        return end(m("question_ack", lang))

    if top == "6":
        offers = _open_offers(db, farmer)
        if not offers:
            return end(m("no_offers", lang))
        kb = get_kb()
        if not rest:
            lines = [
                f"{i}. {o.quantity_kg:g}kg {kb.crops[o.listing.crop].name(lang)} @{o.price_per_kg:g} ({o.buy_request.buyer.display_name})"
                for i, o in enumerate(offers, 1)
            ]
            return con("\n".join(lines))
        idx = int(rest[0]) - 1 if rest[0].isdigit() else -1
        if not 0 <= idx < len(offers):
            return end(m("invalid", lang))
        if len(rest) == 1:
            return con(m("offer_action", lang))
        if rest[1] not in {"1", "2"}:
            return end(m("invalid", lang))
        _, reply = marketplace.farmer_respond(db, farmer, offers[idx].code, accept=rest[1] == "1")
        return end(reply)

    if top == "7":
        if not rest:
            return con(m("languages", lang))
        code = {"1": "en", "2": "sw", "3": "fr"}.get(rest[0])
        if code is None:
            return end(m("invalid", lang))
        farmer.language = code
        return end(m("lang_set", code))

    return end(m("invalid", lang))
