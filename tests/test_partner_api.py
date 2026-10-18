from conftest import ADMIN, make_farmer, make_org


def test_admin_token_required(client):
    assert client.post("/api/v1/admin/orgs", json={"name": "X"}).status_code == 401


def test_org_onboarding_and_key_roles(client):
    org, headers = make_org(client)
    assert client.get("/api/v1/partner/org", headers=headers).json()["name"] == "Kilimo Co-op"
    assert client.get("/api/v1/partner/org", headers={"X-API-Key": "agk_nope"}).status_code == 401

    r = client.post("/api/v1/partner/keys", json={"name": "viewer", "role": "viewer"}, headers=headers)
    assert r.status_code == 201
    viewer = {"X-API-Key": r.json()["key"]}
    assert client.get("/api/v1/partner/farmers", headers=viewer).status_code == 200
    assert client.post("/api/v1/partner/farmers", json={"phone": "0700000001"}, headers=viewer).status_code == 403

    client.delete(f"/api/v1/partner/keys/{r.json()['id']}", headers=headers)
    assert client.get("/api/v1/partner/farmers", headers=viewer).status_code == 401


def test_shortcode_must_be_unique(client):
    make_org(client)
    r = client.post("/api/v1/admin/orgs", json={"name": "Other", "sms_shortcode": "22384"}, headers=ADMIN)
    assert r.status_code == 409


def test_farmer_crud_and_stage(client, outbox):
    _, headers = make_org(client)
    f = make_farmer(client, headers, send_welcome=True)
    assert f["phone"] == "+254712345678"
    assert f["plots"][0]["stage"] == "Vegetative growth"
    assert outbox and "Welcome" in outbox[-1][1]

    r = client.patch(f"/api/v1/partner/farmers/{f['id']}", json={"language": "sw"}, headers=headers)
    assert r.json()["language"] == "sw"
    page = client.get("/api/v1/partner/farmers?crop=mahindi", headers=headers).json()
    assert page["total"] == 1
    assert client.get("/api/v1/partner/farmers?crop=beans", headers=headers).json()["total"] == 0


def test_tenant_isolation(client):
    _, a = make_org(client)
    _, b = make_org(client, name="Agro NGO", sms_shortcode="30000", ussd_code="*300#")
    f = make_farmer(client, a)
    assert client.get(f"/api/v1/partner/farmers/{f['id']}", headers=b).status_code == 404
    assert client.get("/api/v1/partner/farmers", headers=b).json()["total"] == 0
    # A phone number belongs to one partner; another partner cannot silently take it over.
    r = client.post("/api/v1/partner/farmers", json={"phone": "+254712345678"}, headers=b)
    assert r.status_code == 409


def test_csv_import(client):
    _, headers = make_org(client)
    csv_data = (
        "phone,name,language,gender,region,village,lat,lon,crops,planting_date,consent_marketplace\n"
        "0711000001,Amina,sw,female,Kisumu,Ahero,-0.17,34.92,rice;beans,2026-03-01,yes\n"
        "0711000002,Otieno,en,male,Kisumu,Ahero,-0.18,34.91,maize,,no\n"
        "notaphone,Bad,en,,,,,,maize,,\n"
        "0711000003,Bad crop,en,,,,,,unicorn,,\n"
    )
    r = client.post("/api/v1/partner/farmers/import", files={"file": ("farmers.csv", csv_data, "text/csv")}, headers=headers)
    body = r.json()
    assert body["created"] == 2 and len(body["errors"]) == 2
    # Re-import is an update, not a duplicate.
    r = client.post("/api/v1/partner/farmers/import", files={"file": ("farmers.csv", csv_data, "text/csv")}, headers=headers)
    assert r.json()["updated"] == 2


def test_broadcast_targets_segment_and_respects_opt_out(client, outbox, db):
    _, headers = make_org(client)
    make_farmer(client, headers)
    make_farmer(client, headers, phone="0722000000", plots=[{"crop": "beans"}])
    third = make_farmer(client, headers, phone="0733000000")
    client.post("/webhooks/sms", data={"from": "+254733000000", "to": "22384", "text": "STOP"}, headers={"X-Webhook-Secret": "test-hook"})

    dry = client.post("/api/v1/partner/broadcast", json={"message": "Field day Friday", "crop": "maize", "dry_run": True}, headers=headers).json()
    assert dry == {"recipients": 1, "segments_per_message": 1, "total_segments": 1, "sent": 0}
    outbox.clear()
    sent = client.post("/api/v1/partner/broadcast", json={"message": "Field day Friday", "crop": "maize"}, headers=headers).json()
    assert sent["sent"] == 1 and [p for p, _ in outbox] == ["+254712345678"]
    assert third["id"]


def test_delete_farmer_erases_personal_data(client, db):
    _, headers = make_org(client)
    f = make_farmer(client, headers, send_welcome=True)
    assert client.delete(f"/api/v1/partner/farmers/{f['id']}", headers=headers).status_code == 204
    msgs = client.get("/api/v1/partner/messages", headers=headers).json()
    assert msgs and all(m["phone"] == "redacted" and m["farmer_id"] is None for m in msgs)


def test_price_upload_and_analytics(client):
    _, headers = make_org(client)
    make_farmer(client, headers, gender="female")
    r = client.post(
        "/api/v1/partner/prices",
        json={"prices": [{"crop": "maize", "market": "Nakuru", "price_per_kg": 42, "lat": -0.28, "lon": 36.07}]},
        headers=headers,
    )
    assert r.json() == {"recorded": 1}
    quotes = client.get("/api/v1/partner/prices?crop=maize", headers=headers).json()
    assert quotes[0]["price_per_kg"] == 42
    stats = client.get("/api/v1/partner/analytics", headers=headers).json()
    assert stats["farmers"]["total"] == 1
    assert stats["farmers"]["by_gender"] == {"female": 1}
    assert stats["farmers"]["by_crop"] == {"maize": 1}
