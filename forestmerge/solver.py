"""Решатель: наложение рубок → техзачистка → выделы-вырубки → объединение мелких фрагментов → нумерация.

Главная функция — run(). Она детерминирована: одинаковые входы и параметры дают тот же результат,
а каждое решение пишется в журнал с кандидатами, проверками ЛИ и баллами.
"""
from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Callable

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from . import geometry as G
from . import recalc as R
from . import rules as RU
from . import schema as S
from .config import SpeciesBook, load_norms, load_species
from .features import pair_features
from .io.readers import Taxation
from .stand import Stand, num


# ---------------------------------------------------------------------------
@dataclass
class Unit:
    uid: str
    geom: BaseGeometry          # в метрической CRS
    stand: Stand
    kind: str                   # orig | remnant | cut | thinned
    parents: list[str]          # исходные выделы
    main_vid: str | None        # чей номер наследует
    ls_ids: list[str] = field(default_factory=list)
    created: bool = False       # создан рубкой → подчиняется правилу минимальной площади
    pure: bool = True           # состоит только из частей своего исходного выдела без пересчёта
    members: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    removed_m3: float = 0.0     # запас, снятый рубкой с этой площади (по ТО)

    def copy(self) -> "Unit":
        return Unit(self.uid, self.geom, self.stand, self.kind, list(self.parents), self.main_vid, list(self.ls_ids),
                    self.created, self.pure, list(self.members), list(self.flags), self.removed_m3)


@dataclass
class Ctx:
    norms: dict
    species: SpeciesBook
    rank: int
    k_area: float               # перевод м² UTM → геодезические га (× k / 1e4)
    crs: Any
    orig_area: dict[str, float]
    orig_multipart: dict[str, bool]
    vgeom: dict[str, BaseGeometry] = field(default_factory=dict)
    models: Any = None
    overrides: dict = field(default_factory=dict)

    def area_ha(self, g: BaseGeometry) -> float:
        return g.area * self.k_area / 1e4


@dataclass
class State:
    units: dict[str, Unit]
    adj: G.Adjacency
    alias: dict[str, str] = field(default_factory=dict)
    decisions: list[dict] = field(default_factory=list)
    objective: float = 0.0

    def copy(self) -> "State":
        return State({k: u.copy() for k, u in self.units.items()}, self.adj.copy(), dict(self.alias), [],
                     self.objective)

    def resolve(self, uid: str) -> str:
        while uid in self.alias:
            uid = self.alias[uid]
        return uid


@dataclass
class Result:
    crs: Any
    rank: int
    norms: dict
    units: dict[str, Unit]
    out_vid: dict[str, str]
    fragments: list[dict]
    decisions: list[dict]
    metrics: dict
    warnings: list[str]
    tables: dict[str, pd.DataFrame]
    network_before: gpd.GeoDataFrame
    lesoseki: gpd.GeoDataFrame
    training_rows: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
def _cut_form(ls_id: str, volumes: pd.DataFrame | None, norms: dict) -> str:
    forms = norms["cut"].get("forms") or {}
    if ls_id in forms:
        return forms[ls_id]
    if volumes is not None and "form" in volumes and "ls_id" in volumes:
        v = volumes.loc[volumes["ls_id"] == ls_id, "form"].dropna()
        if len(v):
            return str(v.iloc[0])
    return norms["cut"]["default_form"]


def _is_clear(form: str, norms: dict) -> bool:
    f = form.lower()
    return "сплош" in f or f in {x.lower() for x in norms["cut"]["clearcut_forms"]}


def _piece_stand(orig: Stand, frac: float, area_ha: float) -> Stand:
    st = orig.copy()
    for c in S.GENERAL_TOTALS:
        v = num(st.general.get(c))
        if v is not None:
            st.general[c] = v * frac
    st.general[S.AREA] = area_ha
    st.area_ha = area_ha
    return st


def _min_area(u: Unit, ctx: Ctx, cat: str | None = None) -> float:
    return RU.min_area(ctx.norms, ctx.rank, cat or u.stand.area_category())


def _category(u: Unit, st: State, ctx: Ctx) -> str:
    """Категория п.196; «contrast» — спелые среди молодняков и наоборот (по большинству границы)."""
    cat = u.stand.area_category()
    if cat != "natural":
        return cat
    k = u.stand.age_kind(ctx.species)
    if k not in ("young", "mature"):
        return cat
    other = "mature" if k == "young" else "young"
    tot = same_other = 0.0
    for n, L in st.adj.neighbors(u.uid).items():
        nk = st.units[n].stand.age_kind(ctx.species)
        tot += L
        if nk == other:
            same_other += L
    return "contrast" if tot > 0 and same_other / tot > 0.5 else cat


