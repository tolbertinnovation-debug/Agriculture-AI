from datetime import timedelta

from sqlalchemy import select

from agri_ai.models import Listing, MarketPrice, Match, utcnow
from conftest import HOOK, make_farmer, make_org


def setup_market(client, lots):
    """lots: list of (phone, kg, min_price, grade, lat, lon, consent)."""
    _, coop = make_org(client)
    for phone, kg, price, grade, lat, lon, consent in lots:
        make_farmer(client, coop, phone=phone, lat=lat, lon=lon, consent_marketplace=consent)
        client.post(
            "/webhooks/sms",
            data={"from": phone, "to": "22384", "text": f"SELL maize {kg}kg" + (f" {price}" if price else "")},
            headers=HOOK,
        )
    _, buyer = make_org(client, name="Unga Millers", kind="buyer", sms_shortcode=None, ussd_code=None, contact_phone="+254700999999")
    return coop, buyer


def post_request(client, buyer, **overrides):
    body = {"crop": "maize", "quantity_kg": 1000, "price_per_kg": 40, "lat": -0.30, "lon": 35.95, "radius_km": 50}
    body.update(overrides)
    r = client.post("/api/v1/buyer/requests", json=body, headers=buyer)
    assert r.status_code == 201, r.text
    return r.json()


def test_aggregates_several_smallholders_to_fill_an_order(client, outbox, db):
    _, buyer = setup_market(
        client,
        [
            ("+254711000001", 600, 35, "B", -0.31, 35.94, True),
            ("+254711000002", 300, None, "A", -0.32, 35.96, True),
            ("+254711000003", 400, 38, "B", -0.35, 35.90, True),
        ],
    )
    req = post_request(client, buyer)
    assert req["committed_kg"] == 1000
    matches = client.get(f"/api/v1/buyer/requests/{req['id']}/matches", headers=buyer).json()
    assert sum(m["quantity_kg"] for m in matches) == 1000
    assert len(matches) == 3
    # Identity hidden until the farmer accepts.
    assert all(m["farmer_phone"] is None for m in matches)
    offers = [b for p, b in outbox if "Unga Millers wants" in b]
    assert len(offers) == 3


def test_filters_consent_price_grade_and_distance(client, db):
    _, buyer = setup_market(
        client,
        [
            ("+254711000001", 500, None, "B", -0.31, 35.94, True),  # eligible
            ("+254711000002", 500, 55, "B", -0.31, 35.94, True),  # wants more than buyer pays
            ("+254711000003", 500, None, "B", 3.00, 38.00, True),  # far away
        ],
    )
    req = post_request(client, buyer, quantity_kg=2000)
    ids = {c["listing_id"] for c in client.get(f"/api/v1/buyer/requests/{req['id']}/candidates", headers=buyer).json()}
    matched = client.get(f"/api/v1/buyer/requests/{req['id']}/matches", headers=buyer).json()
    assert len(matched) == 1 and ids == set()  # already matched, nothing left
    # Grade filter
    req2 = post_request(client, buyer, min_grade="A")
    assert req2["committed_kg"] == 0


def test_farmer_without_consent_is_never_matched(client, db):
    _, coop = make_org(client)
    make_farmer(client, coop, consent_marketplace=False)
    farmer_listing = Listing(farmer_id=1, crop="maize", quantity_kg=500, remaining_kg=500, currency="KES", available_from=utcnow().date())
    db.add(farmer_listing)
    db.commit()
    _, buyer = make_org(client, name="Buyer Ltd", kind="buyer", sms_shortcode=None, ussd_code=None)
    assert post_request(client, buyer)["committed_kg"] == 0


