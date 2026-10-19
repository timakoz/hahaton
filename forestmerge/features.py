"""Признаки пары «фрагмент × кандидат». Одна функция для решателя (инференс M3) и для синтетики (обучение)."""
from __future__ import annotations

import math

from .rules import Check

TERMS = ["composition", "dominant_species", "age", "age_group", "density", "bonitet", "diameter", "tlu",
         "forest_type", "origin", "stock", "land"]
CLASSES = ["stand", "open", "nonforest", "linear", "unknown"]

FEATURES = (
    [f"d_{t}" for t in TERMS] + [f"has_{t}" for t in TERMS]
    + ["distance", "n_separation", "n_viol", "viol_stock", "viol_coef", "viol_density", "viol_dominant",
       "viol_age_group", "tol_stock_ratio", "tol_coef_ratio", "tol_density_ratio",
       "shared_frac", "shared_len_log", "frag_area_log", "cand_area_log", "area_ratio", "cand_is_largest",
       "frag_min_ratio", "pp_frag", "pp_merged", "pp_delta", "same_parent", "cand_created", "cand_small",
       "fallback", "rank", "frag_cut_kind", "cand_stock_ha", "frag_stock_ha", "emb_cos", "n_candidates"]
    + [f"fcls_{c}" for c in CLASSES] + [f"ccls_{c}" for c in CLASSES]
)


def _log(v: float) -> float:
    return math.log1p(max(v, 0.0))


def pair_features(*, terms: dict, dist: float, checks: list[Check], shared_len: float, frag_perimeter: float,
                  frag_area: float, cand_area: float, largest_nb_area: float, min_area: float, pp_frag: float,
                  pp_merged: float, same_parent: bool, cand_created: bool, cand_small: bool, fallback: bool,
                  rank: int, frag_kind: str, frag_cls: str, cand_cls: str, cand_stock_ha: float | None,
                  frag_stock_ha: float | None, emb_cos: float | None, n_candidates: int) -> dict[str, float]:
    f: dict[str, float] = {}
    for t in TERMS:
        v = terms.get(t)
        f[f"d_{t}"] = float(min(v, 5.0)) if v is not None else -1.0
        f[f"has_{t}"] = 0.0 if v is None else 1.0
    f["distance"] = dist
    f["n_separation"] = float(sum(1 for v in terms.values() if v is not None and v >= 1.0))
    by = {c.code: c for c in checks}
    f["n_viol"] = float(sum(1 for c in checks if not c.ok))
    for code in ("stock", "coef", "density", "dominant", "age_group"):
        f[f"viol_{code}"] = 1.0 if code in by and not by[code].ok else 0.0
    for code in ("stock", "coef", "density"):
        c = by.get(code)
        f[f"tol_{code}_ratio"] = (c.value / c.limit if c and c.limit else 0.0) if c and c.value is not None else -1.0
    f["shared_frac"] = shared_len / frag_perimeter if frag_perimeter > 0 else 0.0
    f["shared_len_log"] = _log(shared_len)
    f["frag_area_log"] = _log(frag_area * 100)
    f["cand_area_log"] = _log(cand_area * 100)
    f["area_ratio"] = frag_area / cand_area if cand_area > 0 else 0.0
    f["cand_is_largest"] = 1.0 if largest_nb_area > 0 and cand_area >= largest_nb_area - 1e-9 else 0.0
    f["frag_min_ratio"] = frag_area / min_area if min_area > 0 else 0.0
    f["pp_frag"], f["pp_merged"] = pp_frag, pp_merged
    f["pp_delta"] = pp_merged - pp_frag
    f["same_parent"] = 1.0 if same_parent else 0.0
    f["cand_created"] = 1.0 if cand_created else 0.0
    f["cand_small"] = 1.0 if cand_small else 0.0
    f["fallback"] = 1.0 if fallback else 0.0
    f["rank"] = float(rank)
    f["frag_cut_kind"] = {"remnant": 0.0, "cut": 1.0, "thinned": 2.0}.get(frag_kind, 3.0)
    f["cand_stock_ha"] = cand_stock_ha if cand_stock_ha is not None else -1.0
    f["frag_stock_ha"] = frag_stock_ha if frag_stock_ha is not None else -1.0
    f["emb_cos"] = emb_cos if emb_cos is not None else -2.0
    f["n_candidates"] = float(n_candidates)
    for c in CLASSES:
        f[f"fcls_{c}"] = 1.0 if frag_cls == c else 0.0
        f[f"ccls_{c}"] = 1.0 if cand_cls == c else 0.0
    return f
