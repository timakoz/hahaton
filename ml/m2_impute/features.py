"""Признаки строки «порода × ярус» для M2. Используются и при обучении, и в ml_hooks при инференсе."""
from __future__ import annotations

import math
import re

from forestmerge import schema as S
from forestmerge.stand import BONITET_NUM, num, txt

TARGETS = {"height": S.S_HEIGHT, "diameter": S.S_DIAM, "age": S.S_AGE}

CAT_FEATURES = ["species", "region", "tlu", "forest_type", "bonitet", "land_kind", "econ_cat", "layer", "is_dom"]
NUM_FEATURES = ["share", "layer_height", "layer_stock_ha", "density", "plant_year", "survey_year", "years_since_plant",
                "bonitet_num", "height_known", "diameter_known", "age_known"]
FEATURES = CAT_FEATURES + NUM_FEATURES


def _year(v) -> float | None:
    s = txt(v)
    if not s:
        return None
    m = re.search(r"(1[89]\d\d|20\d\d)", s)
    return float(m.group(1)) if m else None


def _cat(v, top: set | None) -> str:
    s = txt(v)
    if s is None:
        return "NA"
    return s if top is None or s in top else "OTHER"


def row_features(general: dict, row: dict, meta: dict | None = None, drop_target: str | None = None) -> list:
    """Список значений в порядке FEATURES. drop_target — целевую величину не подставлять в *_known."""
    tops = {k: set(v) for k, v in (meta or {}).get("top_categories", {}).items()}
    py = num(general.get(S.PLANT_YEAR))
    sy = _year(general.get("Дата лесоустройства"))
    b = txt(general.get(S.BONITET))
    known = {}
    for name, col in TARGETS.items():
        known[name] = math.nan if name == drop_target else (num(row.get(col)) if num(row.get(col)) is not None else math.nan)
    f = {
        "species": _cat(row.get(S.S_CODE), tops.get("species")),
        "region": _cat(general.get(S.REGION), tops.get("region")),
        "tlu": _cat(general.get(S.TLU), tops.get("tlu")),
        "forest_type": _cat(general.get(S.FOREST_TYPE), tops.get("forest_type")),
        "bonitet": _cat(b, None),
        "land_kind": _cat(general.get(S.LAND_KIND), tops.get("land_kind")),
        "econ_cat": _cat(general.get(S.ECON_CAT), None),
        "layer": _cat(row.get(S.L_NUM), None),
        "is_dom": "Y" if str(row.get(S.S_DOM)).upper() == "Y" else "N",
        "share": num(row.get(S.S_SHARE)),
        "layer_height": num(row.get(S.L_HEIGHT)),
        "layer_stock_ha": num(row.get(S.L_STOCK_HA)),
        "density": num(row.get(S.L_DENSITY)),
        "plant_year": py,
        "survey_year": sy,
        "years_since_plant": (sy - py) if (sy and py) else None,
        "bonitet_num": BONITET_NUM.get(b.lower()) if b else None,
        "height_known": known["height"],
        "diameter_known": known["diameter"],
        "age_known": known["age"],
    }
    return [math.nan if (k in NUM_FEATURES and f[k] is None) else f[k] for k in FEATURES]
