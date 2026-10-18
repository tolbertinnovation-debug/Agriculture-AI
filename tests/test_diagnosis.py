from sqlalchemy import select

from agri_ai.models import Diagnosis
from agri_ai.services.image_quality import check_and_normalize
from conftest import diagnosis_result, flat_jpeg, leaf_jpeg, make_farmer, make_org


def login(client, phone="0712345678"):
    code = client.post("/api/v1/farmer/auth/request-code", json={"phone": phone}).json()["dev_code"]
    token = client.post("/api/v1/farmer/auth/verify", json={"phone": phone, "code": code}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def upload(client, headers, data, **form):
    return client.post("/api/v1/farmer/diagnose", files={"image": ("leaf.jpg", data, "image/jpeg")}, data=form, headers=headers)


def test_quality_gate():
    assert check_and_normalize(leaf_jpeg()).ok
    assert check_and_normalize(flat_jpeg()).reason == "blurry"
    assert check_and_normalize(flat_jpeg((5, 5, 5))).reason == "too dark"
    assert check_and_normalize(leaf_jpeg(size=100)).reason == "too small"
    assert check_and_normalize(b"not an image").reason == "not an image"


def test_confident_diagnosis_is_delivered_automatically(client, fake_llm):
    _, h = make_org(client)
    make_farmer(client, h)
    auth = login(client)
    r = upload(client, auth, leaf_jpeg())
    body = r.json()
    assert body["diagnosis"]["status"] == "auto"
    assert body["diagnosis"]["pest_id"] == "fall_armyworm"
    assert body["message"].startswith("Fall armyworm")
    assert fake_llm.calls[-1] == ("diagnose", "maize", "en")  # crop inferred from the farmer's only plot


def test_low_confidence_goes_to_officer_queue_and_review_notifies(client, fake_llm, outbox):
    _, h = make_org(client)
    make_farmer(client, h)
    auth = login(client)
    fake_llm.diagnosis = diagnosis_result(pest_id="grey_leaf_spot", confidence=0.4, label="Grey leaf spot")
    body = upload(client, auth, leaf_jpeg()).json()
    assert body["diagnosis"]["status"] == "needs_review"
    assert "extension officer" in body["message"]
    queue = client.get("/api/v1/partner/diagnoses?status=needs_review", headers=h).json()
    assert len(queue) == 1 and queue[0]["has_image"]
    img = client.get(f"/api/v1/partner/diagnoses/{queue[0]['id']}/image", headers=h)
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"

    r = client.post(
        f"/api/v1/partner/diagnoses/{queue[0]['id']}/review",
        json={"status": "confirmed", "pest_id": "northern_leaf_blight", "notes": "Use resistant seed next season."},
        headers=h,
    )
    assert r.json()["status"] == "confirmed" and r.json()["confidence"] == 1.0
    assert outbox[-1][1].startswith(f"Officer review of D{queue[0]['id']}: Northern leaf blight")


def test_notifiable_disease_always_needs_officer(client, fake_llm):
    _, h = make_org(client)
    make_farmer(client, h)
    fake_llm.diagnosis = diagnosis_result(pest_id="maize_lethal_necrosis", confidence=0.95, label="MLN")
    body = upload(client, login(client), leaf_jpeg()).json()
    assert body["diagnosis"]["status"] == "needs_review"


def test_hallucinated_pest_id_is_coerced(client, fake_llm):
    _, h = make_org(client)
    make_farmer(client, h)
    fake_llm.diagnosis = diagnosis_result(pest_id="space_locust", confidence=0.99)
    body = upload(client, login(client), leaf_jpeg()).json()
    assert body["diagnosis"]["pest_id"] == "other" and body["diagnosis"]["status"] == "needs_review"


def test_blurry_photo_asks_for_retake_without_calling_model(client, fake_llm):
    _, h = make_org(client)
    make_farmer(client, h)
    body = upload(client, login(client), flat_jpeg()).json()
    assert body["retake"] is True and body["diagnosis"] is None
    assert fake_llm.calls == []


def test_model_outage_falls_back_to_note_then_queue(client, fake_llm, db):
    _, h = make_org(client)
    make_farmer(client, h)
    auth = login(client)
    fake_llm.fail = True
    body = upload(client, auth, leaf_jpeg(), note="caterpillars eating the whorl, sawdust").json()
    assert body["diagnosis"]["pest_id"] == "fall_armyworm" and body["diagnosis"]["engine"] == "rules"
    body = upload(client, auth, leaf_jpeg(2)).json()
    assert body["diagnosis"]["status"] == "needs_review"


def test_daily_ai_limit(client, fake_llm, monkeypatch):
    import dataclasses

    from agri_ai.config import get_settings
    from agri_ai.services import advisory

    limited = dataclasses.replace(get_settings(), ai_daily_limit=1)
    monkeypatch.setattr(advisory, "get_settings", lambda: limited)
    _, h = make_org(client)
    make_farmer(client, h)
    auth = login(client)
    upload(client, auth, leaf_jpeg())
    upload(client, auth, leaf_jpeg(3))
    assert len([c for c in fake_llm.calls if c[0] == "diagnose"]) == 1


def test_text_diagnosis_endpoint(client, db):
    _, h = make_org(client)
    make_farmer(client, h)
    r = client.post("/api/v1/farmer/diagnose/text", json={"symptoms": "orange powder on leaves", "crop": "coffee"}, headers=login(client))
    assert r.json()["diagnosis"]["pest_id"] == "coffee_leaf_rust"
    assert db.scalar(select(Diagnosis)).source == "text"
