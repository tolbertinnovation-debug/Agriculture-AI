"""Knowledge base, phone/SMS utilities and the weather rules engine."""

from datetime import date, timedelta

import pytest

from agri_ai.knowledge import get_kb
from agri_ai.services.geo import haversine_km, normalize_phone
from agri_ai.services.notifications import sms_segments
from agri_ai.services.weather import CropContext, DayForecast, evaluate_forecast


def test_crop_names_resolve_in_all_languages():
    kb = get_kb()
    assert kb.resolve_crop("MAHINDI").id == "maize"
    assert kb.resolve_crop("maïs").id == "maize"
    assert kb.resolve_crop("Maharagwe").id == "beans"
    assert kb.resolve_crop("pomme de terre").id == "potato"
    assert kb.resolve_crop("tomatoes").id == "tomato"
    assert kb.resolve_crop("spaceship") is None


def test_every_crop_pest_reference_exists():
    kb = get_kb()
    for crop in kb.crops.values():
        for pest in crop.pests:
            assert pest in kb.pests, (crop.id, pest)
    for pest in kb.pests.values():
        assert pest.actions(), pest.id
        for c in pest.crops:
            assert c in kb.crops


def test_growth_stage_and_perennial_cycle():
    kb = get_kb()
    maize = kb.crops["maize"]
    today = date(2026, 6, 1)
    stage, days = maize.stage_on(today - timedelta(days=60), today)
    assert stage.id == "tasseling_silking" and days == 60
    assert maize.stage_on(None)[0] is None
    coffee = kb.crops["coffee"]
    stage, _ = coffee.stage_on(today - timedelta(days=365 + 10), today)
    assert stage.id == "flowering"


def test_symptom_matcher_ranks_the_right_pest():
    kb = get_kb()
    top = kb.match_symptoms("small caterpillar making holes, sawdust in the whorl", "maize")[0][0]
    assert top.id == "fall_armyworm"
    top = kb.match_symptoms("orange powder rust on leaves", "coffee")[0][0]
    assert top.id == "coffee_leaf_rust"
    assert kb.match_symptoms("everything looks great", "maize") == []


@pytest.mark.parametrize(
    "raw,country,expected",
    [
        ("0712 345 678", "KE", "+254712345678"),
        ("254712345678", "KE", "+254712345678"),
        ("+254712345678", "NG", "+254712345678"),
        ("08031234567", "NG", "+2348031234567"),
        ("712345678", "KE", "+254712345678"),
    ],
)
def test_phone_normalisation(raw, country, expected):
    assert normalize_phone(raw, country) == expected


def test_phone_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_phone("hello")


def test_sms_segments_gsm_vs_unicode():
    assert sms_segments("a" * 160) == 1
    assert sms_segments("a" * 161) == 2
    assert sms_segments("é" * 100) == 1  # é is in GSM-7
    assert sms_segments("ï" * 71) == 2  # not GSM-7 -> UCS-2, 70 per segment


def test_haversine():
    assert abs(haversine_km(-1.286, 36.817, -0.303, 36.080) - 136) < 5  # Nairobi - Nakuru


# ------------------------------------------------------------------ weather rules


def _days(rows):
    today = date.today()
    return [DayForecast(today + timedelta(days=i), *r) for i, r in enumerate(rows)]


def _maize_at(day: int) -> CropContext:
    crop = get_kb().crops["maize"]
    stage, days = crop.stage_on(date.today() - timedelta(days=day))
    return CropContext(crop, stage.id, stage.name, days)


def test_heavy_rain_alert():
    fc = _days([(15, 25, 2, 20, 10), (15, 25, 62, 95, 20)] + [(15, 25, 3, 30, 10)] * 5)
    events = evaluate_forecast(fc, [_maize_at(30)])
    rain = [e for e in events if e.kind == "heavy_rain"]
    assert rain and rain[0].severity == "critical" and "62mm" in rain[0].message


def test_dry_spell_during_flowering_is_a_warning():
    fc = _days([(15, 29, 0, 5, 10)] * 7)
    events = evaluate_forecast(fc, [_maize_at(62)])
    dry = next(e for e in events if e.kind == "dry_spell")
    assert dry.severity == "warning" and "water-sensitive" in dry.message


def test_heat_and_frost_and_wind():
    fc = _days([(1, 37, 0, 5, 45)] + [(12, 25, 5, 50, 10)] * 6)
    kinds = {e.kind for e in evaluate_forecast(fc, [_maize_at(62)])}
    assert {"heat", "frost", "strong_wind"} <= kinds


def test_planting_rains_only_without_standing_crop():
    fc = _days([(15, 25, 12, 90, 10), (15, 25, 10, 90, 10), (15, 25, 8, 80, 10)] + [(15, 25, 2, 30, 10)] * 4)
    assert any(e.kind == "planting_rains" for e in evaluate_forecast(fc, []))
    assert not any(e.kind == "planting_rains" for e in evaluate_forecast(fc, [_maize_at(30)]))


def test_spray_window_only_when_needed():
    fc = _days([(15, 25, 0, 10, 8)] * 7)
    assert not any(e.kind == "spray_window" for e in evaluate_forecast(fc, [_maize_at(30)]))
    assert any(e.kind == "spray_window" for e in evaluate_forecast(fc, [_maize_at(30)], needs_spraying=True))


def test_weather_alerts_are_translated():
    fc = _days([(15, 25, 55, 95, 20)] + [(15, 25, 3, 30, 10)] * 6)
    msg = next(e for e in evaluate_forecast(fc, [_maize_at(30)], "sw") if e.kind == "heavy_rain").message
    assert msg.startswith("Mvua kubwa") and "mitaro" in msg


def test_production_refuses_default_secrets():
    from agri_ai.config import Settings

    with pytest.raises(RuntimeError, match="AGRI_ADMIN_TOKEN"):
        Settings(env="production", admin_token="dev-admin-token", dev_expose_otp=False).check_production()
    ok = Settings(env="production", admin_token="a" * 32, secret_key="b" * 32, webhook_secret="c" * 32, dev_expose_otp=False)
    ok.check_production()
    with pytest.raises(RuntimeError, match="OTP"):
        Settings(env="production", admin_token="a", secret_key="b", webhook_secret="c", dev_expose_otp=True).check_production()
