"""Чтение входных данных: выдельная сеть, лесосеки, ТО (XLSX образца или CSV выгрузки), объёмы заготовки."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import geopandas as gpd
import numpy as np
import pandas as pd

from .. import schema as S

NULLS = {"[NULL]", "NULL", "null", "None", "nan", "NaN", ""}
ID_FIELDS_NETWORK = ["label_name", S.VID, "vydel", "vid", "number", "num", "registration_no", "name"]
ID_FIELDS_LESOSEKI = ["registration_no", "label_name", "Номер_лесосеки", "name", "id"]


def norm_id(v) -> str | None:
    """'1.0' → '1', ' 12 ' → '12', NaN → None."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    s = str(v).strip()
    if s in NULLS:
        return None
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    return s


def to_num(s: pd.Series) -> pd.Series:
    """'1 940' (с неразрывным пробелом), '33,8', '[NULL]' → float."""
    if pd.api.types.is_numeric_dtype(s):
        return s.astype(float)
    t = s.astype("string").str.replace(" ", "", regex=False).str.replace(" ", "", regex=False)
    t = t.str.replace(",", ".", regex=False)
    t = t.where(~t.isin(list(NULLS)))
    return pd.to_numeric(t, errors="coerce").astype(float)


def _clean_obj(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in df.columns:
        if not pd.api.types.is_numeric_dtype(df[c]):
            col = df[c].astype(object)
            col = col.map(lambda x: np.nan if (isinstance(x, str) and x.strip() in NULLS) else x)
            df[c] = col
    return df


def _read_vector(src: str | Path | bytes | BinaryIO) -> gpd.GeoDataFrame:
    if isinstance(src, (bytes, bytearray)):
        src = io.BytesIO(src)
    gdf = gpd.read_file(src)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    gdf = gdf.to_crs(4326)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    gdf["geometry"] = gdf.geometry.make_valid()
    gdf = gdf[gdf.geom_type.isin(["Polygon", "MultiPolygon", "GeometryCollection"])]
    # make_valid может вернуть GeometryCollection: оставляем только площадные части
    gdf["geometry"] = [g if g.geom_type != "GeometryCollection" else _polys_only(g) for g in gdf.geometry]
    return gdf[~gdf.geometry.is_empty].reset_index(drop=True)


def _polys_only(g):
    from shapely.geometry import MultiPolygon
    parts = []
    for p in g.geoms:
        if p.geom_type == "Polygon":
            parts.append(p)
        elif p.geom_type == "MultiPolygon":
            parts.extend(p.geoms)
    return MultiPolygon(parts) if parts else g.__class__()


def read_network(src, id_field: str | None = None) -> gpd.GeoDataFrame:
    """Выдельная сеть → GeoDataFrame[vid, geometry] в WGS84."""
    gdf = _read_vector(src)
    fld = id_field or next((f for f in ID_FIELDS_NETWORK if f in gdf.columns), None)
    if fld is None:
        gdf["vid"] = [str(i + 1) for i in range(len(gdf))]
    else:
        gdf["vid"] = gdf[fld].map(norm_id)
    # одинаковые номера (многоконтурный выдел, разбитый на записи) склеиваем
    if gdf["vid"].duplicated().any():
        gdf = gdf.dissolve(by="vid", as_index=False)
    return gdf[["vid", "geometry"]]


def read_lesoseki(src, id_field: str | None = None) -> gpd.GeoDataFrame:
    gdf = _read_vector(src)
    fld = id_field or next((f for f in ID_FIELDS_LESOSEKI if f in gdf.columns), None)
    gdf["ls_id"] = gdf[fld].map(norm_id) if fld else [f"ЛС{i + 1}" for i in range(len(gdf))]
    return gdf[["ls_id", "geometry"]]


@dataclass
class Taxation:
    """Таксационное описание: три таблицы с колонками как в образце. Ключ — vid (строка)."""

    general: pd.DataFrame                 # индекс vid, колонки schema.GENERAL_COLS (+ доп.)
    layers: pd.DataFrame                  # колонки schema.LAYER_COLS, колонка VID = vid
    undergrowth: pd.DataFrame             # колонки schema.UNDERGROWTH_COLS
    meta: dict = field(default_factory=dict)

    def vids(self) -> list[str]:
        return list(self.general.index)

    def subset(self, vids) -> "Taxation":
        vids = [v for v in vids if v in self.general.index]
        return Taxation(
            self.general.loc[vids].copy(),
            self.layers[self.layers[S.VID].isin(vids)].copy(),
            self.undergrowth[self.undergrowth[S.VID].isin(vids)].copy(),
            dict(self.meta),
        )


def _finalize(general: pd.DataFrame, layers: pd.DataFrame, under: pd.DataFrame, meta: dict | None = None) -> Taxation:
    general = _clean_obj(general)
    layers = _clean_obj(layers)
    under = _clean_obj(under)
    for c in S.GENERAL_NUMERIC:
        if c in general:
            general[c] = to_num(general[c])
    for c in S.LAYER_NUMERIC:
        if c in layers:
            layers[c] = to_num(layers[c])
    for c in S.UNDERGROWTH_NUMERIC:
        if c in under:
            under[c] = to_num(under[c])
    for df in (general, layers, under):
        df[S.VID] = df[S.VID].map(norm_id)
    general = general[general[S.VID].notna()]          # итоговая строка в конце листа «Общие»
    general = general.drop_duplicates(S.VID).set_index(S.VID, drop=False)
    general.index.name = "vid"
    for c in S.GENERAL_COLS:
        if c not in general:
            general[c] = np.nan
    for c in S.LAYER_COLS:
        if c not in layers:
            layers[c] = np.nan
    for c in S.UNDERGROWTH_COLS:
        if c not in under:
            under[c] = np.nan
    # коды держим строками без «.0»
    for df, cols in ((general, [S.DOM_CODE, S.LAND_TYPE_CODE, S.LAND_KIND_CODE, S.BONITET_CODE, S.FOREST_TYPE_CODE]),
                     (layers, [S.S_CODE, S.S_AGE_GROUP_CODE]), (under, [S.U_CODE])):
        for c in cols:
            df[c] = df[c].map(norm_id)
    if S.BONITET in general:
        general[S.BONITET] = general[S.BONITET].map(norm_id)
    return Taxation(general, layers[layers[S.VID].notna()].reset_index(drop=True),
                    under[under[S.VID].notna()].reset_index(drop=True), meta or {})


def read_taxation_xlsx(src) -> Taxation:
    """XLSX как «Пример материалов ТО.xlsx»: листы «Общие», «Ярус насаждения», «Подрост»."""
    if isinstance(src, (bytes, bytearray)):
        src = io.BytesIO(src)
    sheets = pd.read_excel(src, sheet_name=None, dtype=object)
    names = list(sheets)

    def pick(key: str, idx: int) -> pd.DataFrame:
        want = S.SHEETS[key].lower()
        for n in names:
            if n.strip().lower().startswith(want[:5]):
                return sheets[n]
        return sheets[names[idx]] if idx < len(names) else pd.DataFrame(columns=[S.VID])

    general, layers, under = pick("general", 0), pick("layers", 1), pick("undergrowth", 2)
    layers = layers.rename(columns={f"{S.L_HEIGHT}.1": S.L_HEIGHT2})
    return _finalize(general, layers, under, {"source": "xlsx"})


def _read_csv_any(src) -> pd.DataFrame:
    if isinstance(src, (bytes, bytearray)):
        raw = bytes(src)
    else:
        raw = Path(src).read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sep = ";" if text.split("\n", 1)[0].count(";") >= text.split("\n", 1)[0].count(",") else ","
    return pd.read_csv(io.StringIO(text), sep=sep, dtype=str)


def read_taxation_csv(vydel, yarus, podrost=None, key: str = "lesn", quarters: list[str] | None = None) -> Taxation:
    """Выгрузка ТО из трёх CSV (cp1251, «;»).

    key="lesn" — ключ выдела = лесоустроительный номер (подходит внутри одного квартала);
    key="reg"  — ключ = регистрационный номер выдела (уникален по всей выгрузке).
    """
    g, l = _read_csv_any(vydel), _read_csv_any(yarus)
    u = _read_csv_any(podrost) if podrost is not None else pd.DataFrame(columns=[S.VID, S.REG_VID])
    l = l.rename(columns={f"{S.L_HEIGHT}.1": S.L_HEIGHT2})
    if quarters is not None:
        qs = set(quarters)
        g, l = g[g[S.REG_QUARTER].isin(qs)], l[l[S.REG_QUARTER].isin(qs)]
        if S.REG_QUARTER in u:
            u = u[u[S.REG_QUARTER].isin(qs)]
    if key == "reg":
        for df in (g, l, u):
            if S.REG_VID in df:
                df[S.VID] = df[S.REG_VID]
    return _finalize(g, l, u, {"source": "csv", "key": key})


def read_volumes(src) -> pd.DataFrame:
    """Объём заготовленной древесины (образец 5_3). Колонки нормализуются к латинским ключам."""
    df = _read_csv_any(src)
    ren = {
        "Номер_лесосеки": "ls_id", "Дата_окончания_действия": "date", "Номер_выдела": "vydel_reg",
        "Порода_(код)": "species_code", "Сокращение_породы_древесины": "species_abbr",
        "Форма_рубки_(код)": "form_code", "Форма_рубки_(наименование)": "form",
        "Вид_рубки_(код)": "kind_code", "Вид_рубки_(наименование)": "kind",
        "Площадь_рубки_га": "cut_area_ha", "Объем_древесины_куб_м": "volume_m3",
    }
    df = df.rename(columns=ren)
    for c in ("cut_area_ha", "volume_m3"):
        if c in df:
            df[c] = to_num(df[c])
    for c in ("ls_id", "species_code", "vydel_reg"):
        if c in df:
            df[c] = df[c].map(norm_id)
    return df
