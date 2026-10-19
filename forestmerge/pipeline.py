"""Сборка: чтение входов → решатель → обоснования → сериализация для API/экспорта."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import mapping

from . import geometry as G
from . import schema as S
from .explain import llm_rewrite, template
from .io import readers as RD
from .io import writers as WR
from .settings import find_sample
from .solver import Result, run


@dataclass
class Inputs:
    network: gpd.GeoDataFrame
    taxation: RD.Taxation
    lesoseki: gpd.GeoDataFrame
    volumes: pd.DataFrame | None = None
    name: str = ""


def load_inputs(network, taxation, lesoseki, volumes=None, taxation_csv: tuple | None = None, name: str = "") -> Inputs:
    """taxation — XLSX образца; либо taxation_csv=(vydel, yarus, podrost) — выгрузка CSV."""
    net = RD.read_network(network)
    if taxation_csv:
        tax = RD.read_taxation_csv(*taxation_csv)
    elif taxation is not None:
        tax = RD.read_taxation_xlsx(taxation)
    else:
        tax = RD.Taxation(pd.DataFrame(columns=S.GENERAL_COLS).set_index(S.VID, drop=False),
                          pd.DataFrame(columns=S.LAYER_COLS), pd.DataFrame(columns=S.UNDERGROWTH_COLS))
    ls = RD.read_lesoseki(lesoseki)
    vol = RD.read_volumes(volumes) if volumes is not None else None
    return Inputs(net, tax, ls, vol, name)


def load_sample() -> Inputs:
    p = find_sample()
    missing = [k for k in ("network", "taxation", "lesoseki") if p[k] is None]
    if missing:
        raise FileNotFoundError(f"Не найдены файлы образца: {missing}")
    return load_inputs(p["network"], p["taxation"], p["lesoseki"], p["volumes"], name="Образец 5 (кв. 29:28:7:105)")


def solve(inp: Inputs, params: dict | None = None, overrides: dict | None = None, models: Any = None,
          llm: bool = False) -> Result:
    res = run(inp.network, inp.taxation, inp.lesoseki, inp.volumes, params=params, overrides=overrides, models=models)
    for d in res.decisions:
        d["explanation"] = template(d)
        if llm and d["type"] != "tech":
            d["explanation_llm"] = llm_rewrite(d, d["explanation"])
    return res


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def _clean(v):
    if isinstance(v, dict):
        # «features» как словарь — это признаки ML у кандидата (в UI не нужны); список features у GeoJSON оставляем
        return {k: _clean(x) for k, x in v.items()
                if not str(k).startswith("_") and k != "geom" and not (k == "features" and isinstance(x, dict))}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def _fc(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def _feat(geom_wgs, props: dict) -> dict:
    return {"type": "Feature", "geometry": mapping(geom_wgs), "properties": _clean(props)}


def _label_point(g) -> list[float]:
    """Точка подписи внутри самого большого контура (representative_point всегда внутри полигона)."""
    parts = G.polygons(g)
    p = max(parts, key=lambda x: x.area).representative_point() if parts else g.centroid
    return [round(p.x, 6), round(p.y, 6)]


def network_after_gdf(res: Result) -> gpd.GeoDataFrame:
    vids = list(res.units)
    geoms = G.series_to_wgs([res.units[v].geom for v in vids], res.crs)
    gen = res.tables["general"].set_index(S.VID)
    rows = []
    for vid, g in zip(vids, geoms):
        u = res.units[vid]
        r = gen.loc[vid] if vid in gen.index else {}
        rows.append({
            "label_name": vid, "kind": u.kind, "changed": not (u.kind == "orig" and len(u.members) == 1),
            "parents": ",".join(u.parents), "ls_ids": ",".join(u.ls_ids), "flags": ",".join(u.flags),
            "area_ha": round(float(r.get(S.AREA, np.nan)), 4) if len(r) else None,
            "stock_m3": None if not len(r) or pd.isna(r.get(S.STOCK)) else float(r.get(S.STOCK)),
            "land_kind": None if not len(r) or pd.isna(r.get(S.LAND_KIND)) else r.get(S.LAND_KIND),
            "dominant": None if not len(r) or pd.isna(r.get(S.DOM_NAME)) else r.get(S.DOM_NAME),
            "lp": _label_point(g),
            "geometry": g,
        })
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=4326)


def result_json(res: Result, inp: Inputs) -> dict:
    after = network_after_gdf(res)
    before = inp.network.copy()
    gen_b = inp.taxation.general
    before_feats = []
    for vid, g in zip(before.vid, before.geometry):
        r = gen_b.loc[vid] if vid in gen_b.index else None
        before_feats.append(_feat(g, {"label_name": vid,
                                      "land_kind": None if r is None else r.get(S.LAND_KIND),
                                      "dominant": None if r is None else r.get(S.DOM_NAME),
                                      "stock_m3": None if r is None else r.get(S.STOCK),
                                      "area_ha": None if r is None else r.get(S.AREA),
                                      "lp": _label_point(g)}))
    frag_geoms = G.series_to_wgs([f["geom"] for f in res.fragments], res.crs)
    frags = [_feat(g, {k: v for k, v in f.items() if k != "geom"}) for f, g in zip(res.fragments, frag_geoms)]
    ls = [_feat(g, {"ls_id": i}) for i, g in zip(inp.lesoseki.ls_id, inp.lesoseki.geometry)]
    return _clean({
        "name": inp.name,
        "rank": res.rank,
        "metrics": res.metrics,
        "warnings": res.warnings,
        "decisions": res.decisions,
        "layers": {
            "before": _fc(before_feats),
            "lesoseki": _fc(ls),
            "fragments": _fc(frags),
            "after": json.loads(after.to_json()),
        },
        "tables": {
            "before": _records(gen_b.reset_index(drop=True)),
            "after": _records(res.tables["general"]),
            "layers_after": _records(res.tables["layers"]),
            "layers_before": _records(inp.taxation.layers),
        },
        "out_vid": res.out_vid,
    })


def _records(df: pd.DataFrame) -> list[dict]:
    return [_clean(r) for r in df.astype(object).where(df.notna(), None).to_dict("records")]


# ---------------------------------------------------------------------------
# Экспорт
# ---------------------------------------------------------------------------
def export_xlsx(res: Result) -> bytes:
    return WR.taxation_xlsx(res.tables, res.decisions, res.metrics, res.norms["output"]["null_token"])


def export_geojson(res: Result) -> bytes:
    return network_after_gdf(res).drop(columns="lp").to_json(ensure_ascii=False).encode("utf-8")


def save_all(res: Result, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {"xlsx": out / "ТО_после_объединения.xlsx", "geojson": out / "выдела_после.geojson",
             "decisions": out / "журнал_решений.json", "metrics": out / "метрики.json"}
    paths["xlsx"].write_bytes(export_xlsx(res))
    paths["geojson"].write_bytes(export_geojson(res))
    paths["decisions"].write_text(json.dumps(_clean(res.decisions), ensure_ascii=False, indent=1), "utf-8")
    paths["metrics"].write_text(json.dumps(_clean(res.metrics), ensure_ascii=False, indent=1), "utf-8")
    return paths
