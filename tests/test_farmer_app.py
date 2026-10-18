from types import SimpleNamespace

import pytest

from agri_ai.services import llm as llm_mod
from conftest import make_farmer, make_org


def test_otp_login_flow(client, outbox):
    _, h = make_org(client)
    make_farmer(client, h)
    r = client.post("/api/v1/farmer/auth/request-code", json={"phone": "0712 345 678"})
    code = r.json()["dev_code"]
    assert code in outbox[-1][1]
    assert client.post("/api/v1/farmer/auth/verify", json={"phone": "0712345678", "code": "000000" if code != "000000" else "111111"}).status_code == 401
    r = client.post("/api/v1/farmer/auth/verify", json={"phone": "+254712345678", "code": code})
    assert r.status_code == 200
    token = r.json()["token"]
    # Codes are single use.
    assert client.post("/api/v1/farmer/auth/verify", json={"phone": "0712345678", "code": code}).status_code == 401
    me = client.get("/api/v1/farmer/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["name"] == "Wanjiku"
    assert client.get("/api/v1/farmer/me", headers={"Authorization": "Bearer forged.token"}).status_code == 401


def test_unknown_phone_gets_same_response_but_no_sms(client, outbox):
    make_org(client)
    r = client.post("/api/v1/farmer/auth/request-code", json={"phone": "0799999999"})
    assert r.json() == {"sent": True} and outbox == []


def test_otp_rate_limit(client):
    _, h = make_org(client)
    make_farmer(client, h)
    for _ in range(3):
        assert client.post("/api/v1/farmer/auth/request-code", json={"phone": "0712345678"}).status_code == 200
    assert client.post("/api/v1/farmer/auth/request-code", json={"phone": "0712345678"}).status_code == 429


def auth(client):
    code = client.post("/api/v1/farmer/auth/request-code", json={"phone": "0712345678"}).json()["dev_code"]
    return {"Authorization": "Bearer " + client.post("/api/v1/farmer/auth/verify", json={"phone": "0712345678", "code": code}).json()["token"]}


def test_home_screen_and_advice_feedback(client, fake_llm):
    _, h = make_org(client)
    make_farmer(client, h)
    a = auth(client)
    home = client.get("/api/v1/farmer/home", headers=a).json()
    assert home["brand"] == "Kilimo Co-op" and len(home["forecast"]) == 7
    assert home["tips"][0].startswith("Maize (Vegetative growth)")
    r = client.post("/api/v1/farmer/advice", json={"question": "Should I weed now?"}, headers=a).json()
    assert r["engine"] == "llm"
    assert client.post(f"/api/v1/farmer/advice/{r['id']}/feedback", json={"helpful": True}, headers=a).status_code == 204
    stats = client.get("/api/v1/partner/analytics", headers=h).json()
    assert stats["advice"] == {"by_engine": {"llm": 1}, "rated": 1, "helpful": 1}


def test_listing_requires_consent(client):
    _, h = make_org(client)
    make_farmer(client, h, consent_marketplace=False)
    a = auth(client)
    assert client.post("/api/v1/farmer/listings", json={"crop": "maize", "quantity_kg": 100}, headers=a).status_code == 409
    client.patch("/api/v1/farmer/me", json={"consent_marketplace": True}, headers=a)
    r = client.post("/api/v1/farmer/listings", json={"crop": "mahindi", "quantity_kg": 100}, headers=a)
    assert r.status_code == 201 and r.json()["crop"] == "maize"
    assert client.post(f"/api/v1/farmer/listings/{r.json()['id']}/withdraw", headers=a).json()["status"] == "withdrawn"


def test_price_alert_crud(client):
    _, h = make_org(client)
    make_farmer(client, h)
    a = auth(client)
    r = client.post("/api/v1/farmer/price-alerts", json={"crop": "beans", "direction": "below", "threshold_per_kg": 80}, headers=a)
    assert r.status_code == 201
    assert len(client.get("/api/v1/farmer/price-alerts", headers=a).json()) == 1
    client.delete(f"/api/v1/farmer/price-alerts/{r.json()['id']}", headers=a)
    assert client.get("/api/v1/farmer/price-alerts", headers=a).json() == []


def test_public_knowledge_endpoints(client):
    crops = client.get("/api/v1/crops").json()
    assert {c["id"] for c in crops} >= {"maize", "cassava", "coffee"}
    pests = client.get("/api/v1/pests?crop=cassava").json()
    assert {p["id"] for p in pests} == {"cassava_mosaic_disease", "cassava_brown_streak", "cassava_mealybug", "whitefly"}


# ------------------------------------------------------------------ Claude request shape


class _Recorder:
    def __init__(self, response):
        self.kwargs = None
        self.response = response
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _response(text, stop_reason="end_turn"):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason, usage=usage, model="claude-opus-5-5")


def test_claude_diagnosis_request(monkeypatch):
    import json

    from conftest import diagnosis_result

    model = llm_mod.ClaudeLLM(api_key="test", model="claude-opus-5-5")
    rec = _Recorder(_response(json.dumps(diagnosis_result())))
    model.client = rec
    out = model.diagnose_image(b"\xff\xd8jpeg", "image/jpeg", crop="maize", context="Crop: maize", lang="sw")
    assert out["pest_id"] == "fall_armyworm"
    kw = rec.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["output_config"]["effort"] == "medium"
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "fall_armyworm" in kw["system"][0]["text"]  # knowledge base is in the cached prefix
    content = kw["messages"][0]["content"]
    assert content[0]["type"] == "image" and "Kiswahili" in content[1]["text"]


def test_claude_refusal_and_bad_json_raise_unavailable():
    model = llm_mod.ClaudeLLM(api_key="test", model="claude-opus-5-5")
    model.client = _Recorder(_response("", stop_reason="refusal"))
    with pytest.raises(llm_mod.LLMUnavailable):
        model.answer("q", context="", lang="en", max_chars=100)
    model.client = _Recorder(_response("not json"))
    with pytest.raises(llm_mod.LLMUnavailable):
        model.diagnose_image(b"x", "image/jpeg", crop=None, context="", lang="en")


def test_system_prompt_is_byte_stable():
    # Prompt caching only works if the prefix never changes between requests.
    assert llm_mod.system_blocks() is llm_mod.system_blocks()
    assert llm_mod.system_blocks()[0]["text"] == llm_mod.BASE_SYSTEM_PROMPT + "\n\n" + llm_mod._kb_reference()