# ---------------------------------------------------------------------------
# Этап 1. Техзачистка
# ---------------------------------------------------------------------------
def _tech_cleanup(pieces: list[dict], untouched: dict[str, BaseGeometry], ctx: Ctx) -> tuple[list[dict], list[dict]]:
    cfg = ctx.norms["tech_slivers"]
    nodes = {f"p{i}": p for i, p in enumerate(pieces)}
    geoms = {k: p["geom"] for k, p in nodes.items()}
    geoms.update({f"v:{vid}": g for vid, g in untouched.items()})
    adj = G.Adjacency(geoms)
    parent = {k: p["vid"] for k, p in nodes.items()}
    parent.update({f"v:{vid}": vid for vid in untouched})
    decisions: list[dict] = []

    def is_sliver(k: str) -> bool:
        p = nodes.get(k)
        if p is None or not p["created"]:
            return False
        a = ctx.area_ha(geoms[k])
        if a < cfg["max_area_ha"]:
            return True
        return a < cfg["thin_max_area_ha"] and G.is_thin(geoms[k], cfg["min_width_m"])

    order = sorted((k for k in nodes if is_sliver(k)), key=lambda k: geoms[k].area)
    absorbed: dict[str, str] = {}
    for k in order:
        if k not in geoms:
            continue
        nb = adj.neighbors(k)
        if not nb:
            continue
        same = {n: L for n, L in nb.items() if parent[n] == parent[k]} if cfg["prefer_same_parent"] else {}
        pool = same or nb
        tgt = max(pool, key=lambda n: (pool[n], n))
        p = nodes[k]
        decisions.append({
            "type": "tech", "fragment_area_ha": ctx.area_ha(geoms[k]), "fragment_parent": p["vid"],
            "fragment_zone": p["zone"], "ls_id": p["ls_id"], "target_parent": parent[tgt],
            "target_zone": nodes[tgt]["zone"] if tgt in nodes else "orig",
            "shared_border_m": nb[tgt], "geom": geoms[k],
            "reason": ("контур лесосеки скорректирован по границе выдела" if parent[tgt] == p["vid"]
                       else "осколок присоединён к соседу с самой длинной общей границей"),
        })
        geoms[tgt] = unary_union([geoms[tgt], geoms[k]])
        adj.merge(tgt, k)
        absorbed[k] = tgt
        del geoms[k]
    out = []
    for k, p in nodes.items():
        if k in geoms:
            q = dict(p)
            q["geom"] = geoms[k]
            out.append(q)
    for vid in untouched:
        untouched[vid] = geoms[f"v:{vid}"]
    return out, decisions


# ---------------------------------------------------------------------------
# Этап 2. Единицы: остатки, вырубки, пройденные выборочной рубкой
# ---------------------------------------------------------------------------
def _build_units(net_m: gpd.GeoDataFrame, stands: dict[str, Stand], pieces: list[dict], untouched: dict[str, BaseGeometry],
                 ls_form: dict[str, str], volumes: pd.DataFrame | None, ctx: Ctx) -> tuple[dict[str, Unit], list[str]]:
    norms, species = ctx.norms, ctx.species
    warns: list[str] = []
    units: dict[str, Unit] = {}
    vgeom = dict(zip(net_m.vid, net_m.geometry))
    for vid, g in untouched.items():
        st = stands[vid]
        if abs(g.area - vgeom[vid].area) > G.EPS_M2:          # поглотил техосколок
            st = _piece_stand(st, g.area / vgeom[vid].area, ctx.area_ha(g))
        units[vid] = Unit(vid, g, st, "orig", [vid], vid, created=False, members=[vid])

    def piece_stand(p) -> Stand:
        orig = stands[p["vid"]]
        frac = p["geom"].area / vgeom[p["vid"]].area
        return _piece_stand(orig, frac, ctx.area_ha(p["geom"]))

    cnt = itertools.count(1)
    by_ls: dict[str, list[dict]] = {}
    for p in pieces:
        orig = stands[p["vid"]]
        if p["zone"] == "in" and orig.merge_class() in ("linear", "nonforest"):
            p = dict(p, zone="out")                      # просека/дорога внутри контура лесосеки остаётся собой
        if p["zone"] == "out":
            uid = f"{p['vid']}·{next(cnt)}"
            units[uid] = Unit(uid, p["geom"], piece_stand(p), "remnant", [p["vid"]], p["vid"], created=p["created"],
                              members=[uid])
        else:
            by_ls.setdefault(p["ls_id"], []).append(p)

    for ls_id, ps in by_ls.items():
        form = ls_form[ls_id]
        harvest = {}
        if volumes is not None and "ls_id" in volumes:
            v = volumes[volumes["ls_id"] == ls_id]
            harvest = {k: float(x) for k, x in v.groupby("species_code")["volume_m3"].sum().items()}
        clear = _is_clear(form, norms)
        if not clear and harvest:
            inv = sum(piece_stand(p).stock_total() for p in ps)
            if inv > 0 and sum(harvest.values()) >= 0.95 * inv:
                warns.append(f"{ls_id}: заготовлено {sum(harvest.values()):.0f} м³ при запасе по ТО {inv:.0f} м³ — "
                             f"рубка обработана как сплошная")
                clear = True
        if clear:
            groups = [ps]
            if norms["cut"]["split_cut_by_tlu"]:
                tl: dict[str, list] = {}
                for p in ps:
                    tl.setdefault(stands[p["vid"]].tlu() or "?", []).append(p)
                mn = RU.min_area(norms, ctx.rank, "unstocked")
                if len(tl) > 1 and all(sum(ctx.area_ha(p["geom"]) for p in g) >= mn for g in tl.values()):
                    groups = list(tl.values())
            for grp in groups:
                for part in G.polygons(unary_union([p["geom"] for p in grp])):
                    inside = [p for p in grp if p["geom"].intersection(part).area > G.EPS_M2]
                    pst = [piece_stand(p) for p in inside]
                    st = R.clearcut_stand(pst, species, norms, [ls_id])
                    st.area_ha = ctx.area_ha(part)
                    st.general[S.AREA] = st.area_ha
                    uid = f"{ls_id}·{next(cnt)}"
                    units[uid] = Unit(uid, part, st, "cut", sorted({p["vid"] for p in inside}), None, [ls_id],
                                      created=True, pure=False, members=[uid],
                                      removed_m3=sum(x.stock_total() for x in pst))
        else:
            pst = [piece_stand(p) for p in ps]
            tot_by_sp = {}
            for st in pst:
                for code, s in st.species_stock_ha().items():
                    tot_by_sp[code] = tot_by_sp.get(code, 0.0) + s * st.area_ha
            if not harvest:
                warns.append(f"{ls_id}: выборочная рубка без объёмов заготовки — запас не уменьшен")
            for p, st in zip(ps, pst):
                share_h = {}
                for code, vol in harvest.items():
                    mine = st.species_stock_ha().get(code, 0.0) * st.area_ha
                    tot = tot_by_sp.get(code, 0.0)
                    share_h[code] = vol * (mine / tot if tot > 0 else st.area_ha / sum(x.area_ha for x in pst))
                nst = R.selective_stand(st, share_h, species, norms) if share_h else st
                uid = f"{p['vid']}·{next(cnt)}"
                units[uid] = Unit(uid, p["geom"], nst, "thinned", [p["vid"]], p["vid"], [ls_id], created=True,
                                  pure=False, members=[uid], removed_m3=st.stock_total() - nst.stock_total())

    # Выдел был меньше минимума ещё до рубки: его крупнейший остаток не «создан» рубкой
    if norms["rules"]["keep_preexisting_small"]:
        by_parent: dict[str, list[Unit]] = {}
        for u in units.values():
            if u.kind == "remnant":
                by_parent.setdefault(u.main_vid, []).append(u)
        for vid, us in by_parent.items():
            st = stands[vid]
            if ctx.orig_area[vid] < RU.min_area(norms, ctx.rank, st.area_category()):
                max(us, key=lambda u: u.geom.area).created = False
    return units, warns


