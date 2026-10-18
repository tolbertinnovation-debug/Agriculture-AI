"""Pest & disease diagnosis from a phone photo or a symptom description.

Flow: quality gate -> model diagnosis grounded on the knowledge base -> confidence
routing. Uncertain, notifiable or high-stakes results go to an extension officer's
review queue, and the farmer still gets safe interim advice immediately.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from .. import i18n
from ..config import get_settings
from ..knowledge import get_kb
from ..models import Diagnosis, DiagnosisStatus, Farmer, utcnow
from . import llm as llm_mod
from .advisory import ai_allowed, farmer_context
from .image_quality import check_and_normalize
from .notifications import send_sms
from .weather import farmer_location

log = logging.getLogger("agri_ai.diagnosis")


@dataclass
class DiagnosisOutcome:
    diagnosis: Diagnosis | None
    message: str  # what to tell the farmer (SMS-length)
    retake: bool = False


def _store_image(jpeg: bytes) -> str:
    folder = Path(get_settings().upload_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}.jpg"
    path.write_bytes(jpeg)
    return str(path)


def _farmer_message(diag: Diagnosis, lang: str) -> str:
    kb = get_kb()
    pest = kb.pests.get(diag.pest_id or "")
    if diag.status == DiagnosisStatus.needs_review.value and diag.confidence < get_settings().review_confidence_threshold:
        msg = i18n.t("diagnosis_review", lang, id=diag.id)
        if pest and diag.confidence >= 0.3:  # still useful to share the leading hypothesis
            msg += " " + i18n.t("diagnosis", lang, label=pest.name(lang), confidence=round(diag.confidence * 100), actions=pest.actions(1)[0])
        return msg
    model_msg = (diag.result or {}).get("farmer_message")
    if model_msg:
        msg = model_msg
    elif pest:
        actions = (diag.result or {}).get("actions") or pest.actions(3)
        msg = i18n.t("diagnosis", lang, label=diag.label or pest.name(lang), confidence=round(diag.confidence * 100), actions="; ".join(actions[:3]))
    else:
        msg = diag.label or i18n.t("diagnosis_review", lang, id=diag.id)
    if diag.status == DiagnosisStatus.needs_review.value:
        msg += f" (D{diag.id})"
    return msg


def _route(diag: Diagnosis, needs_expert: bool) -> None:
    threshold = get_settings().review_confidence_threshold
    pest = get_kb().pests.get(diag.pest_id or "")
    notifiable = bool(pest and pest.notifiable)
    if diag.confidence < threshold or needs_expert or notifiable or diag.pest_id == "other":
        diag.status = DiagnosisStatus.needs_review.value
    else:
        diag.status = DiagnosisStatus.auto.value


def diagnose_photo(
    db: Session,
    farmer: Farmer,
    image: bytes,
    *,
    crop: str | None = None,
    note: str | None = None,
    channel: str = "app",
) -> DiagnosisOutcome:
    lang = farmer.language
    kb = get_kb()
    crop_obj = kb.resolve_crop(crop) if crop else None
    if crop_obj is None and len(farmer.crops) == 1:
        crop_obj = kb.crops.get(farmer.crops[0])
    crop_id = crop_obj.id if crop_obj else None

    check = check_and_normalize(image)
    if not check.ok:
        return DiagnosisOutcome(None, i18n.t("retake_photo", lang, reason=check.reason), retake=True)
    assert check.jpeg is not None

    loc = farmer_location(farmer)
    diag = Diagnosis(
        org_id=farmer.org_id,
        farmer_id=farmer.id,
        crop=crop_id,
        channel=channel,
        source="photo",
        image_path=_store_image(check.jpeg),
        symptoms_text=note,
        lat=loc[0] if loc else None,
        lon=loc[1] if loc else None,
    )

    model = llm_mod.get_llm()
    result: dict | None = None
    if model is not None and ai_allowed(db, farmer):
        context = farmer_context(db, farmer, include_weather=False)
        if note:
            context += f"\nFarmer's note: {note[:500]}"
        try:
            result = model.diagnose_image(check.jpeg, "image/jpeg", crop=crop_id, context=context, lang=lang)
        except llm_mod.LLMUnavailable as exc:
            log.warning("diagnosis LLM unavailable: %s", exc)

    if result is not None:
        diag.engine = "llm"
        if not result.get("image_usable", True):
            return DiagnosisOutcome(None, i18n.t("retake_photo", lang, reason="unclear"), retake=True)
        pest_id = result.get("pest_id") or "other"
        if pest_id not in kb.pests and pest_id not in {"none", "other"}:
            pest_id = "other"
        detected = result.get("crop_detected")
        if crop_id is None and detected in kb.crops:
            diag.crop = detected
        diag.pest_id = pest_id
        diag.label = (result.get("label") or "")[:160] or (kb.pests[pest_id].name(lang) if pest_id in kb.pests else pest_id)
        diag.confidence = max(0.0, min(1.0, float(result.get("confidence") or 0)))
        diag.severity = result.get("severity")
        diag.result = result
        _route(diag, bool(result.get("needs_expert")))
        if result.get("healthy") and pest_id == "none" and diag.confidence >= get_settings().review_confidence_threshold:
            diag.status = DiagnosisStatus.auto.value
    elif note:
        # No model: fall back to the symptom matcher on the farmer's note.
        return _finish(db, farmer, _text_diagnosis(diag, note, crop_id, lang))
    else:
        diag.engine = "rules"
        diag.status = DiagnosisStatus.needs_review.value
        diag.confidence = 0.0

    return _finish(db, farmer, diag)


def diagnose_text(db: Session, farmer: Farmer, symptoms: str, *, crop: str | None = None, channel: str = "sms") -> DiagnosisOutcome:
    """Symptom description without a photo (SMS / USSD / feature phones)."""
    kb = get_kb()
    crop_obj = kb.resolve_crop(crop) if crop else kb.resolve_crop(symptoms)
    if crop_obj is None and len(farmer.crops) == 1:
        crop_obj = kb.crops.get(farmer.crops[0])
    loc = farmer_location(farmer)
    diag = Diagnosis(
        org_id=farmer.org_id,
        farmer_id=farmer.id,
        crop=crop_obj.id if crop_obj else None,
        channel=channel,
        source="text",
        symptoms_text=symptoms[:1000],
        lat=loc[0] if loc else None,
        lon=loc[1] if loc else None,
    )
    return _finish(db, farmer, _text_diagnosis(diag, symptoms, diag.crop, farmer.language))


def _text_diagnosis(diag: Diagnosis, text: str, crop_id: str | None, lang: str) -> Diagnosis:
    kb = get_kb()
    diag.engine = "rules"
    diag.source = diag.source or "text"
    matches = kb.match_symptoms(text, crop_id)
    if matches:
        pest, score = matches[0]
        # Text descriptions are weaker evidence than photos: cap confidence.
        diag.pest_id, diag.label, diag.confidence = pest.id, pest.name(lang), min(score, 0.75)
        diag.severity = pest.severity
        diag.result = {
            "actions": pest.actions(3),
            "alternatives": [p.id for p, _ in matches[1:]],
            "matched_on": "keywords",
        }
        _route(diag, needs_expert=len(matches) > 1 and matches[1][1] >= score - 0.1)
    else:
        diag.status = DiagnosisStatus.needs_review.value
        diag.confidence = 0.0
    return diag


def _finish(db: Session, farmer: Farmer, diag: Diagnosis) -> DiagnosisOutcome:
    db.add(diag)
    db.flush()
    farmer.last_active_at = utcnow()
    return DiagnosisOutcome(diag, _farmer_message(diag, farmer.language))


def review(db: Session, diag: Diagnosis, *, pest_id: str | None, status: str, notes: str | None, notify: bool = True) -> Diagnosis:
    """Extension officer confirms, corrects or rejects a diagnosis; the farmer is told."""
    kb = get_kb()
    farmer = diag.farmer
    lang = farmer.language
    if pest_id:
        if pest_id not in kb.pests and pest_id not in {"none", "other"}:
            raise ValueError(f"unknown pest id {pest_id}")
        diag.pest_id = pest_id
        diag.label = kb.pests[pest_id].name(lang) if pest_id in kb.pests else pest_id
    diag.status = status
    diag.reviewer_notes = notes
    diag.reviewed_at = utcnow()
    if status == DiagnosisStatus.confirmed.value:
        diag.confidence = 1.0
        diag.engine = diag.engine if diag.engine == "llm" else "officer"
    if notify:
        pest = kb.pests.get(diag.pest_id or "")
        detail = notes or ("; ".join(pest.actions(2)) if pest else "")
        send_sms(
            db,
            farmer.phone,
            i18n.t("diagnosis_reviewed", lang, id=diag.id, label=diag.label or "-", notes=detail).strip(),
            farmer=farmer,
            transactional=True,
        )
    return diag
