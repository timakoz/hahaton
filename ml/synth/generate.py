"""Синтетические сценарии рубок для обучения ранжировщика M3.

Атрибуты выделов берём из реальных кварталов выгрузки ТО (реальные сочетания пород, возрастов,
бонитетов внутри квартала). Геометрию делаем двумя способами:
  * диаграмма Вороного с кластерными центрами (размеры выделов неравномерны, как в жизни);
  * --network-file: реальная выдельная сеть (например «Выделы под лесосеками.geojson»), атрибуты
    назначаются из пула ТО того же региона по рангу площади.
Лесосека = выбранные спелые выделы (целиком, частично или реальной формой из Лесосеки-1/2) + шум
границы (буфер ±, сдвиг, поворот). Так рождаются и технологические осколки, и мелкие остатки.

Метка — учитель из solver (_make_teacher): для каждого фрагмента перебираем кандидатов, жадно доигрываем
квартал и сравниваем итоговую цель. Ранжировщик учится повторять этот перебор за один проход.

    python -m ml.synth.generate --n 3000 --workers 16 --out data/synth
    python -m ml.synth.generate --n 20 --workers 1 --out out/smoke_synth     # проверка
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import time
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import affinity
from shapely.geometry import LineString, MultiPoint, Polygon, box
from shapely.ops import split, unary_union, voronoi_diagram

warnings.filterwarnings("ignore")

from ml.common import ROOT, load_processed, quarter_taxation  # noqa: E402
from forestmerge import schema as S  # noqa: E402
from forestmerge.config import load_norms  # noqa: E402
from forestmerge.settings import PROCESSED_DIR, find_geo_extra  # noqa: E402
from forestmerge.solver import run  # noqa: E402

CRS_LOCAL = "EPSG:32637"
ORIGIN = (500000.0, 6840000.0)

_G: dict = {}


# --------------------------------------------------------------------------- геометрия
def voronoi_network(areas_ha: list[float], rng: np.random.Generator) -> list[Polygon]:
    total = sum(areas_ha) * 1e4
    aspect = rng.uniform(1.0, 2.2)
    W = math.sqrt(total / aspect)
    H = total / W
    rect = box(0, 0, W, H)
    n = len(areas_ha)
    k = max(1, n // 6)
    centers = rng.uniform([0, 0], [W, H], size=(k, 2))
    pts = []
    for i in range(n):
        if rng.random() < 0.6:
            c = centers[rng.integers(k)]
            p = c + rng.normal(0, min(W, H) / 6, size=2)
        else:
            p = rng.uniform([0, 0], [W, H])
        pts.append((float(np.clip(p[0], 1, W - 1)), float(np.clip(p[1], 1, H - 1))))
    cells = [c.intersection(rect) for c in voronoi_diagram(MultiPoint(pts), envelope=rect.buffer(10)).geoms]
    cells = [c for c in cells if c.area > 1]
    return [affinity.translate(c, *ORIGIN) for c in cells]


def norm_shapes(paths: list[Path]) -> list[Polygon]:
    shapes = []
    for p in paths:
        if p is None or not Path(p).exists():
            continue
        g = gpd.read_file(p)
        g = g[g.geometry.notna() & ~g.geometry.is_empty]
        for geom in g.geometry:
            try:
                one = gpd.GeoSeries([geom], crs=4326)
                one = one.to_crs(one.estimate_utm_crs()).iloc[0]
            except Exception:  # noqa: BLE001
                continue
            if one.area < 1000:
                continue
            c = one.centroid
            one = affinity.translate(one, -c.x, -c.y)
            one = affinity.scale(one, 1 / math.sqrt(one.area), 1 / math.sqrt(one.area), origin=(0, 0))
            shapes.append(one.buffer(0))
    return shapes


def noisy(g: Polygon, rng: np.random.Generator) -> Polygon:
    d1, d2 = rng.uniform(0, 25), rng.uniform(0, 25)
    g = g.buffer(d1, join_style="mitre").buffer(-d2, join_style="mitre")
    g = affinity.translate(g, *rng.normal(0, 8, size=2))
    g = affinity.rotate(g, rng.normal(0, 1.0), origin="centroid")
    return g.simplify(rng.uniform(0.5, 4))


def make_lesoseki(cells: list[Polygon], stands_q: pd.DataFrame, shapes: list[Polygon], rng: np.random.Generator):
    """Выбор целей рубки и построение лесосек."""
    w = []
    for _, s in stands_q.iterrows():
        wt = 0.05
        if s["merge_class"] == "stand":
            wt = 1.0 + (3.0 if s["age_kind"] == "mature" else 0) + min(3.0, (s["stock_ha"] or 0) / 100)
        w.append(wt)
    w = np.array(w[:len(cells)])
    k = int(rng.choice([1, 2, 3], p=[0.55, 0.32, 0.13]))
    idx = rng.choice(len(w), size=min(k, len(w)), replace=False, p=w / w.sum())
    quarter = unary_union(cells)
    out = []
    for j, i in enumerate(idx):
        cell = cells[i]
        mode = rng.choice(["whole", "partial", "shape"], p=[0.35, 0.45, 0.20] if shapes else [0.45, 0.55, 0.0])
        if mode == "whole":
            g = cell
        elif mode == "partial":
            c = cell.centroid
            ang = rng.uniform(0, math.pi)
            off = rng.normal(0, math.sqrt(cell.area) / 6)
            dx, dy = math.cos(ang), math.sin(ang)
            L = 1e4
            line = LineString([(c.x - dx * L - dy * off, c.y - dy * L + dx * off),
                               (c.x + dx * L - dy * off, c.y + dy * L + dx * off)])
            parts = sorted(split(cell, line).geoms, key=lambda p: -p.area)
            g = parts[0] if parts else cell
        else:
            s = shapes[rng.integers(len(shapes))]
            target = cell.area * rng.uniform(0.5, 1.3)
            g = affinity.scale(s, math.sqrt(target), math.sqrt(target), origin=(0, 0))
            g = affinity.rotate(g, rng.uniform(0, 360), origin=(0, 0))
            c = cell.centroid
            g = affinity.translate(g, c.x, c.y)
        g = noisy(g, rng).intersection(quarter)
        if g.area > 2000:
            out.append((f"ЛС{j + 1}", g))
    return out


# --------------------------------------------------------------------------- сценарий
def _init(proc_dir: str, network_file: str | None, shape_paths: list[str]):
    _G["data"] = load_processed(Path(proc_dir))
    st = pd.read_parquet(Path(proc_dir) / "stands.parquet")
    _G["stands"] = st.set_index("vid")
    cnt = st.groupby("quarter").size()
    good = st[(st.merge_class == "stand")].groupby("quarter").size()
    qs = [q for q in cnt.index if cnt[q] >= 6 and good.get(q, 0) >= 3 and cnt[q] <= 80]
    _G["quarters"] = qs
    _G["shapes"] = norm_shapes([Path(p) for p in shape_paths])
    _G["norms"] = load_norms()
    _G["real_net"] = None
    if network_file:
        net = gpd.read_file(network_file)
        net = net[net.geometry.notna()].to_crs(CRS_LOCAL)
        key = "registration_no" if "registration_no" in net else net.columns[0]
        net["quarter"] = net[key].astype(str).str.rsplit(":", n=1).str[0]
        _G["real_net"] = {q: list(g.geometry) for q, g in net.groupby("quarter") if 6 <= len(g) <= 80}


def scenario(seed: int) -> list[dict]:
    rng = np.random.default_rng(seed)
    data = _G["data"]
    q = _G["quarters"][rng.integers(len(_G["quarters"]))]
    tax, mapping = quarter_taxation(data, q)
    inv = {v: k for k, v in mapping.items()}
    stands_q = _G["stands"].loc[[inv[v] for v in tax.general.index]].reset_index()
    areas = [max(0.3, float(a)) for a in stands_q["area_ha"].fillna(1.0)]
    if _G["real_net"] and rng.random() < 0.7:
        rq = list(_G["real_net"])
        cells = list(_G["real_net"][rq[rng.integers(len(rq))]])
    else:
        cells = voronoi_network(areas, rng)
    order_s = np.argsort(-np.array(areas))[:len(cells)]
    order_c = np.argsort([-c.area for c in cells])[:len(order_s)]
    vids = list(tax.general.index)
    geoms, vlist = [], []
    for si, ci in zip(order_s, order_c):
        geoms.append(cells[ci])
        vlist.append(vids[si])
    tax = tax.subset(vlist)
    # площадь ТО → площадь ячейки (удельные показатели сохраняем)
    for vid, g in zip(vlist, geoms):
        a0 = float(tax.general.at[vid, S.AREA] or 0) or 1.0
        k = (g.area / 1e4) / a0
        for c in S.GENERAL_TOTALS:
            v = pd.to_numeric(tax.general.at[vid, c], errors="coerce")
            if pd.notna(v):
                tax.general.at[vid, c] = float(v) * k
        tax.general.at[vid, S.AREA] = g.area / 1e4
    net = gpd.GeoDataFrame({"vid": vlist}, geometry=geoms, crs=CRS_LOCAL).to_crs(4326)
    st_q = stands_q.iloc[order_s].reset_index(drop=True)
    ls = make_lesoseki(geoms, st_q, _G["shapes"], rng)
    if not ls:
        return []
    lsg = gpd.GeoDataFrame({"ls_id": [x[0] for x in ls]}, geometry=[x[1] for x in ls], crs=CRS_LOCAL).to_crs(4326)
    volumes = None
    if rng.random() < 0.15:
        rows = []
        for lid, g in ls:
            for vid, cg in zip(vlist, geoms):
                inter = cg.intersection(g).area / 1e4
                if inter <= 0:
                    continue
                st = tax.layers[tax.layers[S.VID] == vid]
                for _, r in st.iterrows():
                    sh = pd.to_numeric(r[S.S_SHARE], errors="coerce")
                    ls_ha = pd.to_numeric(r[S.L_STOCK_HA], errors="coerce")
                    if pd.notna(sh) and pd.notna(ls_ha):
                        rows.append({"ls_id": lid, "species_code": r[S.S_CODE],
                                     "volume_m3": sh / 10 * ls_ha * inter * rng.uniform(0.15, 0.4),
                                     "form": "Выборочная рубка"})
        volumes = pd.DataFrame(rows) if rows else None
    params = {
        "rank": int(rng.choice([1, 2, 3], p=[0.3, 0.5, 0.2])),
        "tolerances": {"method": str(rng.choice(["глазомерный", "глазомерно-измерительный", "актуализация"],
                                                p=[0.6, 0.25, 0.15]))},
    }
    try:
        res = run(net, tax, lsg, volumes, params=params, teacher=True)
    except Exception as e:  # noqa: BLE001  — битые сочетания геометрии пропускаем
        return [{"_error": repr(e)[:200], "seed": seed}]
    out = []
    for r in res.training_rows:
        r = dict(r)
        r["group"] = f"{seed}-{r['group']}"
        r["scenario"] = seed
        r["quarter"] = q
        r["region"] = str(tax.general[S.REGION].iloc[0]) if S.REGION in tax.general else ""
        out.append(r)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000, help="число сценариев")
    ap.add_argument("--workers", type=int, default=max(1, mp.cpu_count() - 1))
    ap.add_argument("--out", default=str(ROOT / "data" / "synth"))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--chunk", type=int, default=200)
    ap.add_argument("--network-file", default=None, help="реальная сеть (Выделы под лесосеками.geojson)")
    a = ap.parse_args()
    extra = find_geo_extra()
    shape_paths = [str(p) for p in (extra["lesoseki1"], extra["lesoseki2"]) if p]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    seeds = list(range(a.seed * 1_000_000, a.seed * 1_000_000 + a.n))
    t0 = time.time()
    n_rows = n_err = 0
    init_args = (str(PROCESSED_DIR), a.network_file, shape_paths)
    if a.workers > 1:
        pool = mp.Pool(a.workers, initializer=_init, initargs=init_args)
        it = pool.imap_unordered(scenario, seeds, chunksize=4)
    else:
        _init(*init_args)
        it = map(scenario, seeds)
    buf, part = [], 0
    for i, rows in enumerate(it, 1):
        errs = [r for r in rows if "_error" in r]
        n_err += len(errs)
        buf += [r for r in rows if "_error" not in r]
        if i % a.chunk == 0 or i == len(seeds):
            if buf:
                pd.DataFrame(buf).to_parquet(out / f"part-{a.seed:03d}-{part:04d}.parquet", index=False)
                n_rows += len(buf)
                part += 1
                buf = []
            print(f"{i}/{len(seeds)} сценариев, строк {n_rows}, ошибок {n_err}, {time.time() - t0:.0f} с", flush=True)
    if a.workers > 1:
        pool.close()
    (out / f"summary-{a.seed:03d}.json").write_text(json.dumps(
        {"scenarios": a.n, "rows": n_rows, "errors": n_err, "seconds": round(time.time() - t0)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
