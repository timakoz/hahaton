"""Правила Лесоустроительной инструкции: минимальные площади, совместимость, допуски, таксационная дистанция.

Каждая проверка возвращает ссылку на пункт ЛИ, чтобы решение было объяснимым.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import composition as C
from .config import SpeciesBook, tolerance
from .stand import Stand

CATEGORY_RU = {
    "natural": "естественные лесные насаждения",
    "contrast": "спелые среди молодняков / молодняки среди старших",
    "unstocked": "лесные культуры и не покрытые лесом земли",
    "agri": "с/х угодья и земли спецназначения",
    "nonforest": "просеки, дороги, линейные и другие нелесные земли",
}
MERGE_CLASS_RU = {"stand": "насаждение", "open": "не покрытые лесом лесные земли", "nonforest": "нелесные земли",
                  "linear": "линейный объект", "unknown": "нет ТО"}


@dataclass
class Check:
    code: str
    ok: bool
    text: str
    para: str
    value: float | None = None
    limit: float | None = None

    def as_dict(self) -> dict:
        return {"code": self.code, "ok": bool(self.ok), "text": self.text, "para": self.para,
                "value": None if self.value is None else round(float(self.value), 3),
                "limit": None if self.limit is None else round(float(self.limit), 3)}


def auto_rank(norms: dict, mean_area_ha: float) -> int:
    """Разряд по средней площади выдела (п.196). Это только подсказка, приоритет у явного параметра."""
    best, best_d = 2, 1e9
    for r, (lo, hi) in norms["avg_area_by_rank"].items():
        hi = hi if hi is not None else 1e9
        if lo <= mean_area_ha <= hi:
            return int(r)
        d = min(abs(mean_area_ha - lo), abs(mean_area_ha - hi))
        if d < best_d:
            best, best_d = int(r), d
    return best


def min_area(norms: dict, rank: int, category: str) -> float:
    return float(norms["min_area_ha"][rank][category])


def compatible(frag: Stand, cand: Stand, norms: dict, fallback: bool = False) -> Check:
    """Жёсткий фильтр: класс фрагмента → допустимые классы соседа (матрица rules.compat)."""
    fc, cc = frag.merge_class(), cand.merge_class()
    allowed = list(norms["rules"]["compat"].get(fc, []))
    if fallback:
        allowed += norms["rules"]["compat_fallback"].get(fc, [])
    ok = cc in allowed
    if ok and fc == cc == "nonforest":
        ok = frag.kind_text() == cand.kind_text()
    para = "п.218, п.220–221" if fc != cc else "п.218"
    text = (f"{MERGE_CLASS_RU[fc]} → {MERGE_CLASS_RU[cc]}: " + ("допустимо" if ok else "нельзя объединять")
            + (" (ослабленное правило)" if fallback and ok else ""))
    return Check("compat", ok, text, para)


def _rel_ok(v0: float | None, v1: float | None, pct: float, small_below: float | None, small_abs: float | None):
    if v0 is None or v1 is None:
        return None, None, None
    diff = abs(v1 - v0)
    if small_below is not None and v0 < small_below and small_abs is not None:
        return diff <= small_abs + 1e-9, diff, small_abs
    lim = abs(v0) * pct / 100
    return diff <= lim + 1e-9, diff, lim


def tolerance_checks(main: Stand, merged: Stand, norms: dict, species: SpeciesBook) -> list[Check]:
    """п.218: расхождения основного и объединённого выдела в пределах допусков п.215,
    запроектированное мероприятие не меняется (proxy: преобладающая порода и группа возраста)."""
    t = tolerance(norms)
    out: list[Check] = []
    if main.merge_class() != "stand":
        return out
    ok, d, lim = _rel_ok(main.stock_ha(), merged.stock_ha(), t["stock_pct"], t["stock_below_m3"], t["stock_abs_m3"])
    if ok is not None:
        out.append(Check("stock", ok, f"запас на 1 га: Δ {d:.0f} м³ (допуск {lim:.0f})", "п.218, п.215", d, lim))
    dom = main.dominant_code()
    if dom:
        k0 = main.composition().get(dom, 0.0) * 10
        k1 = merged.composition().get(dom, 0.0) * 10
        d = abs(k1 - k0)
        lim = float(t["coef_units"])
        out.append(Check("coef", d <= lim + 1e-9, f"коэффициент состава преобладающей породы: Δ {d:.1f} ед. "
                                                   f"(допуск {lim:g})", "п.218, п.215", d, lim))
    d0, d1 = main.density(), merged.density()
    if d0 is not None and d1 is not None:
        d = abs(d1 - d0)
        lim = float(t["density_abs"])
        out.append(Check("density", d <= lim + 1e-9, f"полнота: Δ {d:.2f} (допуск {lim:g})", "п.218, п.215", d, lim))
    dom1 = merged.dominant_code()
    if dom and dom1:
        mc = merged.composition()
        # ничья (5Е5Б → 5Б5Е) не считается сменой породы: разница долей < 0.5 единицы
        same = dom == dom1 or (mc.get(dom1, 0) - mc.get(dom, 0)) * 10 < 0.5
        out.append(Check("dominant", same, "преобладающая порода " + ("не меняется" if same else
                         f"меняется: {species.abbr(dom)} → {species.abbr(dom1)}"), "п.218 (мероприятие)"))
        if same and dom1 != dom:
            return out + _age_check(main, main, species)
    return out + _age_check(main, merged, species)


def _age_check(main: Stand, merged: Stand, species: SpeciesBook) -> list[Check]:
    g0, g1 = main.age_kind(species), merged.age_kind(species)
    if not (g0 and g1):
        return []
    return [Check("age_group", g0 == g1, "группа возраста " + ("не меняется" if g0 == g1 else "меняется"),
                  "п.218 (мероприятие), п.238")]


def similarity_terms(a: Stand, b: Stand, norms: dict, species: SpeciesBook) -> dict[str, float | None]:
    """Нормированные различия по признакам обособления (1.0 = ровно порог ЛИ). None — нет данных."""
    sep = norms["separation"]
    out: dict[str, float | None] = {}
    ca, cb = a.composition(), b.composition()
    out["composition"] = C.units_diff(ca, cb) / sep["composition_units"] if ca and cb else None
    da, db = a.dominant_code(), b.dominant_code()
    out["dominant_species"] = (0.0 if da == db else 1.0) * sep["dominant_species"] if da and db else None
    aa, ab = a.dom_age(), b.dom_age()
    if aa is not None and ab is not None and da:
        out["age"] = abs(aa - ab) / species.class_len(da) / sep["age_classes"]
    else:
        out["age"] = None
    ga, gb = a.age_kind(species), b.age_kind(species)
    out["age_group"] = (0.0 if ga == gb else 1.0) * sep["age_group"] if ga and gb else None
    pa, pb = a.density(), b.density()
    out["density"] = abs(pa - pb) / sep["density"] if pa is not None and pb is not None else None
    ba, bb = a.bonitet_num(), b.bonitet_num()
    out["bonitet"] = abs(ba - bb) / sep["bonitet_classes"] if ba is not None and bb is not None else None
    xa, xb = a.dom_diameter(), b.dom_diameter()
    out["diameter"] = abs(xa - xb) / sep["diameter_cm"] if xa is not None and xb is not None else None
    ta, tb = a.tlu(), b.tlu()
    out["tlu"] = (0.0 if ta == tb else 1.0) * sep["tlu"] if ta and tb else None
    fa, fb = a.forest_type(), b.forest_type()
    out["forest_type"] = (0.0 if fa == fb else 1.0) * sep["forest_type"] if fa and fb else None
    oa, ob = a.origin(), b.origin()
    out["origin"] = (0.0 if oa == ob else 1.0) * sep["origin"] if oa and ob else None
    out["land"] = 0.0 if a.merge_class() == b.merge_class() else 1.0
    sa, sb = a.stock_ha(), b.stock_ha()
    if sa is not None and sb is not None:
        t = tolerance(norms)
        base = max(sa, sb, 1.0)
        lim = t["stock_abs_m3"] if base < t["stock_below_m3"] else base * t["stock_pct"] / 100
        out["stock"] = abs(sa - sb) / max(lim, 1.0)
    else:
        out["stock"] = None
    return out


def distance(terms: dict[str, float | None], norms: dict, cap: float = 3.0) -> float:
    """Взвешенное среднее по имеющимся признакам; каждый признак обрезается сверху cap."""
    w = norms["similarity_weights"]
    num = den = 0.0
    for k, v in terms.items():
        wk = float(w.get(k, 0.0))
        if v is None or wk <= 0:
            continue
        num += wk * min(v, cap)
        den += wk
    return num / den if den > 0 else 1.0


def separation_flags(terms: dict[str, float | None]) -> list[str]:
    """Признаки, по которым участки в норме были бы разными выделами (≥ порога ЛИ)."""
    return [k for k, v in terms.items() if v is not None and v >= 1.0 - 1e-9]


SEPARATION_PARA = {
    "composition": "п.236", "dominant_species": "п.236", "age": "п.238", "age_group": "п.238",
    "density": "п.241а", "bonitet": "п.241б", "diameter": "п.241в", "tlu": "п.227", "forest_type": "п.227",
    "origin": "п.228", "stock": "п.215", "land": "п.220–221",
}
TERM_RU = {
    "composition": "состав", "dominant_species": "преобладающая порода", "age": "возраст", "age_group":
    "группа возраста", "density": "полнота", "bonitet": "бонитет", "diameter": "диаметр", "tlu": "ТЛУ",
    "forest_type": "тип леса", "origin": "происхождение", "stock": "запас на 1 га", "land": "категория земель",
}
