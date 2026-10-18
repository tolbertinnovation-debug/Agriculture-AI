"""Agronomy knowledge base: crops, growth stages, pests and diseases."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Stage:
    id: str
    name: str
    start: int
    end: int
    tasks: tuple[str, ...]


@dataclass(frozen=True)
class Crop:
    id: str
    names: dict[str, str]
    aliases: tuple[str, ...]
    season_days: int
    stages: tuple[Stage, ...]
    pests: tuple[str, ...]
    spacing: str
    fertilizer: str
    frost_sensitive: bool
    heat_stress_c: float
    critical_water_stages: tuple[str, ...]
    perennial: bool = False

    def name(self, lang: str = "en") -> str:
        return self.names.get(lang) or self.names["en"]

    def stage_on(self, planting_date: date | None, today: date | None = None) -> tuple[Stage | None, int | None]:
        """Growth stage on ``today`` for a crop planted on ``planting_date``.

        Perennials cycle through their stages every ``season_days``.
        """
        if planting_date is None:
            return None, None
        days = ((today or date.today()) - planting_date).days
        if days < 0:
            return None, days
        effective = days % self.season_days if self.perennial else days
        for stage in self.stages:
            if stage.start <= effective <= stage.end:
                return stage, days
        return (self.stages[-1] if not self.perennial else None), days


@dataclass(frozen=True)
class Pest:
    id: str
    type: str
    crops: tuple[str, ...]
    names: dict[str, str]
    symptoms: str
    keywords: tuple[str, ...]
    severity: str
    spread_risk: str
    notifiable: bool
    cultural: tuple[str, ...]
    biological: tuple[str, ...]
    chemical: tuple[str, ...]

    def name(self, lang: str = "en") -> str:
        return self.names.get(lang) or self.names["en"]

    def actions(self, limit: int = 3) -> list[str]:
        """Most important actions first: cultural, then biological, then chemical."""
        return [*self.cultural, *self.biological, *self.chemical][:limit]


@dataclass
class KnowledgeBase:
    crops: dict[str, Crop] = field(default_factory=dict)
    pests: dict[str, Pest] = field(default_factory=dict)
    _alias_index: dict[str, str] = field(default_factory=dict)

    def resolve_crop(self, text: str | None) -> Crop | None:
        """Map a free-text crop name in any supported language to a crop."""
        if not text:
            return None
        key = normalize(text)
        if key in self._alias_index:
            return self.crops[self._alias_index[key]]
        # Tolerate plurals and trailing words ("maize seed", "beans 50kg").
        for token in key.split():
            if token in self._alias_index:
                return self.crops[self._alias_index[token]]
            if token.endswith("s") and token[:-1] in self._alias_index:
                return self.crops[self._alias_index[token[:-1]]]
        return None

    def pests_for(self, crop_id: str | None) -> list[Pest]:
        if crop_id and crop_id in self.crops:
            return [self.pests[p] for p in self.crops[crop_id].pests if p in self.pests]
        return list(self.pests.values())

    def match_symptoms(self, text: str, crop_id: str | None = None, limit: int = 3) -> list[tuple[Pest, float]]:
        """Rank pests by keyword overlap with a farmer's symptom description.

        Used offline (no LLM) and for SMS/USSD where there is no photo. Scores are
        in [0, 1]; a score is only meaningful relative to the others.
        """
        norm = f" {normalize(text)} "
        tokens = set(norm.split())
        scored: list[tuple[Pest, float]] = []
        for pest in self.pests_for(crop_id):
            hits = 0.0
            for kw in pest.keywords:
                k = normalize(kw)
                if " " in k:
                    if f" {k} " in norm:
                        hits += 1.5  # multi-word phrases are stronger evidence
                elif k in tokens or (k + "s") in tokens or (k.endswith("s") and k[:-1] in tokens):
                    hits += 1.0
            if hits:
                score = min(1.0, hits / 4.0)
                if crop_id is None or crop_id in pest.crops:
                    score = min(1.0, score + 0.05)
                scored.append((pest, round(score, 2)))
        scored.sort(key=lambda x: (-x[1], x[0].id))
        return scored[:limit]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


@lru_cache
def get_kb() -> KnowledgeBase:
    kb = KnowledgeBase()
    crops_raw = json.loads((_DIR / "crops.json").read_text(encoding="utf-8"))["crops"]
    for c in crops_raw:
        crop = Crop(
            id=c["id"],
            names=c["names"],
            aliases=tuple(c.get("aliases", [])),
            season_days=c["season_days"],
            stages=tuple(Stage(s["id"], s["name"], s["start"], s["end"], tuple(s["tasks"])) for s in c["stages"]),
            pests=tuple(c["pests"]),
            spacing=c["spacing"],
            fertilizer=c["fertilizer"],
            frost_sensitive=c.get("frost_sensitive", True),
            heat_stress_c=c.get("heat_stress_c", 35),
            critical_water_stages=tuple(c.get("critical_water_stages", [])),
            perennial=c.get("perennial", False),
        )
        kb.crops[crop.id] = crop
        for alias in {crop.id, *crop.aliases, *crop.names.values()}:
            kb._alias_index[normalize(alias)] = crop.id
    pests_raw = json.loads((_DIR / "pests.json").read_text(encoding="utf-8"))["pests"]
    for p in pests_raw:
        kb.pests[p["id"]] = Pest(
            id=p["id"],
            type=p["type"],
            crops=tuple(p["crops"]),
            names=p["names"],
            symptoms=p["symptoms"],
            keywords=tuple(p["keywords"]),
            severity=p["severity"],
            spread_risk=p["spread_risk"],
            notifiable=p["notifiable"],
            cultural=tuple(p["cultural"]),
            biological=tuple(p["biological"]),
            chemical=tuple(p["chemical"]),
        )
    return kb
