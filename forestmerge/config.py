"""Загрузка профилей норм (YAML) и справочника пород."""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import yaml

from .settings import CONFIG_DIR


def deep_merge(base: dict, over: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@lru_cache(maxsize=8)
def _load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def list_profiles() -> list[str]:
    return sorted(p.stem for p in CONFIG_DIR.glob("norms_*.yaml"))


def load_norms(profile: str = "norms_2022", overrides: dict | None = None) -> dict:
    """Профиль норм с пользовательскими переопределениями. Ключи min_area_ha/rank приводятся к int."""
    norms = deep_merge(_load_yaml(f"{profile}.yaml"), overrides)
    norms["min_area_ha"] = {int(k): v for k, v in norms["min_area_ha"].items()}
    norms["avg_area_by_rank"] = {int(k): v for k, v in norms["avg_area_by_rank"].items()}
    if norms.get("rank") is not None:
        norms["rank"] = int(norms["rank"])
    return norms


def tolerance(norms: dict) -> dict:
    t = norms["tolerances"]
    return {**t["table"][t["method"]], **t["small_values"]}


@dataclass
class SpeciesInfo:
    code: str
    name: str
    abbr: str
    group: str
    age_class_years: int
    maturity_age: int


class SpeciesBook:
    """Справочник пород: код ↔ сокращение ↔ название, классы возраста, группы возраста."""

    def __init__(self, doc: dict):
        self.by_code: dict[str, SpeciesInfo] = {}
        for code, d in doc["species"].items():
            self.by_code[str(code)] = SpeciesInfo(
                str(code), d["name"], d["abbr"], d["group"], int(d["age_class_years"]), int(d["maturity_age"])
            )
        self.by_abbr = {s.abbr: s for s in self.by_code.values()}
        self.by_name = {s.name.lower(): s for s in self.by_code.values()}
        ag = doc.get("age_groups", {})
        self.young_classes = int(ag.get("young_classes", 2))
        self.mature_classes = ag.get("mature_classes", {"conifer": 2, "hard": 2, "soft": 2, "shrub": 1})

    def get(self, code: Any) -> SpeciesInfo | None:
        if code is None:
            return None
        key = str(code).split(".")[0]
        return self.by_code.get(key)

    def abbr(self, code: Any, name: str | None = None) -> str:
        s = self.get(code)
        if s:
            return s.abbr
        if name:
            s = self.by_name.get(str(name).lower())
            if s:
                return s.abbr
            return str(name)[:3].upper()
        return "?"

    def class_len(self, code: Any) -> int:
        s = self.get(code)
        return s.age_class_years if s else 20

    def age_class(self, code: Any, age: float | None) -> int | None:
        if age is None or (isinstance(age, float) and math.isnan(age)) or age <= 0:
            return None
        return int(math.ceil(age / self.class_len(code)))

    def age_group(self, code: Any, age: float | None) -> str | None:
        """Группа возраста по классу возраста и возрасту спелости (приложение 8 ЛИ упрощённо)."""
        s = self.get(code)
        cls = self.age_class(code, age)
        if s is None or cls is None:
            return None
        mature_cls = int(math.ceil(s.maturity_age / s.age_class_years))
        n_mature = int(self.mature_classes.get(s.group, 2))
        if cls <= self.young_classes and cls < mature_cls - 1:
            return "молодняки"
        if cls < mature_cls - 1:
            return "средневозрастные"
        if cls == mature_cls - 1:
            return "приспевающие"
        if cls < mature_cls + n_mature:
            return "спелые"
        return "перестойные"


@lru_cache(maxsize=1)
def load_species() -> SpeciesBook:
    return SpeciesBook(_load_yaml("species.yaml"))


AGE_GROUP_KIND = {
    "молодняки": "young",
    "средневозрастные": "mid",
    "приспевающие": "mid",
    "спелые": "mature",
    "перестойные": "mature",
}


def age_group_kind(name: str | None) -> str | None:
    if not name or not isinstance(name, str):
        return None
    n = name.lower()
    if n.startswith("молодняк"):
        return "young"
    if n.startswith("спел") or n.startswith("перест"):
        return "mature"
    if n.startswith("средн") or n.startswith("присп"):
        return "mid"
    return None
