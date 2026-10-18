from __future__ import annotations

from ..knowledge import get_kb
from ..models import Diagnosis, Farmer, Match, MatchStatus
from .schemas import DiagnosisOut, FarmerOut, MatchOut, PlotOut


def farmer_out(farmer: Farmer) -> FarmerOut:
    kb = get_kb()
    plots = []
    for p in farmer.plots:
        crop = kb.crops.get(p.crop)
        stage, _ = crop.stage_on(p.planting_date) if crop else (None, None)
        out = PlotOut.model_validate(p)
        out.stage = stage.name if stage else None
        plots.append(out)
    data = FarmerOut.model_validate(farmer)
    data.plots = plots
    return data


def diagnosis_out(d: Diagnosis) -> DiagnosisOut:
    out = DiagnosisOut.model_validate(d)
    out.has_image = bool(d.image_path)
    return out


def match_out(m: Match, *, for_buyer: bool) -> MatchOut:
    listing, req = m.listing, m.buy_request
    out = MatchOut(
        id=m.id,
        code=m.code,
        status=m.status,
        crop=listing.crop,
        quantity_kg=m.quantity_kg,
        price_per_kg=m.price_per_kg,
        currency=req.currency,
        distance_km=m.distance_km,
        score=m.score,
        created_at=m.created_at,
        farmer_region=listing.farmer.region,
        buyer_name=req.buyer.display_name,
    )
    if for_buyer and m.status in (MatchStatus.accepted.value, MatchStatus.completed.value):
        out.farmer_name = listing.farmer.name
        out.farmer_phone = listing.farmer.phone
    return out
