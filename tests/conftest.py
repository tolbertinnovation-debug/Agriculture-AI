from __future__ import annotations

import io
import os
import random
import tempfile
from datetime import date, timedelta

os.environ.update(
    {
        "AGRI_DATABASE_URL": "sqlite://",
        "AGRI_ADMIN_TOKEN": "test-admin",
        "AGRI_WEBHOOK_SECRET": "test-hook",
        "AGRI_WEATHER_PROVIDER": "static",
        "AGRI_UPLOAD_DIR": tempfile.mkdtemp(prefix="agri-uploads-"),
        "AGRI_DEV_EXPOSE_OTP": "1",
    }
)
os.environ.pop("ANTHROPIC_API_KEY", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from agri_ai import db as db_mod  # noqa: E402
from agri_ai.db import Base  # noqa: E402
from agri_ai.main import app  # noqa: E402
from agri_ai.services import llm as llm_mod  # noqa: E402
from agri_ai.services import notifications, weather  # noqa: E402
from agri_ai.services.weather import DayForecast  # noqa: E402

ADMIN = {"X-Admin-Token": "test-admin"}
HOOK = {"X-Webhook-Secret": "test-hook"}


class FakeWeather:
    name = "fake"

    def __init__(self) -> None:
        self.days: list[DayForecast] | None = None
        self.calls = 0

    def set(self, rows: list[tuple[float, float, float, float, float]]) -> None:
        """rows of (tmin, tmax, rain_mm, rain_prob, wind_kmh) starting today."""
        today = date.today()
        self.days = [DayForecast(today + timedelta(days=i), *r) for i, r in enumerate(rows)]

    def forecast(self, lat, lon, days=7):
        self.calls += 1
        if self.days is None:
            self.set([(15, 26, 2, 30, 10)] * 7)
        return self.days[:days]


class FakeLLM:
    def __init__(self) -> None:
        self.diagnosis: dict | None = None
        self.answer_text = "Scout your maize twice a week and remove caterpillars by hand."
        self.fail = False
        self.calls: list[tuple] = []

    def diagnose_image(self, image, media_type, *, crop, context, lang):
        self.calls.append(("diagnose", crop, lang))
        if self.fail:
            raise llm_mod.LLMUnavailable("boom")
        return self.diagnosis or diagnosis_result()

    def answer(self, question, *, context, lang, max_chars):
        self.calls.append(("answer", question, lang, max_chars, context))
        if self.fail:
            raise llm_mod.LLMUnavailable("boom")
        return self.answer_text


def diagnosis_result(**overrides) -> dict:
    base = {
        "image_usable": True,
        "crop_detected": "maize",
        "healthy": False,
        "pest_id": "fall_armyworm",
        "label": "Fall armyworm",
        "confidence": 0.9,
        "severity": "high",
        "visible_evidence": "window-pane feeding and frass in whorl",
        "alternatives": ["maize_stalk_borer"],
        "actions": ["Hand-pick caterpillars", "Spray Bt into the whorl"],
        "needs_expert": False,
        "farmer_message": "Fall armyworm. Hand-pick caterpillars today and spray Bt into the whorl.",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _fresh_state(tmp_path):
    engine = db_mod.init_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    gateway = notifications.ConsoleGateway()
    notifications.set_gateway(gateway)
    weather.set_provider(FakeWeather())
    llm_mod.set_llm(None)
    yield
    llm_mod.reset_llm()
    engine.dispose()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    session = db_mod.session_factory()()
    yield session
    session.close()


@pytest.fixture
def outbox() -> list[tuple[str, str]]:
    return notifications.get_gateway().outbox  # type: ignore[attr-defined]


@pytest.fixture
def fake_weather() -> FakeWeather:
    return weather.get_provider()  # type: ignore[return-value]


@pytest.fixture
def fake_llm() -> FakeLLM:
    model = FakeLLM()
    llm_mod.set_llm(model)
    return model


def make_org(client, **overrides) -> tuple[dict, dict]:
    body = {"name": "Kilimo Co-op", "kind": "cooperative", "country": "KE", "currency": "KES", "sms_shortcode": "22384", "ussd_code": "*384*1#"}
    body.update(overrides)
    r = client.post("/api/v1/admin/orgs", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    data = r.json()
    return data["org"], {"X-API-Key": data["api_key"]}


def make_farmer(client, headers, **overrides) -> dict:
    body = {
        "phone": "0712345678",
        "name": "Wanjiku",
        "language": "en",
        "region": "Nakuru",
        "village": "Njoro",
        "lat": -0.33,
        "lon": 35.94,
        "consent_marketplace": True,
        "plots": [{"crop": "maize", "planting_date": (date.today() - timedelta(days=30)).isoformat(), "area_ha": 0.8}],
        "send_welcome": False,
    }
    body.update(overrides)
    r = client.post("/api/v1/partner/farmers", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def leaf_jpeg(seed: int = 1, size: int = 400) -> bytes:
    """A sharp, textured green image that passes the quality gate."""
    rnd = random.Random(seed)
    img = Image.new("RGB", (size, size))
    px = img.load()
    for x in range(size):
        for y in range(size):
            g = 90 + rnd.randint(0, 120)
            px[x, y] = (rnd.randint(20, 80), g, rnd.randint(10, 60))
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()


def flat_jpeg(color=(120, 140, 100), size: int = 400) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (size, size), color).save(out, format="JPEG")
    return out.getvalue()
