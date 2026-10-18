"""FastAPI application: ``uvicorn agri_ai.main:app``."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import admin, buyer, farmer_app, partner, webhooks
from .config import get_settings
from .db import create_all
from .knowledge import get_kb

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    create_all()
    get_kb()  # fail fast on a broken knowledge base
    yield


app = FastAPI(
    title="Agriculture AI",
    version=__version__,
    description=(
        "Crop advice, pest detection from a phone photo, weather and price alerts, and farmer-buyer matching "
        "for smallholder farmers - delivered through telcos, co-operatives and NGOs over SMS, USSD, WhatsApp and web."
    ),
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

for r in (admin.router, partner.router, buyer.router, farmer_app.router, webhooks.router):
    app.include_router(r)


@app.get("/health", tags=["public"])
def health():
    s = get_settings()
    return {"status": "ok", "version": __version__, "ai": "claude" if s.llm_available else "rules", "sms_gateway": s.sms_gateway}


@app.get("/api/v1/crops", tags=["public"])
def crops():
    kb = get_kb()
    return [
        {
            "id": c.id,
            "names": c.names,
            "season_days": c.season_days,
            "perennial": c.perennial,
            "spacing": c.spacing,
            "fertilizer": c.fertilizer,
            "stages": [{"id": st.id, "name": st.name, "start_day": st.start, "end_day": st.end, "tasks": list(st.tasks)} for st in c.stages],
            "pests": list(c.pests),
        }
        for c in kb.crops.values()
    ]


@app.get("/api/v1/pests", tags=["public"])
def pests(crop: str | None = None):
    kb = get_kb()
    c = kb.resolve_crop(crop) if crop else None
    return [
        {
            "id": p.id,
            "names": p.names,
            "type": p.type,
            "crops": list(p.crops),
            "symptoms": p.symptoms,
            "severity": p.severity,
            "notifiable": p.notifiable,
            "cultural": list(p.cultural),
            "biological": list(p.biological),
            "chemical": list(p.chemical),
        }
        for p in kb.pests_for(c.id if c else None)
    ]


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/app", include_in_schema=False)
def farmer_app_page():
    return FileResponse(STATIC / "app" / "index.html")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    # Served from the root so it can control /app.
    return FileResponse(STATIC / "app" / "sw.js", media_type="application/javascript")


@app.get("/dashboard", include_in_schema=False)
def dashboard_page():
    return FileResponse(STATIC / "dashboard" / "index.html")


@app.get("/docs/api", include_in_schema=False)
def api_docs():
    return RedirectResponse("/docs")
