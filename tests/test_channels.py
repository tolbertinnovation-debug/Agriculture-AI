"""SMS, USSD and WhatsApp webhooks."""

from sqlalchemy import select

from agri_ai.models import Farmer, Listing, PriceAlertSubscription
from conftest import HOOK, make_farmer, make_org


def sms(client, text, phone="+254712345678", to="22384"):
    r = client.post("/webhooks/sms", data={"from": phone, "to": to, "text": text}, headers=HOOK)
    assert r.status_code == 200, r.text
    return r


def ussd(client, text, phone="+254712345678", code="*384*1#"):
    r = client.post("/webhooks/ussd", data={"sessionId": "s1", "serviceCode": code, "phoneNumber": phone, "text": text}, headers=HOOK)
    assert r.status_code == 200, r.text
    return r.text


def test_webhook_secret_required(client):
    r = client.post("/webhooks/sms", data={"from": "+254712345678", "text": "HELP"})
    assert r.status_code == 401
    r = client.post("/webhooks/sms?secret=test-hook", data={"from": "+254712345678", "to": "22384", "text": "HELP"})
    assert r.status_code == 200


def test_unknown_number_self_registers_with_shortcode_org(client, outbox, db):
    make_org(client)
    sms(client, "JOIN Juma Otieno maize beans", phone="0798000111")
    farmer = db.scalar(select(Farmer).where(Farmer.phone == "+254798000111"))
    assert farmer is not None and farmer.name == "Juma Otieno"
    assert sorted(farmer.crops) == ["beans", "maize"]
    assert all("Welcome" in body for _, body in outbox[-2:])


def test_help_in_swahili(client, outbox):
    _, h = make_org(client)
    make_farmer(client, h, language="sw")
    sms(client, "msaada")
    assert outbox[-1][1].startswith("Kilimo Co-op: HALI")


def test_weather_and_price_commands(client, outbox):
    _, h = make_org(client)
    make_farmer(client, h)
    client.post(
        "/api/v1/partner/prices",
        json={"prices": [{"crop": "maize", "market": "Nakuru", "price_per_kg": 40, "lat": -0.28, "lon": 36.07}]},
        headers=h,
    )
    sms(client, "WEATHER")
    assert outbox[-1][1].startswith("Weather Njoro:") and "today" in outbox[-1][1]
    sms(client, "BEI mahindi")
    assert "Nakuru: 40 KES/kg" in outbox[-1][1]
    sms(client, "PRICE unicorn")
    assert "Unknown crop" in outbox[-1][1]


def test_sell_by_sms_parses_bags_and_price(client, outbox, db):
    _, h = make_org(client)
    make_farmer(client, h, consent_marketplace=False)
    sms(client, "UZA mahindi 5 bags 38")
    listing = db.scalar(select(Listing))
    assert listing.crop == "maize" and listing.quantity_kg == 450 and listing.min_price_per_kg == 38
    assert listing.farmer.consent_marketplace is True  # listing is an explicit opt-in
    assert f"L{listing.id}" in outbox[-1][1]


def test_price_alert_subscription(client, outbox, db):
    _, h = make_org(client)
    make_farmer(client, h)
    sms(client, "ALERT maize above 45")
    sub = db.scalar(select(PriceAlertSubscription))
    assert (sub.crop, sub.direction, sub.threshold_per_kg) == ("maize", "above", 45)
    assert "45 KES/kg" in outbox[-1][1]


def test_stop_and_start(client, outbox, db):
    _, h = make_org(client)
    make_farmer(client, h)
    sms(client, "STOP")
    assert db.scalar(select(Farmer)).opted_out is True
    assert "no longer receive" in outbox[-1][1]
    sms(client, "START")
    db.expire_all()
    assert db.scalar(select(Farmer)).opted_out is False


def test_free_text_is_answered_as_question(client, outbox, fake_llm):
    _, h = make_org(client)
    make_farmer(client, h)
    sms(client, "when should I top dress my maize?")
    assert outbox[-1][1] == fake_llm.answer_text
    kind, question, lang, max_chars, context = fake_llm.calls[-1]
    assert max_chars == 450 and "maize" in context and "Vegetative" in context


def test_free_text_rules_fallback_without_ai(client, outbox):
    _, h = make_org(client)
    make_farmer(client, h)
    sms(client, "how much fertilizer for maize")
    assert "DAP" in outbox[-1][1]


def test_pest_command_uses_symptom_matcher(client, outbox):
    _, h = make_org(client)
    make_farmer(client, h)
    sms(client, "PEST maize caterpillar holes sawdust in whorl")
    assert "Fall armyworm" in outbox[-1][1]


def test_lang_switch(client, outbox, db):
    _, h = make_org(client)
    make_farmer(client, h)
    sms(client, "LANG FR")
    assert db.scalar(select(Farmer)).language == "fr"
    assert outbox[-1][1].startswith("Kilimo Co-op: METEO")


# ------------------------------------------------------------------ USSD


def test_ussd_main_menu_and_weather(client):
    _, h = make_org(client)
    make_farmer(client, h)
    screen = ussd(client, "")
    assert screen.startswith("CON Kilimo Co-op\n1. Weather")
    assert ussd(client, "1").startswith("END Weather")


def test_ussd_sell_flow(client, db):
    _, h = make_org(client)
    make_farmer(client, h)
    assert ussd(client, "3").startswith("CON Choose crop:\n1. Maize")
    assert ussd(client, "3*1") == "CON Quantity in kg:"
    assert ussd(client, "3*1*200").startswith("CON Lowest price")
    final = ussd(client, "3*1*200*0")
    assert final.startswith("END Listed 200kg maize")
    listing = db.scalar(select(Listing))
    assert listing.min_price_per_kg is None


def test_ussd_invalid_and_language(client, db):
    _, h = make_org(client)
    make_farmer(client, h)
    assert ussd(client, "9") == "END Invalid choice."
    assert ussd(client, "7*2") == "END Lugha imewekwa Kiswahili."
    assert ussd(client, "").startswith("CON Kilimo Co-op\n1. Hali ya hewa")


def test_ussd_question_answered_by_sms(client, outbox, fake_llm):
    _, h = make_org(client)
    make_farmer(client, h)
    assert ussd(client, "5*my beans are yellow").startswith("END Thank you")
    assert outbox[-1] == ("+254712345678", fake_llm.answer_text)


def test_ussd_pest_flow(client):
    _, h = make_org(client)
    make_farmer(client, h)
    assert ussd(client, "4*1").startswith("CON Describe")
    assert "Fall armyworm" in ussd(client, "4*1*caterpillars and sawdust in whorl")


# ------------------------------------------------------------------ WhatsApp


def test_whatsapp_media_from_unknown_host_is_refused(client, outbox):
    _, h = make_org(client)
    make_farmer(client, h)
    r = client.post(
        "/webhooks/whatsapp",
        data={"From": "whatsapp:+254712345678", "To": "whatsapp:+14155238886", "Body": "", "MediaUrl0": "https://evil.example.com/x.jpg"},
        headers=HOOK,
    )
    assert r.status_code == 200
    assert "retake" in outbox[-1][1].lower()
