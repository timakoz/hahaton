"""Шаг 0. Большие CSV ТО → очищенные parquet + таблица признаков выделов.

    python -m ml.prepare_data [--vydel CSV --yarus CSV --podrost CSV] [--out data/processed]

Выход:
  general.parquet / layers.parquet / undergrowth.parquet — колонки как в образце, ключ = рег. номер выдела
  stands.parquet — плоские признаки выдела (для анализа, M1, отбора кварталов в синтетике)
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from ml.common import ROOT, iter_stands  # noqa: F401  (ROOT добавляет корень в sys.path)
from forestmerge import schema as S
from forestmerge.config import load_species
from forestmerge.io.readers import read_taxation_csv
from forestmerge.settings import PROCESSED_DIR, find_big_taxation


def _to_parquet(df: pd.DataFrame, path: Path) -> None:
    df = df.copy()
    for c in df.columns:
        if df[c].dtype == object or str(df[c].dtype).startswith("string") or str(df[c].dtype) == "str":
            df[c] = df[c].map(lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) else str(v))
    df.to_parquet(path, index=False)


def stand_features(vid: str, st, species) -> dict:
    comp = st.composition()
    top = sorted(comp.items(), key=lambda kv: -kv[1])[:3]
    dom = st.dominant_code()
    return {
        "vid": vid,
        "region": st.general.get(S.REGION),
        "quarter": st.general.get(S.REG_QUARTER),
        "area_ha": st.area_ha,
        "stock_m3": st.stock_total(),
        "stock_ha": st.stock_ha(),
        "merge_class": st.merge_class(),
        "area_category": st.area_category(),
        "origin": st.origin(),
        "dominant": dom,
        "dominant_share": comp.get(dom) if dom else None,
        "age": st.dom_age(), "height": st.dom_height(), "diameter": st.dom_diameter(), "density": st.density(),
        "bonitet": st.bonitet_num(), "tlu": st.tlu(), "forest_type": st.forest_type(),
        "age_kind": st.age_kind(species), "n_layers": len(st.layer_nums()), "n_species": len(comp),
        **{f"sp{i + 1}": (top[i][0] if i < len(top) else None) for i in range(3)},
        **{f"sp{i + 1}_share": (top[i][1] if i < len(top) else None) for i in range(3)},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    found = find_big_taxation()
    ap.add_argument("--vydel", type=Path, default=found["vydel"])
    ap.add_argument("--yarus", type=Path, default=found["yarus"])
    ap.add_argument("--podrost", type=Path, default=found["podrost"])
    ap.add_argument("--out", type=Path, default=PROCESSED_DIR)
    a = ap.parse_args()
    if not (a.vydel and a.yarus):
        raise SystemExit("Не найдены CSV ТО — укажите --vydel/--yarus/--podrost")
    a.out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    tax = read_taxation_csv(a.vydel, a.yarus, a.podrost, key="reg")
    print(f"прочитано: {len(tax.general)} выделов, {len(tax.layers)} строк ярусов, {len(tax.undergrowth)} подроста "
          f"за {time.time() - t0:.1f} с")
    _to_parquet(tax.general.reset_index(drop=True), a.out / "general.parquet")
    _to_parquet(tax.layers, a.out / "layers.parquet")
    _to_parquet(tax.undergrowth, a.out / "undergrowth.parquet")

    from ml.common import load_processed
    data = load_processed(a.out)
    species = load_species()
    rows = [stand_features(vid, st, species) for vid, st in iter_stands(data)]
    feats = pd.DataFrame(rows)
    _to_parquet(feats, a.out / "stands.parquet")
    summary = {
        "n_vydels": len(feats), "n_quarters": int(feats["quarter"].nunique()), "n_regions": int(feats["region"].nunique()),
        "merge_class": feats["merge_class"].value_counts().to_dict(),
        "missing_share": {c: round(float(feats[c].isna().mean()), 3) for c in
                          ("age", "height", "diameter", "density", "bonitet", "tlu", "stock_ha")},
        "elapsed_s": round(time.time() - t0, 1),
    }
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