# ---------------------------------------------------------------------------
# Этап 3. Объединение
# ---------------------------------------------------------------------------
def _is_small(u: Unit, st: State, ctx: Ctx) -> bool:
    if not u.created and ctx.norms["rules"]["keep_preexisting_small"]:
        return False
    return ctx.area_ha(u.geom) < _min_area(u, ctx, _category(u, st, ctx)) - 1e-9


def _evaluate(st: State, f: Unit, c: Unit, ctx: Ctx, fallback: bool, largest_nb: float,
              n_candidates: int, force: bool = False) -> dict:
    norms, species = ctx.norms, ctx.species
    shared = st.adj.neighbors(f.uid).get(c.uid, 0.0)
    same_parent = f.main_vid is not None and f.main_vid == c.main_vid
    restore = same_parent and f.pure and c.pure
    used_fallback = False
    if restore:
        comp = RU.Check("compat", True, "части одного исходного выдела (восстановление)", "п.217")
    else:
        comp = RU.compatible(f.stand, c.stand, norms, False)
        if not comp.ok and fallback:
            comp = RU.compatible(f.stand, c.stand, norms, True)
            used_fallback = comp.ok
    if force and not comp.ok:
        comp = RU.Check("compat", True, comp.text + " — выбрано пользователем", comp.para)
    fa, ca = ctx.area_ha(f.geom), ctx.area_ha(c.geom)
    cand = {"uid": c.uid, "vid": c.main_vid, "kind": c.kind, "area_ha": round(ca, 4), "shared_border_m": round(shared, 1),
            "shared_frac": round(shared / f.geom.length, 3) if f.geom.length else 0.0, "compatible": comp.ok,
            "compat": comp.as_dict(), "label": c.stand.label(species), "fallback": used_fallback}
    if not comp.ok:
        return cand
    main_is_c = ca >= fa
    parts = [c.stand, f.stand] if main_is_c else [f.stand, c.stand]
    main = parts[0]
    if restore:
        merged = _piece_stand(main, (main.area_ha + parts[1].area_ha) / main.area_ha if main.area_ha else 1.0,
                              main.area_ha + parts[1].area_ha)
        for col in S.GENERAL_TOTALS:
            a, b = num(main.general.get(col)), num(parts[1].general.get(col))
            if a is not None or b is not None:
                merged.general[col] = (a or 0.0) + (b or 0.0)
    elif c.kind == "cut" and f.kind == "cut":
        merged = R.clearcut_stand(parts, species, norms, sorted(set(c.ls_ids) | set(f.ls_ids)))
    elif main.merge_class() == "open":
        merged = R.absorb_into_open(parts, 0, species, norms)
    else:
        merged = R.merge_stands(parts, 0, species, norms)
    checks = [] if restore else RU.tolerance_checks(main, merged, norms, species)
    terms = RU.similarity_terms(f.stand, c.stand, norms, species)
    dist = 0.0 if restore else RU.distance(terms, norms)
    mg = unary_union([f.geom, c.geom])
    pp_c, pp_m = G.polsby_popper(c.geom), G.polsby_popper(mg)
    w = norms["cost_weights"]
    n_viol = sum(1 for k in checks if not k.ok)
    cost = (w["similarity"] * dist + w["border"] * (1 - min(1.0, shared / f.geom.length if f.geom.length else 0))
            + w["violation"] * n_viol + w["compactness"] * max(0.0, pp_c - pp_m)
            - w["same_parent_bonus"] * (1.0 if same_parent else 0.0)
            + w["largest_neighbor"] * (1 - ca / largest_nb if largest_nb > 0 else 0)
            + w.get("fallback", 1.0) * (1.0 if used_fallback else 0.0))
    emb_cos = ctx.models.embedding_cos(f.stand, c.stand) if ctx.models is not None else None
    feats = pair_features(
        terms=terms, dist=dist, checks=checks, shared_len=shared, frag_perimeter=f.geom.length, frag_area=fa,
        cand_area=ca, largest_nb_area=largest_nb, min_area=_min_area(f, ctx), pp_frag=G.polsby_popper(f.geom),
        pp_merged=pp_m, same_parent=same_parent, cand_created=c.created, cand_small=_is_small(c, st, ctx),
        fallback=used_fallback, rank=ctx.rank, frag_kind=f.kind, frag_cls=f.stand.merge_class(),
        cand_cls=c.stand.merge_class(), cand_stock_ha=c.stand.stock_ha(), frag_stock_ha=f.stand.stock_ha(),
        emb_cos=emb_cos, n_candidates=n_candidates)
    cand.update({
        "main": "candidate" if main_is_c else "fragment", "checks": [k.as_dict() for k in checks],
        "n_viol": n_viol, "terms": {k: (None if v is None else round(v, 3)) for k, v in terms.items()},
        "separation": RU.separation_flags(terms), "distance": round(dist, 4), "cost_rules": round(cost, 4),
        "cost": round(cost, 4), "same_parent": same_parent, "restore": restore, "features": feats,
        "_merged": merged, "_geom": mg, "_main_is_c": main_is_c,
    })
    return cand


