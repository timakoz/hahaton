"""Бэктест на реальных сетях ФГИС ЛК (раздел 2.1–2.2 PLAN.md).

    python3 -m ml.backtest.run --network "Выделы под лесосеками.geojson" --lesoseki "Лесосеки-2.geojson" \
                              [--lesoseki "Лесосеки-1.geojson"] [--max-quarters 300] --out out/backtest

1) Базовая линия ФГИС: доля выделов < 0.1 / 0.5 / 1 га и многоконтурных среди выделов, пересекающих
   лесосеки («сейчас 28 % новых выделов < 0.1 га»).
2) Наш конвейер на кварталах, где ТО «до» восстанавливается (в квартале заменён ровно один выдел:
   старый выдел = объединение новых, пересекающих лесосеку). Сравниваем число мелких выделов.

ВНИМАНИЕ: файлы ФГИС в репозитории отсутствуют — скрипт не прогонялся на них; эвристика
реконструкции взята из PLAN.md 2.1 и требует проверки на данных.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from ml.common import load_processed, quarter_taxation
from forestmerge import schema as S
from forestmerge.geometry import geodesic_area_ha, polygons
from forestmerge.io.readers import Taxation
from forestmerge.solver import run


def fgis_baseline(net: gpd.GeoDataFrame, ls: gpd.GeoDataFrame) -> dict:
    hit = gpd.sjoin(net, ls[["geometry"]], predicate="intersects", how="inner").index.unique()
    sub = net.loc[hit]
    a = np.array([geodesic_area_ha(g) for g in sub.geometry])
    multi = np.array([len(polygons(g)) > 1 for g in sub.geometry])
    return {"n_vydels_touching_cuts": int(len(sub)), "lt_0_1": round(float((a < 0.1).mean()), 4),
            "lt_0_5": round(float((a < 0.5).mean()), 4), "lt_1": round(float((a < 1).mean()), 4),
            "multipart": round(float(multi.mean()), 4)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", required=True)
    ap.add_argument("--lesoseki", action="append", required=True)
    ap.add_argument("--max-quarters", type=int, default=300)
    ap.add_argument("--rank", type=int, default=2)
    ap.add_argument("--out", default="out/backtest")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    net = gpd.read_file(a.network)
    net = net[net.geometry.notna() & ~net.geometry.is_empty].to_crs(4326)
    bounds_ok = net.geometry.bounds
    net = net[(bounds_ok.minx > 19) & (bounds_ok.maxx < 191) & (bounds_ok.miny > 40)]   # битые координаты
    key = "registration_no" if "registration_no" in net else net.columns[0]
    net["reg"] = net[key].astype(str)
    net["quarter"] = net["reg"].str.rsplit(":", n=1).str[0]
    net["num"] = net["reg"].str.rsplit(":", n=1).str[1]
    ls = pd.concat([gpd.read_file(p) for p in a.lesoseki], ignore_index=True).to_crs(4326)
    ls = ls[ls.geometry.notna() & ~ls.geometry.is_empty]
    lk = "registration_no" if "registration_no" in ls else ls.columns[0]
    ls["ls_id"] = ls[lk].astype(str)
    ls["quarter"] = ls["ls_id"].str.rsplit(":", n=1).str[0]
    report = {"fgis": fgis_baseline(net, ls)}
    print("ФГИС:", report["fgis"])

    data = load_processed()
    to_q = data["general"].groupby(S.REG_QUARTER).size()
    rows = []
    for q, gq in net.groupby("quarter"):
        if len(rows) >= a.max_quarters:
            break
        if to_q.get(q, 0) != 1:              # заменён ровно один выдел
            continue
        lq = ls[ls["quarter"] == q]
        if lq.empty:
            continue
        tax, mapping = quarter_taxation(data, q, rename=False)
        old_vid = next(iter(mapping))
        touched = gpd.sjoin(gq, lq[["geometry"]], predicate="intersects", how="inner").index.unique()
        new = gq.loc[touched]
        before = pd.concat([gq.drop(index=touched)[["reg", "geometry"]],
                            gpd.GeoDataFrame({"reg": [old_vid]}, geometry=[new.union_all()], crs=4326)])
        before = gpd.GeoDataFrame(before.rename(columns={"reg": "vid"}), geometry="geometry", crs=4326)
        try:
            res = run(before.reset_index(drop=True), tax, lq[["ls_id", "geometry"]].reset_index(drop=True),
                      params={"rank": a.rank, "rules": {"compat_fallback": {"stand": ["open", "unknown"]}}})
        except Exception as e:  # noqa: BLE001
            rows.append({"quarter": q, "error": repr(e)[:200]})
            continue
        fg_small = int(sum(geodesic_area_ha(g) < 0.1 for g in new.geometry))
        rows.append({"quarter": q, "fgis_new_vydels": int(len(new)), "fgis_lt_0_1": fg_small,
                     "ours_vydels": res.metrics["n_vydels_after"], "ours_small_after": res.metrics["n_small_after"],
                     "ours_tech": res.metrics["n_tech_slivers"], "ours_merges": res.metrics["n_merges"]})
    df = pd.DataFrame(rows)
    df.to_csv(out / "quarters.csv", index=False, encoding="utf-8-sig")
    ok = df[df.get("error").isna()] if "error" in df else df
    report["ours"] = {"quarters": int(len(ok)), "errors": int(len(df) - len(ok)),
                      "fgis_lt_0_1_total": int(ok["fgis_lt_0_1"].sum()) if len(ok) else 0,
                      "ours_small_after_total": int(ok["ours_small_after"].sum()) if len(ok) else 0}
    report["seconds"] = round(time.time() - t0)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
