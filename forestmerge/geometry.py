"""Геометрия: проекция, наложение лесосек на сеть, классы кусков, граф смежности."""
from __future__ import annotations

import math

import geopandas as gpd
import numpy as np
from pyproj import Geod
from shapely import STRtree
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

GEOD = Geod(ellps="WGS84")
EPS_M2 = 1.0           # куски меньше 1 м² отбрасываем как численный шум overlay


def metric_crs(gdf: gpd.GeoDataFrame):
    return gdf.estimate_utm_crs()


def geodesic_area_ha(geom_wgs84: BaseGeometry) -> float:
    return abs(GEOD.geometry_area_perimeter(geom_wgs84)[0]) / 1e4


def polygons(g: BaseGeometry) -> list[Polygon]:
    if g is None or g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    if g.geom_type in ("MultiPolygon", "GeometryCollection"):
        out = []
        for p in g.geoms:
            out.extend(polygons(p))
        return out
    return []


def as_multi(parts: list[Polygon]) -> BaseGeometry:
    parts = [p for p in parts if not p.is_empty]
    if not parts:
        return Polygon()
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def is_thin(g: BaseGeometry, width_m: float) -> bool:
    """Кусок уже width_m: отрицательный буфер на половину ширины его уничтожает."""
    if width_m <= 0:
        return False
    return g.buffer(-width_m / 2).is_empty


def polsby_popper(g: BaseGeometry) -> float:
    p = g.length
    return 4 * math.pi * g.area / (p * p) if p > 0 else 0.0


def shared_border(a: BaseGeometry, b: BaseGeometry, tol: float = 0.5) -> float:
    """Длина границы a, лежащей в пределах tol от b (м)."""
    try:
        return a.boundary.intersection(b.buffer(tol)).length
    except Exception:  # noqa: BLE001 — топологические исключения GEOS на грязных данных
        return a.boundary.intersection(b.buffer(tol).buffer(0)).length


def resolve_lesoseki_overlaps(ls: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, list[dict]]:
    """Перекрытия лесосек: площадь достаётся первой по порядку номера, у остальных вырезается."""
    ls = ls.sort_values("ls_id").reset_index(drop=True)
    taken = Polygon()
    geoms, warns = [], []
    for _, r in ls.iterrows():
        g = r.geometry
        ov = g.intersection(taken) if not taken.is_empty else Polygon()
        if ov.area > EPS_M2:
            warns.append({"ls_id": r.ls_id, "overlap_ha": ov.area / 1e4})
            g = g.difference(taken)
        geoms.append(g)
        taken = unary_union([taken, r.geometry]) if not taken.is_empty else r.geometry
    out = ls.copy()
    out["geometry"] = geoms
    return out[~out.geometry.is_empty], warns


def overlay_pieces(net: gpd.GeoDataFrame, ls: gpd.GeoDataFrame) -> tuple[list[dict], set[str]]:
    """Режет затронутые выделы лесосеками.

    Возвращает куски (single-part полигоны): {vid, zone: in|out, ls_id, geom, created}
    и множество затронутых номеров. created=False для внешнего куска, совпадающего с исходным
    контуром выдела (например, отдельная часть многоконтурного выдела, которую рубка не задела).
    """
    tree = STRtree(list(ls.geometry))
    ls_geoms = list(ls.geometry)
    ls_ids = list(ls.ls_id)
    pieces: list[dict] = []
    touched: set[str] = set()
    for _, row in net.iterrows():
        vg = row.geometry
        idx = [i for i in tree.query(vg) if vg.intersection(ls_geoms[i]).area > EPS_M2]
        if not idx:
            continue
        touched.add(row.vid)
        cut_union = unary_union([ls_geoms[i] for i in idx])
        for i in idx:
            for p in polygons(vg.intersection(ls_geoms[i])):
                if p.area > EPS_M2:
                    pieces.append({"vid": row.vid, "zone": "in", "ls_id": ls_ids[i], "geom": p, "created": True})
        orig_parts = polygons(vg)
        for p in polygons(vg.difference(cut_union)):
            if p.area <= EPS_M2:
                continue
            same = any(abs(p.area - o.area) < EPS_M2 and p.symmetric_difference(o).area < EPS_M2 for o in orig_parts)
            pieces.append({"vid": row.vid, "zone": "out", "ls_id": None, "geom": p, "created": not same})
    return pieces, touched


class Adjacency:
    """Граф смежности единиц (кусков/выделов) с длиной общей границы."""

    def __init__(self, geoms: dict[str, BaseGeometry], tol: float = 0.5):
        self.tol = tol
        self.nb: dict[str, dict[str, float]] = {k: {} for k in geoms}
        keys = list(geoms)
        tree = STRtree([geoms[k] for k in keys])
        for i, k in enumerate(keys):
            g = geoms[k]
            for j in tree.query(g.buffer(tol)):
                if j <= i:
                    continue
                k2 = keys[j]
                L = shared_border(g, geoms[k2], tol)
                if L > tol * 2:
                    self.nb[k][k2] = L
                    self.nb[k2][k] = L

    def copy(self) -> "Adjacency":
        new = Adjacency.__new__(Adjacency)
        new.tol = self.tol
        new.nb = {k: dict(v) for k, v in self.nb.items()}
        return new

    def neighbors(self, k: str) -> dict[str, float]:
        return self.nb.get(k, {})

    def merge(self, keep: str, gone: str) -> None:
        """gone присоединён к keep: соседи складываются, общая граница исчезает."""
        for n, L in self.nb.pop(gone, {}).items():
            if n == keep:
                continue
            self.nb[n].pop(gone, None)
            self.nb[keep][n] = self.nb[keep].get(n, 0.0) + L
            self.nb[n][keep] = self.nb[keep][n]
        self.nb[keep].pop(gone, None)
        for n in list(self.nb):
            self.nb[n].pop(gone, None)


def to_wgs(geom: BaseGeometry, crs) -> BaseGeometry:
    return gpd.GeoSeries([geom], crs=crs).to_crs(4326).iloc[0]


def series_to_wgs(geoms: list[BaseGeometry], crs) -> list[BaseGeometry]:
    if not geoms:
        return []
    return list(gpd.GeoSeries(geoms, crs=crs).to_crs(4326))


def geodesic_areas_ha(geoms_metric: list[BaseGeometry], crs) -> list[float]:
    return [geodesic_area_ha(g) for g in series_to_wgs(geoms_metric, crs)]


__all__ = ["metric_crs", "overlay_pieces", "Adjacency", "is_thin", "polsby_popper", "polygons", "as_multi",
           "resolve_lesoseki_overlaps", "geodesic_area_ha", "series_to_wgs", "geodesic_areas_ha", "np"]