def _score_ml(cands: list[dict], ctx: Ctx) -> None:
    mode = ctx.norms.get("scoring", "rules")
    if mode == "rules" or ctx.models is None or not ctx.models.has_ranker():
        return
    ok = [c for c in cands if c.get("compatible") and "features" in c]
    if not ok:
        return
    scores = ctx.models.rank([c["features"] for c in ok])
    for c, s in zip(ok, scores):
        c["score_ml"] = round(float(s), 4)
        c["cost"] = round(-float(s) if mode == "ml" else c["cost_rules"] - ctx.norms["ml_weight"] * float(s), 4)


def _apply_merge(st: State, f: Unit, c: Unit, cand: dict, ctx: Ctx) -> Unit:
    main, other = (c, f) if cand["_main_is_c"] else (f, c)
    nu = main.copy()
    nu.geom = cand["_geom"]
    nu.stand = cand["_merged"]
    nu.stand.area_ha = ctx.area_ha(nu.geom)
    nu.stand.general[S.AREA] = nu.stand.area_ha
    nu.parents = sorted(set(main.parents) | set(other.parents))
    nu.ls_ids = sorted(set(main.ls_ids) | set(other.ls_ids))
    nu.members = main.members + other.members
    nu.created = main.created          # результат наследует статус основного выдела
    nu.pure = bool(cand.get("restore"))
    nu.flags = sorted(set(main.flags) | set(other.flags))
    nu.removed_m3 = main.removed_m3 + other.removed_m3
    st.units[main.uid] = nu
    del st.units[other.uid]
    st.adj.merge(main.uid, other.uid)
    st.alias[other.uid] = main.uid
    return nu


