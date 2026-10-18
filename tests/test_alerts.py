"""Scheduled jobs: weather, price, stage tips and outbreak alerts."""

from datetime import date, timedelta

from sqlalchemy import select

from agri_ai.models import Alert, Diagnosis, Farmer, MarketPrice, Outbreak
from conftest import ADMIN, make_farmer, make_org


def run(client, jobs=None):
    q = "" if jobs is None else "?" + "&".join(f"jobs={j}" for j in jobs)
    r = client.post(f"/api/v1/admin/jobs/run{q}", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def test_weather_alerts_are_sent_once(client, outbox, fake_weather):
    _, h = make_org(client)
    make_farmer(client, h)
    fake_weather.set([(15, 25, 3, 20, 10), (15, 25, 70, 95, 20)] + [(15, 25, 5, 30, 10)] * 5)
    assert run(client, ["weather"])["weather_alerts"] == 1
    assert "Heavy rain expected tomorrow (70mm)" in outbox[-1][1]
    assert run(client, ["weather"])["weather_alerts"] == 0  # de-duplicated


def test_forecast_is_cached_per_grid_cell(client, fake_weather):
    _, h = make_org(client)
    make_farmer(client, h, phone="0711000001", lat=-0.331, lon=35.941)
    make_farmer(client, h, phone="0711000002", lat=-0.334, lon=35.938)
    run(client, ["weather"])
    assert fake_weather.calls == 1


def test_opted_out_farmers_get_no_alerts(client, outbox, fake_weather, db):
    _, h = make_org(client)
    f = make_farmer(client, h)
    db.get(Farmer, f["id"]).opted_out = True
    db.commit()
    fake_weather.set([(15, 25, 80, 95, 20)] * 7)
    assert run(client, ["weather"])["weather_alerts"] == 0


def _prices(db, rows):
    for crop, market, price, days_ago in rows:
        db.add(
            MarketPrice(crop=crop, market=market, price_per_kg=price, currency="KES", country="KE", lat=-0.28, lon=36.07, observed_on=date.today() - timedelta(days=days_ago))
        )
    db.commit()


def test_price_threshold_and_big_move_alerts(client, outbox, db):
    _, h = make_org(client)
    make_farmer(client, h)
    client.post("/webhooks/sms", data={"from": "+254712345678", "to": "22384", "text": "ALERT maize above 45"}, headers={"X-Webhook-Secret": "test-hook"})
    _prices(db, [("maize", "Nakuru", 38, 8), ("maize", "Nakuru", 46, 0)])
    sent = run(client, ["prices"])["price_alerts"]
    assert sent == 2
    bodies = [b for _, b in outbox[-2:]]
    assert any("now 46 KES/kg (above your 45 target)" in b for b in bodies)
    assert any("up 21.1% this week" in b for b in bodies)
    assert run(client, ["prices"])["price_alerts"] == 0


def test_price_trend_in_sms(client, outbox, db):
    _, h = make_org(client)
    make_farmer(client, h)
    _prices(db, [("beans", "Nakuru", 100, 7), ("beans", "Nakuru", 90, 0)])
    client.post("/webhooks/sms", data={"from": "+254712345678", "to": "22384", "text": "PRICE beans"}, headers={"X-Webhook-Secret": "test-hook"})
    assert "90 KES/kg (down 10%)" in outbox[-1][1]


def test_stage_tips_once_per_stage(client, outbox):
    _, h = make_org(client)
    make_farmer(client, h)
    assert run(client, ["stages"])["stage_tips"] == 1
    assert outbox[-1][1].startswith("Maize (Vegetative growth): Weed at 2-3")
    assert run(client, ["stages"])["stage_tips"] == 0


def _report(db, farmer_id, org_id, lat, lon, pest="fall_armyworm", status="confirmed"):
    db.add(Diagnosis(org_id=org_id, farmer_id=farmer_id, crop="maize", pest_id=pest, status=status, confidence=1.0, lat=lat, lon=lon, source="photo"))


def test_outbreak_detection_warns_neighbours_across_partners(client, outbox, db):
    org_a, a = make_org(client)
    org_b, b = make_org(client, name="Telco", kind="telco", sms_shortcode="40000", ussd_code="*400#")
    reporters = [make_farmer(client, a, phone=f"07110000{i:02d}", lat=-0.33 + i * 0.01, lon=35.94) for i in range(3)]
    neighbour = make_farmer(client, b, phone="0722111222", lat=-0.40, lon=35.99)  # other partner, ~10 km away
    make_farmer(client, b, phone="0722111333", lat=-0.40, lon=35.99, plots=[{"crop": "coffee"}])  # crop not affected
    make_farmer(client, b, phone="0722111444", lat=1.5, lon=37.0)  # far away
    for r in reporters:
        _report(db, r["id"], org_a["id"], r["lat"], r["lon"])
    db.commit()

    summary = run(client, ["outbreaks"])
    assert summary["outbreaks_updated"] == 1
    ob = db.scalar(select(Outbreak))
    assert ob.report_count == 3 and ob.pest_id == "fall_armyworm"
    alerted = {a.farmer_id for a in db.scalars(select(Alert).where(Alert.kind == "outbreak"))}
    assert neighbour["id"] in alerted and len(alerted) == 4  # three reporters + neighbour
    assert any("Fall armyworm reported on 3 farms" in body for _, body in outbox)
    assert run(client, ["outbreaks"])["outbreak_alerts"] == 0
    assert client.get("/api/v1/partner/outbreaks", headers=b).json()[0]["report_count"] == 3


def test_repeated_reports_from_one_farm_are_not_an_outbreak(client, db):
    org, h = make_org(client)
    f = make_farmer(client, h)
    for _ in range(5):
        _report(db, f["id"], org["id"], -0.33, 35.94)
    _report(db, f["id"], org["id"], -0.33, 35.94, status="needs_review")
    db.commit()
    assert run(client, ["outbreaks"])["outbreaks_updated"] == 0
