"""Пересчёт таксационных характеристик: объединение выделов, сплошная и выборочная рубка.

Методы пересчёта задаются в norms["recalc"] (см. config/norms_2022.yaml) и переключаются из UI.
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

from . import composition as C
from . import schema as S
from .config import SpeciesBook
from .stand import BONITET_NUM, BONITET_STR, Stand, num, txt

AGE_GROUP_CODES = {
    "молодняки I класса возраста": "40010002",
    "молодняки II класса возраста": "40010003",
    "средневозрастные": "40020004",
    "приспевающие": "40030005",
    "спелые": "40050007",
    "перестойные": "40060008",
}


def _wavg(pairs) -> float | None:
    pairs = [(v, w) for v, w in pairs if v is not None and w is not None and w > 0]
    sw = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / sw if sw > 0 else None


def _mode_by_area(values_areas) -> object:
    acc: dict = defaultdict(float)
    first = {}
    for v, a in values_areas:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        acc[v] += a
        first.setdefault(v, len(first))
    if not acc:
        return None
    return max(acc, key=lambda k: (acc[k], -first[k]))


def _round(v, nd=0):
    if v is None:
        return np.nan
    return round(float(v), nd) if nd else float(round(float(v)))


def full_age_group(species: SpeciesBook, code, age) -> str | None:
    g = species.age_group(code, age)
    if g == "молодняки":
        cls = species.age_class(code, age) or 1
        return "молодняки I класса возраста" if cls <= 1 else "молодняки II класса возраста"
    return g


# ---------------------------------------------------------------------------
# Сборка яруса по агрегированным данным пород
# ---------------------------------------------------------------------------
def _build_layer(n: int, area: float, sp_total: dict[str, float], sp_weight: dict[str, float],
                 sp_params: dict[str, dict[str, float | None]], layer_fields: dict, templates: dict[str, dict],
                 species: SpeciesBook, norms: dict, is_main: bool, group_info: dict | None) -> list[dict]:
    """sp_total — запас породы на всей площади, м³; sp_weight — веса долей, если запаса нет."""
    fcfg = norms["recalc"]["formula"]
    tot = sum(sp_total.values())
    shares = {k: v / tot for k, v in sp_total.items() if v > 0} if tot > 0 else {}
    if not shares:
        wt = sum(sp_weight.values())
        shares = {k: v / wt for k, v in sp_weight.items() if v > 0} if wt > 0 else {}
    if not shares:
        return []
    abbr = {k: species.abbr(k, templates.get(k, {}).get(S.S_NAME)) for k in shares}
    formula = C.build({abbr[k]: v for k, v in shares.items()}, fcfg["plus_below"], fcfg["single_below"])
    coefs = dict((a, c) for a, c in C.parse(formula))
    dom = max(shares, key=shares.get)
    layer_stock_ha = tot / area if area > 0 and tot > 0 else layer_fields.get("stock_ha")
    rows = []
    for code in sorted(shares, key=lambda k: -shares[k]):
        t = templates.get(code, {})
        coef = coefs.get(abbr[code])
        p = sp_params.get(code, {})
        row = {c: np.nan for c in S.LAYER_COLS}
        row.update({
            S.L_NUM: n,
            S.L_MAIN: layer_fields.get("main", "Y" if is_main else "N"),
            S.L_HEIGHT: _round(layer_fields.get("height"), 2),
            S.L_FORMULA: formula,
            S.L_AGE: _round(layer_fields.get("age")),
            S.L_DIAM: _round(layer_fields.get("diam")),
            S.L_STOCK_HA: _round(layer_stock_ha),
            S.L_DENSITY: _round(layer_fields.get("density"), 2),
            S.S_CODE: code,
            S.S_NAME: t.get(S.S_NAME) or (species.get(code).name if species.get(code) else code),
            S.S_DOM: "Y" if (is_main and code == dom) else "N",
            S.S_SHARE: float(coef) * 10 if coef is not None and coef >= 0.1 else np.nan,
            S.S_AGE: _round(p.get("age")),
            S.S_HEIGHT: _round(p.get("height")),
            S.S_DIAM: _round(p.get("diam")),
            S.S_AGE_CLASS: t.get(S.S_AGE_CLASS, np.nan),
            S.S_STOCK_HA: _round(sp_total.get(code, 0.0) / area) if area > 0 and tot > 0 else np.nan,
        })
        if is_main and code == dom and group_info:
            row[S.S_AGE_GROUP] = group_info.get("name")
            row[S.S_AGE_GROUP_CODE] = group_info.get("code")
        rows.append(row)
    return rows


def _aggregate_layers(parts: list[Stand], main_idx: int, species: SpeciesBook, norms: dict,
                      harvest: dict[str, float] | None = None) -> list[dict]:
    """Объединяет ярусы нескольких частей (или одной части с вычетом заготовки)."""
    method = norms["recalc"]
    A = sum(p.area_ha for p in parts)
    main = parts[main_idx]
    main_n = main.main_layer_num()
    layer_nums = sorted({n for p in parts for n in p.layer_nums()})
    # значения модели M2 попадают в выходное ТО только по явному флагу
    getv = (lambda p, r, c: p._val(r, c)) if method.get("use_imputed") else (lambda p, r, c: num(r.get(c)))
    out: list[dict] = []
    for n in layer_nums:
        sp_total: dict[str, float] = defaultdict(float)
        sp_weight: dict[str, float] = defaultdict(float)
        sp_param_pairs: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        templates: dict[str, dict] = {}
        lf_pairs = defaultdict(list)
        main_flag = None
        for i, p in enumerate(parts):
            rows = p.layer_rows(n)
            if not rows:
                continue
            ss = p.species_stock_ha(n)
            comp = p.composition(n)
            layer_tot_i = sum(ss.values()) * p.area_ha
            for r in rows:
                code = txt(r.get(S.S_CODE))
                if code is None:
                    continue
                if code not in templates or i == main_idx:
                    templates[code] = r
                tot_ij = ss.get(code, 0.0) * p.area_ha
                sp_total[code] += tot_ij
                share = comp.get(code, 0.0)
                sp_weight[code] += share * p.area_ha
                w = tot_ij if (method["tree_params"] == "weighted_by_stock" and tot_ij > 0) else share * p.area_ha
                if method["tree_params"] == "inherit_main" and i != main_idx:
                    w = 0.0
                for key, col in (("age", S.S_AGE), ("height", S.S_HEIGHT), ("diam", S.S_DIAM)):
                    sp_param_pairs[code][key].append((getv(p, r, col), w))
            r0 = rows[0]
            if i == main_idx or main_flag is None:
                main_flag = r0.get(S.L_MAIN) if txt(r0.get(S.L_MAIN)) else main_flag
            w_area = p.area_ha
            w_stock = layer_tot_i if layer_tot_i > 0 else p.area_ha
            lf_pairs["height"].append((num(r0.get(S.L_HEIGHT)), w_stock))
            lf_pairs["age"].append((num(r0.get(S.L_AGE)), w_stock))
            lf_pairs["diam"].append((num(r0.get(S.L_DIAM)), w_stock))
            lf_pairs["density"].append((num(r0.get(S.L_DENSITY)), w_area))
            lf_pairs["stock_ha"].append((p.layer_stock_ha(n), w_area))
        if harvest and n == main_n:
            tot_before = sum(sp_total.values())
            missing = 0.0
            for code, vol in harvest.items():
                if code in sp_total:
                    take = min(vol, sp_total[code])
                    sp_total[code] -= take
                    missing += vol - take
                else:
                    missing += vol
            if missing > 0 and sum(sp_total.values()) > 0:     # порода из файла объёмов не в составе
                k = max(0.0, 1 - missing / sum(sp_total.values()))
                for code in sp_total:
                    sp_total[code] *= k
            ratio = sum(sp_total.values()) / tot_before if tot_before > 0 else 1.0
            if norms["cut"]["selective_density_scaling"] == "proportional":
                lf_pairs["density"] = [(v * ratio if v is not None else None, w) for v, w in lf_pairs["density"]]
        sp_params = {code: {k: _wavg(v) for k, v in d.items()} for code, d in sp_param_pairs.items()}
        # плотность и т.п. по площади: часть без яруса даёт 0 (разбавление по площади)
        covered = sum(w for v, w in lf_pairs["density"] if v is not None)
        dens = _wavg(lf_pairs["density"])
        if dens is not None and A > 0 and method["density"] == "weighted_by_area":
            dens = dens * covered / A
        elif method["density"] == "inherit_main":
            dens = main.density() if n == main_n else dens
        layer_fields = {
            "height": _wavg(lf_pairs["height"]),
            "age": _wavg(lf_pairs["age"]),
            "diam": _wavg(lf_pairs["diam"]),
            "density": dens,
            "stock_ha": _wavg(lf_pairs["stock_ha"]),
            "main": main_flag,
        }
        is_main = n == main_n
        group_info = None
        if is_main:
            group_info = _age_group_info(parts, main_idx, sp_total, sp_weight, sp_params, species, norms)
        out.extend(_build_layer(n, A, dict(sp_total), dict(sp_weight), sp_params, layer_fields, templates,
                                species, norms, is_main, group_info))
    return out


def _age_group_info(parts, main_idx, sp_total, sp_weight, sp_params, species, norms) -> dict | None:
    main = parts[main_idx]
    tot = sum(sp_total.values())
    base = sp_total if tot > 0 else sp_weight
    if not base or sum(base.values()) <= 0:
        return None
    dom = max(base, key=base.get)
    age = (sp_params.get(dom) or {}).get("age")
    mode = norms["recalc"]["age_group"]
    main_row = main.dom_row()
    main_name = txt(main_row.get(S.S_AGE_GROUP)) if main_row else None
    main_code = txt(main_row.get(S.S_AGE_GROUP_CODE)) if main_row else None
    if mode == "inherit_main" and main_name:
        return {"name": main_name, "code": main_code}
    if mode == "recompute_if_changed" and main_name:
        same_sp = main.dominant_code() == dom
        a0 = main.dom_age()
        same_cls = (a0 is not None and age is not None
                    and species.age_class(dom, a0) == species.age_class(dom, age))
        if same_sp and (same_cls or age is None):
            return {"name": main_name, "code": main_code}
    name = full_age_group(species, dom, age)
    if name is None:
        return {"name": main_name, "code": main_code} if main_name else None
    return {"name": name, "code": AGE_GROUP_CODES.get(name)}


def _aggregate_undergrowth(parts: list[Stand], main_idx: int, species: SpeciesBook, norms: dict) -> list[dict]:
    if norms["recalc"]["undergrowth"] == "inherit_main":
        return [dict(r) for r in parts[main_idx].undergrowth]
    A = sum(p.area_ha for p in parts)
    sp_cnt: dict[str, float] = defaultdict(float)
    names: dict[str, str] = {}
    h_pairs, a_pairs, cnt_tot = [], [], 0.0
    best_eval, best_w = (np.nan, np.nan), -1.0
    for p in parts:
        if not p.undergrowth:
            continue
        r0 = p.undergrowth[0]
        cnt = num(r0.get(S.U_COUNT)) or 0.0
        w = cnt * p.area_ha
        cnt_tot += w
        shares = {}
        for r in p.undergrowth:
            code = txt(r.get(S.U_CODE))
            if code:
                shares[code] = shares.get(code, 0.0) + (num(r.get(S.U_SHARE)) or 0.0)
                names[code] = txt(r.get(S.U_NAME)) or names.get(code, code)
        ssum = sum(shares.values()) or 1.0
        for code, s in shares.items():
            sp_cnt[code] += w * s / ssum if w > 0 else p.area_ha * s / ssum * 1e-6
        h_pairs.append((num(r0.get(S.U_HEIGHT)), w or p.area_ha))
        a_pairs.append((num(r0.get(S.U_AGE)), w or p.area_ha))
        if w > best_w:
            best_w, best_eval = w, (r0.get(S.U_EVAL_CODE), r0.get(S.U_EVAL))
    if not sp_cnt:
        return []
    fcfg = norms["recalc"]["formula"]
    tot = sum(sp_cnt.values())
    shares = {k: v / tot for k, v in sp_cnt.items()}
    abbr = {k: species.abbr(k, names.get(k)) for k in shares}
    formula = C.build({abbr[k]: v for k, v in shares.items()}, fcfg["plus_below"], fcfg["single_below"])
    coefs = dict(C.parse(formula))
    rows = []
    for code in sorted(shares, key=lambda k: -shares[k]):
        coef = coefs.get(abbr[code])
        rows.append({
            S.VID: None,
            S.U_HEIGHT: _round(_wavg(h_pairs), 1),
            S.U_AGE: _round(_wavg(a_pairs)),
            S.U_COUNT: round(cnt_tot / A, 2) if A > 0 else np.nan,
            S.U_FORMULA: formula,
            S.U_EVAL_CODE: best_eval[0],
            S.U_EVAL: best_eval[1],
            S.U_CODE: code,
            S.U_NAME: names.get(code),
            S.U_SHARE: float(coef) * 10 if coef is not None and coef >= 0.1 else np.nan,
            S.U_DOM: "N",
        })
    return rows


# ---------------------------------------------------------------------------
# Публичные функции
# ---------------------------------------------------------------------------
def merge_stands(parts: list[Stand], main_idx: int, species: SpeciesBook, norms: dict) -> Stand:
    """Объединённый выдел. main_idx — основной (наибольший) выдел, к которому присоединяют."""
    method = norms["recalc"]
    main = parts[main_idx]
    A = sum(p.area_ha for p in parts)
    g = dict(main.general)
    g[S.AREA] = A
    for c in S.GENERAL_TOTALS:
        vals = [num(p.general.get(c)) for p in parts]
        g[c] = sum(v for v in vals if v is not None) if any(v is not None for v in vals) else np.nan

    def pick(cols: list[str], how: str):
        if how != "mode_by_area":
            return
        key = cols[-1]
        best = _mode_by_area((txt(p.general.get(key)), p.area_ha) for p in parts)
        src = next((p for p in parts if txt(p.general.get(key)) == best), None)
        if src is not None:
            for c in cols:
                g[c] = src.general.get(c)

    pick([S.LAND_TYPE_CODE, S.LAND_TYPE, S.LAND_KIND_CODE, S.LAND_KIND, S.NONFOREST_KIND_CODE, S.NONFOREST_KIND],
         method["land_type"])
    pick([S.FOREST_TYPE_CODE, S.FOREST_TYPE], method["forest_type"])
    pick([S.TLU], method["tlu"])
    if method["bonitet"] == "mode_by_area":
        best = _mode_by_area((p.bonitet_num(), p.area_ha) for p in parts)
        if best is not None:
            src = next(p for p in parts if p.bonitet_num() == best)
            g[S.BONITET], g[S.BONITET_CODE] = BONITET_STR.get(best, src.general.get(S.BONITET)), src.general.get(
                S.BONITET_CODE)
    if method["plant_year"] == "weighted_by_area":
        y = _wavg((num(p.general.get(S.PLANT_YEAR)), p.area_ha) for p in parts)
        g[S.PLANT_YEAR] = _round(y)

    layers = _aggregate_layers(parts, main_idx, species, norms)
    under = _aggregate_undergrowth(parts, main_idx, species, norms)
    st = Stand(g, layers, under, A)
    dom = st.dominant_code()
    if dom:
        g[S.DOM_CODE] = dom
        sp = species.get(dom)
        g[S.DOM_NAME] = sp.name if sp else g.get(S.DOM_NAME)
    return st


def clearcut_stand(parts: list[Stand], species: SpeciesBook, norms: dict, ls_ids: list[str]) -> Stand:
    """Выдел-вырубка на месте сплошной рубки: ТЛУ и тип леса от доминирующей по площади части,
    запас 0, древостоя нет, подрост сохраняется (если включено)."""
    A = sum(p.area_ha for p in parts)
    with_data = [p for p in parts if p.has_data()] or parts
    main = max(with_data, key=lambda p: p.area_ha)
    g = {c: np.nan for c in S.GENERAL_COLS}
    g.update({
        "Дата лесоустройства": main.general.get("Дата лесоустройства"),
        S.AREA: A,
        S.STOCK: 0.0,
        S.LAND_TYPE_CODE: S.CODE_FOREST_LANDS,
        S.LAND_TYPE: "Лесные земли",
        S.LAND_KIND: norms["cut"]["clearcut_land_kind"],
        S.FOREST_TYPE_CODE: main.general.get(S.FOREST_TYPE_CODE),
        S.FOREST_TYPE: main.general.get(S.FOREST_TYPE),
        S.TLU: _mode_by_area((p.tlu(), p.area_ha) for p in with_data) or main.tlu(),
        "Код целевой породы": main.general.get("Код целевой породы"),
        "Целевая порода (справочник) НСИ groupsTreeSpecies": main.general.get(
            "Целевая порода (справочник) НСИ groupsTreeSpecies"),
        "Наименование целевой породы": main.general.get("Наименование целевой породы"),
    })
    under = []
    if norms["cut"]["keep_undergrowth_on_clearcut"]:
        under = _aggregate_undergrowth(parts, parts.index(main), species, norms)
    st = Stand(g, [], under, A)
    st.general["_cut_from"] = ",".join(ls_ids)
    return st


def absorb_into_open(parts: list[Stand], main_idx: int, species: SpeciesBook, norms: dict) -> Stand:
    """Мелкий фрагмент насаждения присоединяется к выделу не покрытых лесом земель (вырубке).

    Выдел остаётся вырубкой. Запас присоединённого древостоя записывается как запас единичных деревьев
    (недорубы, семенные куртины), а не как ярус: иначе получилась бы «вырубка с составом».
    """
    main = parts[main_idx]
    A = sum(p.area_ha for p in parts)
    g = dict(main.general)
    g[S.AREA] = A
    for c in S.GENERAL_TOTALS:
        vals = [num(p.general.get(c)) for p in parts]
        g[c] = sum(v for v in vals if v is not None) if any(v is not None for v in vals) else np.nan
    single = sum(p.stock_total() for i, p in enumerate(parts) if i != main_idx and p.merge_class() == "stand")
    g[S.STOCK] = main.stock_total() + sum(p.stock_total() for i, p in enumerate(parts)
                                          if i != main_idx and p.merge_class() != "stand")
    g[S.STOCK_SINGLE] = (num(main.general.get(S.STOCK_SINGLE)) or 0.0) + single if single else main.general.get(
        S.STOCK_SINGLE)
    under = _aggregate_undergrowth(parts, main_idx, species, norms)
    return Stand(g, [dict(r) for r in main.layers], under, A)


def selective_stand(piece: Stand, harvest_m3: dict[str, float], species: SpeciesBook, norms: dict) -> Stand:
    """Часть выдела после выборочной рубки: запас пород уменьшен на объём заготовки."""
    st = piece.copy()
    layers = _aggregate_layers([st], 0, species, norms, harvest=harvest_m3)
    removed = sum(harvest_m3.values())
    g = dict(st.general)
    g[S.STOCK] = max(0.0, (num(g.get(S.STOCK)) or 0.0) - removed)
    out = Stand(g, layers, st.undergrowth, st.area_ha)
    # запас выдела по ярусам после вычета (точнее, чем вычитание из общего запаса)
    lay_tot = sum(sum(out.species_stock_ha(n).values()) for n in out.layer_nums()) * out.area_ha
    if lay_tot > 0:
        out.general[S.STOCK] = lay_tot
    return out


def finalize_rows(st: Stand, vid: str, norms: dict) -> tuple[dict, list[dict], list[dict]]:
    """Строки для XLSX: номер выдела, округления как в образце."""
    g = {c: st.general.get(c, np.nan) for c in S.GENERAL_COLS}
    g[S.VID] = vid
    g[S.AREA] = round(st.area_ha, 4)
    for c in S.GENERAL_TOTALS:
        v = num(g.get(c))
        g[c] = float(round(v)) if v is not None else np.nan
    py = num(g.get(S.PLANT_YEAR))
    g[S.PLANT_YEAR] = float(round(py)) if py is not None else np.nan
    layers = []
    for r in st.layers:
        rr = {c: r.get(c, np.nan) for c in S.LAYER_COLS}
        rr[S.VID] = vid
        layers.append(rr)
    under = []
    for r in st.undergrowth:
        rr = {c: r.get(c, np.nan) for c in S.UNDERGROWTH_COLS}
        rr[S.VID] = vid
        under.append(rr)
    return g, layers, under


__all__ = ["merge_stands", "clearcut_stand", "selective_stand", "finalize_rows", "BONITET_NUM"]
