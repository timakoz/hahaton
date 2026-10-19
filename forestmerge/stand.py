"""Stand — таксационная характеристика одного (будущего) выдела и производные признаки.

Значения хранятся строками/числами с теми же именами колонок, что в XLSX образца, поэтому
выход пишется без потерь. Объекты считаются неизменяемыми: пересчёт создаёт новый Stand.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

import numpy as np

from . import schema as S
from .config import SpeciesBook, age_group_kind

BONITET_NUM = {"1б": -1.0, "1а": 0.0, "1": 1.0, "2": 2.0, "3": 3.0, "4": 4.0, "5": 5.0, "5а": 6.0, "5б": 7.0}
BONITET_STR = {v: k for k, v in BONITET_NUM.items()}

LINEAR_KW = ("просек", "дорог", "трасс", "электропередач", "лэп", "канав", "разрыв", "тропа", "граница",
             "ручей", "газопровод", "нефтепровод", "линии связи", "профиль")
AGRI_KW = ("сенокос", "пастбищ", "выгон", "пашн", "сад", "огород", "усадьб")
OPEN_KW = ("вырубк", "лесосек", "гар", "прогалин", "пустыр", "редин", "погибш", "несомкнувш", "питомник",
           "не покрыт")
CULTURE_KW = ("лесные культуры", "культуры лесные", "искусственн", "несомкнувш")


def _nan(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and v.strip() == "")


def num(v) -> float | None:
    if _nan(v):
        return None
    try:
        return float(str(v).replace(",", ".").replace(" ", "").replace(" ", ""))
    except ValueError:
        return None


def txt(v) -> str | None:
    return None if _nan(v) else str(v).strip()


@dataclass
class Stand:
    general: dict
    layers: list[dict] = field(default_factory=list)
    undergrowth: list[dict] = field(default_factory=list)
    area_ha: float = 0.0
    imputed: dict = field(default_factory=dict)   # {(row_idx, col): value} — значения модели M2

    # ---------- конструкторы ----------
    @classmethod
    def from_taxation(cls, tax, vid: str, area_ha: float | None = None) -> "Stand":
        if vid not in tax.general.index:
            return cls.empty(area_ha or 0.0, vid)
        g = tax.general.loc[vid].to_dict()
        lay = tax.layers[tax.layers[S.VID] == vid].to_dict("records")
        und = tax.undergrowth[tax.undergrowth[S.VID] == vid].to_dict("records")
        a = num(g.get(S.AREA))
        return cls(g, lay, und, area_ha if a is None else a)

    @classmethod
    def empty(cls, area_ha: float, vid: str | None = None) -> "Stand":
        g = {c: np.nan for c in S.GENERAL_COLS}
        g[S.VID] = vid
        g[S.AREA] = area_ha
        return cls(g, [], [], area_ha)

    def copy(self) -> "Stand":
        return Stand(copy.deepcopy(self.general), copy.deepcopy(self.layers), copy.deepcopy(self.undergrowth),
                     self.area_ha, dict(self.imputed))

    def scaled(self, new_area: float) -> "Stand":
        """Часть того же выдела: удельные показатели те же, итоговые запасы ∝ площади."""
        st = self.copy()
        k = new_area / self.area_ha if self.area_ha > 0 else 0.0
        for c in S.GENERAL_TOTALS:
            v = num(st.general.get(c))
            if v is not None:
                st.general[c] = v * k
        st.general[S.AREA] = new_area
        st.area_ha = new_area
        return st

    # ---------- категории ----------
    def has_data(self) -> bool:
        g = self.general
        return bool(self.layers) or any(not _nan(g.get(c)) for c in (S.LAND_TYPE, S.LAND_KIND, S.NONFOREST_KIND))

    def kind_text(self) -> str:
        return (txt(self.general.get(S.LAND_KIND)) or txt(self.general.get(S.NONFOREST_KIND)) or "").lower()

    def is_nonforest(self) -> bool:
        lt = (txt(self.general.get(S.LAND_TYPE)) or "").lower()
        return lt.startswith("нелес") or str(self.general.get(S.LAND_TYPE_CODE)) == S.CODE_NONFOREST_LANDS

    def merge_class(self) -> str:
        """stand | open | nonforest | linear | unknown — для матрицы совместимости объединения."""
        if not self.has_data():
            return "unknown"
        k = self.kind_text()
        if any(w in k for w in LINEAR_KW):
            return "linear"
        if self.is_nonforest() or any(w in k for w in AGRI_KW) or "болот" in k or "вод" in k or "прочие" in k:
            return "nonforest"
        if any(w in k for w in OPEN_KW) and not self.layers:
            return "open"
        if any(w in k for w in OPEN_KW) and "несомкнувш" not in k:
            return "open"
        return "stand"

    def area_category(self) -> str:
        """Категория для таблицы минимальных площадей п.196."""
        mc = self.merge_class()
        k = self.kind_text()
        if mc == "linear":
            return "nonforest"
        if mc == "nonforest":
            return "agri" if any(w in k for w in AGRI_KW) else "nonforest"
        if mc == "open":
            return "unstocked"
        if mc == "stand" and any(w in k for w in CULTURE_KW):
            return "unstocked"
        return "natural"

    def origin(self) -> str | None:
        if self.merge_class() != "stand":
            return None
        return "culture" if any(w in self.kind_text() for w in CULTURE_KW) else "natural"

    # ---------- ярусы и породы ----------
    def layer_nums(self) -> list[int]:
        return sorted({int(n) for n in (num(r.get(S.L_NUM)) for r in self.layers) if n is not None})

    def main_layer_num(self) -> int | None:
        nums = self.layer_nums()
        if not nums:
            return None
        main = [int(num(r.get(S.L_NUM))) for r in self.layers
                if str(r.get(S.L_MAIN)).upper() == "Y" and num(r.get(S.L_NUM)) is not None]
        return min(main) if main else nums[0]

    def layer_rows(self, n: int | None = None) -> list[dict]:
        n = self.main_layer_num() if n is None else n
        return [r for r in self.layers if num(r.get(S.L_NUM)) is not None and int(num(r.get(S.L_NUM))) == n]

    def _val(self, row: dict, col: str) -> float | None:
        v = num(row.get(col))
        if v is None:
            idx = next((i for i, r in enumerate(self.layers) if r is row), None)
            v = self.imputed.get((idx, col))
        return v

    def layer_stock_ha(self, n: int | None = None) -> float | None:
        for r in self.layer_rows(n):
            v = num(r.get(S.L_STOCK_HA))
            if v is not None:
                return v
        return None

    def species_stock_ha(self, n: int | None = None) -> dict[str, float]:
        """Запас пород яруса, м³/га.

        В выгрузках у преобладающей породы в поле «запас породы» иногда стоит запас всего яруса
        (в образце так у выделов 2 и 4). Поэтому явные запасы пород берём, только если они
        заполнены у всех пород и в сумме согласуются с запасом яруса (±15 %). Иначе считаем
        «коэффициент состава / 10 × запас яруса».
        """
        rows = [r for r in self.layer_rows(n) if txt(r.get(S.S_CODE))]
        lstock = self.layer_stock_ha(n)
        explicit: dict[str, float] = {}
        est: dict[str, float] = {}
        complete = True
        for r in rows:
            code = txt(r.get(S.S_CODE))
            share = num(r.get(S.S_SHARE)) or 0.0
            v = num(r.get(S.S_STOCK_HA))
            if v is not None:
                explicit[code] = explicit.get(code, 0.0) + v
            elif share > 0:
                complete = False
            if lstock is not None:
                est[code] = est.get(code, 0.0) + share / 10 * lstock
        if lstock is None:
            return explicit
        tot_e = sum(explicit.values())
        if explicit and complete and abs(tot_e - lstock) <= 0.15 * lstock + 1:
            return explicit
        if sum(est.values()) > 0:
            return est
        return explicit

    def composition(self, n: int | None = None) -> dict[str, float]:
        """Доли пород яруса (0..1): по запасу, если он есть, иначе по коэффициентам состава."""
        st = self.species_stock_ha(n)
        tot = sum(st.values())
        if tot > 0:
            return {k: v / tot for k, v in st.items() if v > 0}
        shares: dict[str, float] = {}
        for r in self.layer_rows(n):
            code = txt(r.get(S.S_CODE))
            s = num(r.get(S.S_SHARE))
            if code and s:
                shares[code] = shares.get(code, 0.0) + s
        tot = sum(shares.values())
        return {k: v / tot for k, v in shares.items()} if tot > 0 else {}

    def dom_row(self) -> dict | None:
        rows = self.layer_rows()
        for r in rows:
            if str(r.get(S.S_DOM)).upper() == "Y":
                return r
        comp = self.composition()
        if comp:
            best = max(comp, key=comp.get)
            return next((r for r in rows if txt(r.get(S.S_CODE)) == best), None)
        return None

    def dominant_code(self) -> str | None:
        r = self.dom_row()
        if r is not None:
            return txt(r.get(S.S_CODE))
        return txt(self.general.get(S.DOM_CODE))

    def dom_param(self, col: str) -> float | None:
        r = self.dom_row()
        return None if r is None else self._val(r, col)

    def dom_age(self) -> float | None:
        return self.dom_param(S.S_AGE)

    def dom_height(self) -> float | None:
        v = self.dom_param(S.S_HEIGHT)
        if v is None:
            rows = self.layer_rows()
            v = num(rows[0].get(S.L_HEIGHT)) if rows else None
        return v

    def dom_diameter(self) -> float | None:
        return self.dom_param(S.S_DIAM)

    def density(self) -> float | None:
        rows = self.layer_rows()
        return num(rows[0].get(S.L_DENSITY)) if rows else None

    def stock_total(self) -> float:
        return num(self.general.get(S.STOCK)) or 0.0

    def stock_ha(self) -> float | None:
        v = num(self.general.get(S.STOCK))
        if v is None:
            return None
        return v / self.area_ha if self.area_ha > 0 else None

    def bonitet_num(self) -> float | None:
        b = txt(self.general.get(S.BONITET))
        return BONITET_NUM.get(b.lower()) if b else None

    def tlu(self) -> str | None:
        return txt(self.general.get(S.TLU))

    def forest_type(self) -> str | None:
        return txt(self.general.get(S.FOREST_TYPE))

    def age_group(self, species: SpeciesBook | None = None) -> str | None:
        r = self.dom_row()
        g = txt(r.get(S.S_AGE_GROUP)) if r is not None else None
        if g is None and species is not None:
            g = species.age_group(self.dominant_code(), self.dom_age())
        return g

    def age_kind(self, species: SpeciesBook | None = None) -> str | None:
        return age_group_kind(self.age_group(species))

    def label(self, species: SpeciesBook) -> str:
        """Краткая подпись для UI: '6Е3Б1ОС 220 м³/га'."""
        rows = self.layer_rows()
        f = txt(rows[0].get(S.L_FORMULA)) if rows else None
        sh = self.stock_ha()
        k = self.general.get(S.LAND_KIND) if not f else None
        parts = [p for p in [f, txt(k), f"{sh:.0f} м³/га" if sh else None] if p]
        return " ".join(parts) if parts else "нет ТО"