def _decide(st: State, f: Unit, ctx: Ctx, record: bool = True) -> dict | None:
    """Решение для одного мелкого фрагмента. Возвращает запись журнала (или None, если нечего решать)."""
    nb = st.adj.neighbors(f.uid)
    fa = ctx.area_ha(f.geom)
    base = {"type": "merge", "fragment": f.uid, "fragment_vid": f.main_vid, "fragment_kind": f.kind,
            "fragment_area_ha": round(fa, 4), "fragment_parents": f.parents, "fragment_label": f.stand.label(ctx.species),
            "category": _category(f, st, ctx), "min_area_ha": _min_area(f, ctx, _category(f, st, ctx)),
            "rank": ctx.rank, "geom": f.geom, "flags": []}
    if not nb:
        return {**base, "type": "keep", "target": None, "candidates": [],
                "reason": "нет смежных выделов", "flags": ["isolated"]}
    largest = max(ctx.area_ha(st.units[n].geom) for n in nb)
    cands = [_evaluate(st, f, st.units[n], ctx, True, largest, len(nb)) for n in sorted(nb)]
    _score_ml(cands, ctx)
    # Варианты из ослабленной матрицы рассматриваются, только если среди обычных нет варианта без нарушений
    primary = [c for c in cands if c["compatible"] and not c["fallback"]]
    if primary and min(c["n_viol"] for c in primary) == 0:
        pool = primary
        for c in cands:
            if c["fallback"]:
                c["compatible"] = False
                c["compat"] = {**c["compat"], "ok": False,
                               "text": c["compat"]["text"] + " — не нужен: есть сосед без нарушений"}
    else:
        pool = [c for c in cands if c["compatible"]]
    ok = sorted(pool, key=lambda c: (c["cost"], c["uid"]))
    fallback_used = bool(ok) and ok[0]["fallback"]
    ov = ctx.overrides.get(f.uid)
    chosen = None
    flags = []
    if ov == "keep":
        chosen, flags = None, ["user_override"]
    elif ov:
        tgt = st.resolve(ov)
        chosen = next((c for c in cands if c["uid"] == tgt and "_merged" in c), None)
        if chosen is None and tgt in nb:   # пользователь выбрал несовместимого соседа — считаем принудительно
            chosen = _evaluate(st, f, st.units[tgt], ctx, True, largest, len(nb), force=True)
            if "_merged" not in chosen:
                chosen = None
        flags = ["user_override"]
    elif ok:
        chosen = ok[0]
        if chosen["n_viol"] > 0:
            flags.append("needs_review")
            if not ctx.norms["rules"]["allow_violation_if_no_alternative"]:
                chosen = None
                flags = ["kept_small_p197"]
    if chosen is not None and chosen.get("fallback"):
        flags.append("fallback")
    rec = {**base, "candidates": cands, "flags": flags}
    if chosen is None:
        rec.update({"type": "keep", "target": None,
                    "reason": ("оставлен по решению пользователя" if "user_override" in flags else
                               "нет допустимого соседа: выдел меньше минимума оставлен с отметкой (п.197)")})
        if "user_override" not in flags and "kept_small_p197" not in rec["flags"]:
            rec["flags"].append("kept_small_p197")
        return rec
    rec.update({"target": chosen["uid"], "target_vid": chosen.get("vid"), "chosen": chosen})
    return rec


def _greedy(st: State, ctx: Ctx, teacher: Callable | None = None, record: bool = True) -> None:
    counter = itertools.count()
    heap = [(ctx.area_ha(u.geom), next(counter), uid) for uid, u in st.units.items() if _is_small(u, st, ctx)]
    heapq.heapify(heap)
    kept: set[str] = set()
    W_KEEP = 5.0
    while heap:
        _, _, uid = heapq.heappop(heap)
        if uid not in st.units or uid in kept:
            continue
        f = st.units[uid]
        if not _is_small(f, st, ctx):
            continue
        rec = _decide(st, f, ctx)
        if teacher is not None and rec["type"] == "merge":
            teacher(st, f, rec)
        if rec["type"] == "keep":
            kept.add(uid)
            f.flags = sorted(set(f.flags) | set(rec["flags"]))
            st.objective += W_KEEP
            if record:
                st.decisions.append(rec)
            continue
        ch = rec["chosen"]
        st.objective += (ctx.norms["cost_weights"]["violation"] * ch["n_viol"]
                         + ctx.area_ha(f.geom) * ch["distance"] + 0.5 * (1 - ch["shared_frac"]))
        nu = _apply_merge(st, f, st.units[ch["uid"]], ch, ctx)
        nu.flags = sorted(set(nu.flags) | {x for x in rec["flags"] if x in ("needs_review", "fallback")})
        if record:
            st.decisions.append(rec)
        if _is_small(nu, st, ctx):
            heapq.heappush(heap, (ctx.area_ha(nu.geom), next(counter), nu.uid))


# ---------------------------------------------------------------------------
# Нумерация и выход
# ---------------------------------------------------------------------------
def _group_stand(grp: list[Unit], stands: dict[str, Stand], ctx: Ctx) -> Stand:
    """Характеристика группы частей одного исходного выдела."""
    main = max(grp, key=lambda u: u.geom.area)
    area = sum(ctx.area_ha(u.geom) for u in grp)
    if all(u.pure for u in grp) and main.main_vid in stands:
        return _piece_stand(stands[main.main_vid], sum(u.geom.area for u in grp) / ctx.vgeom[main.main_vid].area, area)
    ordered = [main] + [u for u in grp if u is not main]
    st = R.merge_stands([u.stand for u in ordered], 0, ctx.species, ctx.norms)
    st.area_ha = area
    st.general[S.AREA] = area
    return st