def test_accept_reveals_contacts_and_fills_order(client, outbox, db):
    _, buyer = setup_market(client, [("+254711000001", 1000, None, "B", -0.31, 35.94, True)])
    req = post_request(client, buyer)
    code = db.scalar(select(Match)).code
    client.post("/webhooks/sms", data={"from": "+254711000001", "to": "22384", "text": f"YES {code.lower()}"}, headers=HOOK)
    assert f"Deal {code} confirmed" in outbox[-1][1] and "+254700999999" in outbox[-1][1]
    m = client.get(f"/api/v1/buyer/requests/{req['id']}/matches", headers=buyer).json()[0]
    assert m["status"] == "accepted" and m["farmer_phone"] == "+254711000001"
    assert client.get(f"/api/v1/buyer/requests/{req['id']}", headers=buyer).json()["status"] == "filled"
    db.expire_all()
    assert db.scalar(select(Listing)).status == "sold"

    r = client.post(f"/api/v1/buyer/matches/{m['id']}/complete", headers=buyer)
    assert r.json()["status"] == "completed"
    deal_price = db.scalar(select(MarketPrice).where(MarketPrice.source == "deal"))
    assert deal_price.price_per_kg == 40


def test_decline_rematches_next_best_lot(client, outbox, db):
    _, buyer = setup_market(
        client,
        [
            ("+254711000001", 1000, None, "A", -0.30, 35.95, True),
            ("+254711000002", 1000, None, "C", -0.60, 36.10, True),
        ],
    )
    req = post_request(client, buyer)
    first = db.scalar(select(Match))
    assert first.listing.farmer.phone == "+254711000001"
    client.post("/webhooks/sms", data={"from": "+254711000001", "to": "22384", "text": f"NO {first.code}"}, headers=HOOK)
    matches = client.get(f"/api/v1/buyer/requests/{req['id']}/matches", headers=buyer).json()
    statuses = sorted(m["status"] for m in matches)
    assert statuses == ["declined", "proposed"]
    assert any(p == "+254711000002" and "Unga Millers wants" in b for p, b in outbox)
    assert outbox[-1] == ("+254711000001", f"Offer {first.code} declined. Your produce stays listed.")


def test_wrong_farmer_cannot_accept_someone_elses_offer(client, outbox, db):
    _, buyer = setup_market(
        client, [("+254711000001", 1000, None, "B", -0.31, 35.94, True), ("+254711000002", 10, None, "B", -0.31, 35.94, True)]
    )
    post_request(client, buyer)
    code = db.scalar(select(Match).join(Listing).where(Listing.quantity_kg == 1000)).code
    client.post("/webhooks/sms", data={"from": "+254711000002", "to": "22384", "text": f"YES {code}"}, headers=HOOK)
    assert "No open offer" in outbox[-1][1]


def test_unanswered_offers_expire_and_rematch(client, db):
    _, buyer = setup_market(
        client, [("+254711000001", 1000, None, "A", -0.30, 35.95, True), ("+254711000002", 1000, None, "C", -0.60, 36.10, True)]
    )
    post_request(client, buyer)
    m = db.scalar(select(Match))
    m.created_at = utcnow() - timedelta(hours=60)
    db.commit()
    r = client.post("/api/v1/admin/jobs/run?jobs=marketplace", headers={"X-Admin-Token": "test-admin"})
    assert r.json()["offers_expired"] == 1
    db.expire_all()
    assert [x.status for x in db.scalars(select(Match).order_by(Match.id))] == ["cancelled", "proposed"]


def test_new_listing_fills_open_order(client, db):
    _, coop = make_org(client)
    make_farmer(client, coop)
    _, buyer = make_org(client, name="Buyer Ltd", kind="buyer", sms_shortcode=None, ussd_code=None)
    req = post_request(client, buyer)
    assert req["committed_kg"] == 0
    client.post("/webhooks/sms", data={"from": "+254712345678", "to": "22384", "text": "SELL maize 300"}, headers=HOOK)
    assert client.get(f"/api/v1/buyer/requests/{req['id']}", headers=buyer).json()["committed_kg"] == 300
    supply = client.get("/api/v1/buyer/supply", headers=buyer).json()
    assert supply == [{"crop": "maize", "region": "Nakuru", "currency": "KES", "lots": 1, "total_kg": 300.0}]


def test_buyer_cannot_touch_other_buyers_requests(client):
    _, buyer = setup_market(client, [])
    _, other = make_org(client, name="Other", kind="buyer", sms_shortcode=None, ussd_code=None)
    req = post_request(client, buyer)
    assert client.get(f"/api/v1/buyer/requests/{req['id']}", headers=other).status_code == 404
