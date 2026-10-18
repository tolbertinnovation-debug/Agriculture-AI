from __future__ import annotations

import math
import re

EARTH_RADIUS_KM = 6371.0088

# Country dialling codes for the markets the platform launches in; extend as needed.
CALLING_CODES = {
    "KE": "254", "UG": "256", "TZ": "255", "RW": "250", "ET": "251", "NG": "234", "GH": "233",
    "CI": "225", "SN": "221", "ML": "223", "BF": "226", "ZM": "260", "MW": "265", "MZ": "258",
    "ZW": "263", "IN": "91", "BD": "880", "PK": "92",
}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def normalize_phone(raw: str, country: str = "KE") -> str:
    """Normalise a phone number to E.164 (``+254712345678``).

    Accepts local formats ("0712 345 678"), international without plus
    ("254712345678") and E.164. Raises ``ValueError`` for anything that cannot be
    a phone number.
    """
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("+"):
        number = digits[1:]
    elif digits.startswith("00"):
        number = digits[2:]
    else:
        code = CALLING_CODES.get(country.upper())
        if digits.startswith("0") and code:
            number = code + digits[1:]
        elif code and not digits.startswith(code) and len(digits) <= 10:
            number = code + digits
        else:
            number = digits
    if not number.isdigit() or not 8 <= len(number) <= 15:
        raise ValueError(f"invalid phone number: {raw!r}")
    return "+" + number


def mask_phone(phone: str) -> str:
    return phone[:4] + "*" * max(0, len(phone) - 7) + phone[-3:] if len(phone) > 7 else "***"