def _final_groups(st: State, stands: dict[str, Stand], ctx: Ctx) -> list[list[Unit]]:
    """Части одного исходного выдела снова становятся одним выделом, если:
    они смежны или исходный выдел был многоконтурным (или allow_multipart), и
    их характеристики после пересчёта не выходят за допуски п.215 (п.217–218).
    Чистые части (без пересчёта) объединяются без проверки."""
    groups: list[list[Unit]] = []
    by_vid: dict[str, list[Unit]] = {}
    for u in st.units.values():
        if u.main_vid is not None and u.kind in ("orig", "remnant", "thinned"):
            by_vid.setdefault(u.main_vid, []).append(u)
        else:
            groups.append([u])
    allow = ctx.norms["rules"]["allow_multipart"]
    for vid, us in by_vid.items():
        multi_ok = allow or ctx.orig_multipart.get(vid, False)
        pending = sorted(us, key=lambda u: -u.geom.area)
        while pending:
            grp = [pending.pop(0)]
            changed = True
            while changed:
                changed = False
                for u in list(pending):
                    if not (multi_ok or any(u.geom.distance(x.geom) < 0.5 for x in grp)):
                        continue
                    if all(x.pure for x in grp) and u.pure:
                        ok = True
                    else:
                        gs = _group_stand(grp, stands, ctx)
                        merged = R.merge_stands([gs, u.stand], 0, ctx.species, ctx.norms)
                        checks = RU.tolerance_checks(gs, merged, ctx.norms, ctx.species) + RU.tolerance_checks(
                            u.stand, merged, ctx.norms, ctx.species)
                        ok = all(c.ok for c in checks)
                    if ok:
                        grp.append(u)
                        pending.remove(u)
                        changed = True
            groups.append(grp)
    return groups


def _numbering(groups: list[list[Unit]], ctx: Ctx, all_vids: list[str]) -> list[tuple[str, list[Unit]]]:
    mode = ctx.norms["recalc"]["numbering"]
    nums = [int(v) for v in all_vids if str(v).isdigit()]
    nxt = itertools.count((max(nums) if nums else 0) + 1)
    named: list[tuple[str | None, list[Unit]]] = []
    best_for_vid: dict[str, tuple[float, int]] = {}
    for i, g in enumerate(groups):
        mv = g[0].main_vid
        unchanged = len(g) == 1 and g[0].kind == "orig" and not g[0].members[1:]
        if mv is None:
            continue
        if mode == "new_after_max" and not unchanged:
            continue
        a = sum(u.geom.area for u in g)
        if mv not in best_for_vid or a > best_for_vid[mv][0]:
            best_for_vid[mv] = (a, i)
    keep_idx = {i: vid for vid, (_, i) in best_for_vid.items()}
    new_groups = []
    for i, g in enumerate(groups):
        if i in keep_idx:
            named.append((keep_idx[i], g))
        else:
            new_groups.append(g)
    # п.290: нумерация с северо-западного угла к юго-восточному
    new_groups.sort(key=lambda g: (-unary_union([u.geom for u in g]).centroid.y // 200,
                                   unary_union([u.geom for u in g]).centroid.x))
    for g in new_groups:
        named.append((str(next(nxt)), g))
    return named  # type: ignore[return-value]


def run(network: gpd.GeoDataFrame, tax: Taxation, lesoseki: gpd.GeoDataFrame, volumes: pd.DataFrame | None = None,
        params: dict | None = None, overrides: dict | None = None, models: Any = None, teacher: bool = False,
        profile: str = "norms_2022") -> Result:
    norms = load_norms(profile, params)
    species = load_species()
    warns: list[str] = []
    crs = G.metric_crs(network)
    net_m = network.to_crs(crs).reset_index(drop=True)
    ls_m = lesoseki.to_crs(crs).reset_index(drop=True)
    geod = [G.geodesic_area_ha(g) for g in network.geometry]
    k_area = sum(geod) * 1e4 / net_m.area.sum()

    stands: dict[str, Stand] = {}
    for vid, g, ga in zip(net_m.vid, net_m.geometry, geod):
        st = Stand.from_taxation(tax, vid) if vid in tax.general.index else Stand.empty(ga, vid)
        if not st.area_ha:
            st.area_ha = ga
        stands[vid] = st
    if models is not None and getattr(models, "imputers", None):
        for st in stands.values():
            models.impute_stand(st)
    missing = [v for v in net_m.vid if v not in tax.general.index]
    if missing:
        warns.append(f"Нет ТО для выделов: {', '.join(missing[:20])}{' …' if len(missing) > 20 else ''}")
    rank = norms.get("rank") or RU.auto_rank(norms, float(np.mean(geod)))
    orig_area = {vid: stands[vid].area_ha for vid in net_m.vid}
    ctx = Ctx(norms, species, int(rank), k_area, crs, orig_area,
              {vid: len(G.polygons(g)) > 1 for vid, g in zip(net_m.vid, net_m.geometry)},
              dict(zip(net_m.vid, net_m.geometry)), models, overrides or {})

    ls_m, ov = G.resolve_lesoseki_overlaps(ls_m)
    for o in ov:
        warns.append(f"Лесосека {o['ls_id']} перекрывается с предыдущими на {o['overlap_ha']:.2f} га — "
                     f"перекрытие отнесено к первой")
    ls_form = {lid: _cut_form(lid, volumes, norms) for lid in ls_m.ls_id}

    pieces, touched = G.overlay_pieces(net_m, ls_m)
    n_raw = len(pieces)
    untouched = {vid: g for vid, g in zip(net_m.vid, net_m.geometry) if vid not in touched}
    pieces, tech = _tech_cleanup(pieces, untouched, ctx)
    units, w2 = _build_units(net_m, stands, pieces, untouched, ls_form, volumes, ctx)
    warns += w2

    adj = G.Adjacency({k: u.geom for k, u in units.items()})
    state = State(units, adj)
    # снимок фрагментов до объединения (для карты)
    fragments = []
    largest_rem: dict[str, str] = {}
    for u in units.values():
        if u.kind == "remnant" and (u.main_vid not in largest_rem
                                    or u.geom.area > units[largest_rem[u.main_vid]].geom.area):
            largest_rem[u.main_vid] = u.uid
    for u in units.values():
        if u.kind == "orig":
            continue
        cat = _category(u, state, ctx)
        a = ctx.area_ha(u.geom)
        mn = _min_area(u, ctx, cat)
        cls = {"cut": "cut", "thinned": "thinned"}.get(u.kind, "ok")
        if _is_small(u, state, ctx):
            cls = "small"
        elif not u.created:
            cls = "kept"
        elif u.kind == "remnant" and largest_rem.get(u.main_vid) == u.uid:
            cls = "main"
        fragments.append({"uid": u.uid, "class": cls, "kind": u.kind, "parent": ",".join(u.parents),
                          "ls_id": ",".join(u.ls_ids), "area_ha": round(a, 4), "min_area_ha": mn, "category": cat,
                          "label": u.stand.label(species), "geom": u.geom})
    for i, t in enumerate(tech):
        fragments.append({"uid": f"tech{i + 1}", "class": "tech", "kind": "tech", "parent": t["fragment_parent"],
                          "ls_id": t["ls_id"] or "", "area_ha": round(t["fragment_area_ha"], 5), "min_area_ha": None,
                          "category": "tech", "label": t["reason"], "geom": t["geom"]})

    training_rows: list[dict] = []
    teacher_fn = _make_teacher(ctx, training_rows) if teacher else None
    _greedy(state, ctx, teacher_fn)

    groups = _final_groups(state, stands, ctx)
    named = _numbering(groups, ctx, list(net_m.vid))
    out_vid: dict[str, str] = {}
    final_units: dict[str, Unit] = {}
    for vid, g in named:
        if len(g) == 1:
            u = g[0]
        else:
            g = sorted(g, key=lambda x: -x.geom.area)
            u = g[0].copy()
            u.geom = unary_union([x.geom for x in g])
            u.stand = _group_stand(g, stands, ctx)
            u.pure = all(x.pure for x in g)
            u.members = [m for x in g for m in x.members]
            u.removed_m3 = sum(x.removed_m3 for x in g)
            u.flags = sorted({f for x in g for f in x.flags})
        final_units[vid] = u
        for x in g:
            out_vid[x.uid] = vid
            for m in x.members:
                out_vid[m] = vid

    decisions = tech_decisions(tech) + state.decisions
    for i, d in enumerate(decisions):
        d["id"] = f"d{i + 1}"
        if d.get("target"):
            d["target_out_vid"] = out_vid.get(state.resolve(d["target"]))
        d["fragment_out_vid"] = out_vid.get(state.resolve(d["fragment"])) if d.get("fragment") else None
    for fr in fragments:
        fr["out_vid"] = out_vid.get(state.resolve(fr["uid"])) if not fr["uid"].startswith("tech") else None
        fr["decision"] = next((d["id"] for d in decisions if d.get("fragment") == fr["uid"]), None)

    tables = _tables(final_units, stands, net_m, ctx)
    metrics = _metrics(network, net_m, stands, final_units, fragments, decisions, ls_m, volumes, ctx, n_raw,
                       len(touched), tables)
    return Result(crs, ctx.rank, norms, final_units, out_vid, fragments, decisions, metrics, warns, tables,
                  network, lesoseki, training_rows)


def tech_decisions(tech: list[dict]) -> list[dict]:
    out = []
    for i, t in enumerate(tech):
        out.append({"type": "tech", "fragment": f"tech{i + 1}", "fragment_area_ha": round(t["fragment_area_ha"], 5),
                    "fragment_parents": [t["fragment_parent"]], "ls_id": t["ls_id"], "target": None,
                    "target_parent": t["target_parent"], "reason": t["reason"], "shared_border_m": round(
                        t["shared_border_m"], 1), "flags": [], "geom": t["geom"]})
    return out


def _make_teacher(ctx: Ctx, rows: list[dict]) -> Callable:
    """Учитель для синтетики: для каждого кандидата доигрываем квартал жадно и считаем итоговую цель.
    Метка (релевантность) — ранг кандидата по цели; ранжировщик M3 учится заменять этот перебор."""
    gid = itertools.count()

    def teach(st: State, f: Unit, rec: dict) -> None:
        cands = [c for c in rec["candidates"] if c.get("compatible") and "features" in c]
        if len(cands) < 2:
            return
        objs = []
        for c in cands:
            s2 = st.copy()
            f2 = s2.units[f.uid]
            c2 = s2.units[c["uid"]]
            s2.objective = (ctx.norms["cost_weights"]["violation"] * c["n_viol"]
                            + ctx.area_ha(f.geom) * c["distance"] + 0.5 * (1 - c["shared_frac"]))
            _apply_merge(s2, f2, c2, c, ctx)
            _greedy(s2, ctx, None, record=False)
            objs.append(s2.objective)
        g = next(gid)
        order = np.argsort(objs)
        rel = np.zeros(len(cands), dtype=int)
        for r, i in enumerate(order):
            rel[i] = max(0, 3 - r)
        best = float(min(objs))
        rule_pick = min(range(len(cands)), key=lambda i: (cands[i]["cost_rules"], cands[i]["uid"]))
        for i, (c, o, r) in enumerate(zip(cands, objs, rel)):
            rows.append({"group": g, "label": int(r), "objective": float(o), "regret": float(o - best),
                         "cost_rules": float(c["cost_rules"]), "rule_pick": int(i == rule_pick), **c["features"]})

    return teach


def _tables(units: dict[str, Unit], stands: dict[str, Stand], net_m, ctx: Ctx) -> dict[str, pd.DataFrame]:
    gen, lay, und = [], [], []
    for vid in sorted(units, key=lambda v: (int(v) if v.isdigit() else 10 ** 9, v)):
        u = units[vid]
        st = u.stand
        if u.kind == "orig" and not u.members[1:] and vid in stands and abs(u.geom.area - ctx.vgeom[vid].area) < G.EPS_M2:
            st = stands[vid]                                      # неизменённый выдел — как в исходном ТО
        g, l, w = R.finalize_rows(st, vid, ctx.norms)
        if u.kind == "orig" and vid in stands:
            g[S.AREA] = round(st.area_ha, 4)
        gen.append(g)
        lay += l
        und += w
    return {"general": pd.DataFrame(gen, columns=S.GENERAL_COLS), "layers": pd.DataFrame(lay, columns=S.LAYER_COLS),
            "undergrowth": pd.DataFrame(und, columns=S.UNDERGROWTH_COLS)}


def _metrics(network, net_m, stands, units, fragments, decisions, ls_m, volumes, ctx, n_raw, n_touched,
             tables) -> dict:
    min_by = {vid: _min_area(u, ctx) for vid, u in units.items()}
    small_after = [vid for vid, u in units.items() if u.created and ctx.area_ha(u.geom) < min_by[vid] - 1e-9]
    created_frag = [f for f in fragments if f["class"] not in ("tech",)]
    # запас единичных деревьев учитываем: туда переносится древостой мелких фрагментов, присоединённых к вырубкам
    stock_before = sum(st.stock_total() + (num(st.general.get(S.STOCK_SINGLE)) or 0.0) for st in stands.values())
    stock_after = float(np.nansum(pd.to_numeric(tables["general"][S.STOCK], errors="coerce"))
                        + np.nansum(pd.to_numeric(tables["general"][S.STOCK_SINGLE], errors="coerce")))
    removed_inventory = sum(u.removed_m3 for u in units.values())
    harvested = float(volumes["volume_m3"].sum()) if volumes is not None and "volume_m3" in volumes else None
    area_before = sum(G.geodesic_area_ha(g) for g in network.geometry)
    area_after = sum(ctx.area_ha(u.geom) for u in units.values())
    merges = [d for d in decisions if d["type"] == "merge"]
    return {
        "rank": ctx.rank,
        "n_vydels_before": int(len(net_m)),
        "n_vydels_after": int(len(units)),
        "n_lesoseki": int(len(ls_m)),
        "n_touched": n_touched,
        "n_pieces_raw": n_raw,
        "n_tech_slivers": sum(1 for d in decisions if d["type"] == "tech"),
        "n_fragments_small_before": sum(1 for f in created_frag if f["class"] == "small"),
        "n_fragments_lt_0_1ha_before": sum(1 for f in fragments if f["area_ha"] < 0.1),
        "n_merges": len(merges),
        "n_kept_small": sum(1 for d in decisions if d["type"] == "keep"),
        "n_needs_review": sum(1 for d in merges if "needs_review" in d["flags"]),
        "n_small_after": len(small_after),
        "small_after": small_after,
        "area_before_ha": round(area_before, 4),
        "area_after_ha": round(area_after, 4),
        "area_diff_ha": round(area_after - area_before, 6),
        "stock_before_m3": round(stock_before),
        "stock_after_m3": round(stock_after),
        "stock_removed_inventory_m3": round(removed_inventory),
        "harvested_volume_m3": None if harvested is None else round(harvested),
        "stock_balance_m3": round(stock_before - removed_inventory - stock_after),
    }
