"""Препроцессинг выдела в тензоры для M1 (только numpy — нужен и при обучении, и при ONNX-инференсе).

Выдел = набор токенов «порода × ярус» + общий токен с признаками выдела.
"""
from __future__ import annotations

import math
import re

import numpy as np

from forestmerge import schema as S
from forestmerge.stand import BONITET_NUM, Stand, num, txt

MAX_TOKENS = 12
TOK_NUM = ["share", "age", "height", "diam", "sp_stock", "density", "l_height", "l_stock"]
TOK_SCALE = np.array([10, 100, 30, 40, 300, 1, 30, 300], dtype=np.float32)
G_NUM = ["log_area", "stock_ha", "bonitet", "years_since_plant", "n_layers", "density"]
G_SCALE = np.array([1, 300, 5, 100, 3, 1], dtype=np.float32)
G_CAT = ["region", "tlu", "forest_type", "land_kind", "econ_cat", "merge_class"]
PAD, UNK, MASK = 0, 1, 2


def _year(v) -> float | None:
    s = txt(v)
    m = re.search(r"(1[89]\d\d|20\d\d)", s) if s else None
    return float(m.group(1)) if m else None


def raw_values(st: Stand) -> dict:
    """Сырые значения без словарей (используется при построении словарей и в stand_to_arrays)."""
    toks = []
    for n in st.layer_nums():
        ss = st.species_stock_ha(n)
        rows = sorted(st.layer_rows(n), key=lambda r: -(num(r.get(S.S_SHARE)) or 0))
        for r in rows:
            code = txt(r.get(S.S_CODE))
            if not code:
                continue
            toks.append({
                "species": code, "layer": str(n), "dom": str(r.get(S.S_DOM)).upper() == "Y",
                "num": [num(r.get(S.S_SHARE)), st._val(r, S.S_AGE), st._val(r, S.S_HEIGHT), st._val(r, S.S_DIAM),
                        ss.get(code), num(r.get(S.L_DENSITY)), num(r.get(S.L_HEIGHT)), num(r.get(S.L_STOCK_HA))],
            })
    toks = toks[:MAX_TOKENS]
    g = st.general
    py, sy = num(g.get(S.PLANT_YEAR)), _year(g.get("Дата лесоустройства"))
    b = txt(g.get(S.BONITET))
    gnum = [math.log1p(st.area_ha) if st.area_ha else None, st.stock_ha(), BONITET_NUM.get(b.lower()) if b else None,
            (sy - py) if (sy and py) else None, float(len(st.layer_nums())), st.density()]
    gcat = [txt(g.get(S.REGION)), st.tlu(), st.forest_type(), txt(g.get(S.LAND_KIND)), txt(g.get(S.ECON_CAT)),
            st.merge_class()]
    return {"tokens": toks, "g_num": gnum, "g_cat": gcat}


def stand_to_arrays(st: Stand, meta: dict, raw: dict | None = None) -> dict[str, np.ndarray]:
    raw = raw or raw_values(st)
    voc = meta["vocab"]
    T = MAX_TOKENS
    tok_cat = np.zeros((T, 3), dtype=np.int64)
    tok_num = np.zeros((T, len(TOK_NUM)), dtype=np.float32)
    tok_nmask = np.zeros((T, len(TOK_NUM)), dtype=np.float32)
    tok_valid = np.zeros((T,), dtype=np.float32)
    for i, t in enumerate(raw["tokens"]):
        tok_cat[i] = [voc["species"].get(t["species"], UNK), voc["layer"].get(t["layer"], UNK), 1 + int(t["dom"])]
        for j, v in enumerate(t["num"]):
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                tok_num[i, j] = v / TOK_SCALE[j]
                tok_nmask[i, j] = 1
        tok_valid[i] = 1
    g_cat = np.array([voc[name].get(v, UNK) if v else UNK for name, v in zip(G_CAT, raw["g_cat"])], dtype=np.int64)
    g_num = np.zeros((len(G_NUM),), dtype=np.float32)
    g_nmask = np.zeros((len(G_NUM),), dtype=np.float32)
    for j, v in enumerate(raw["g_num"]):
        if v is not None and not (isinstance(v, float) and math.isnan(v)):
            g_num[j] = v / G_SCALE[j]
            g_nmask[j] = 1
    # выбросы выгрузки (запас 9999 и т.п.) не должны доминировать в MSE
    np.clip(tok_num, -5, 5, out=tok_num)
    np.clip(g_num, -5, 5, out=g_num)
    return {"tok_cat": tok_cat, "tok_num": tok_num, "tok_nmask": tok_nmask, "tok_valid": tok_valid,
            "g_cat": g_cat, "g_num": g_num, "g_nmask": g_nmask}


def build_vocab(raws: list[dict], min_count: int = 3) -> dict:
    from collections import Counter
    cnt = {k: Counter() for k in ["species", "layer", *G_CAT]}
    for r in raws:
        for t in r["tokens"]:
            cnt["species"][t["species"]] += 1
            cnt["layer"][t["layer"]] += 1
        for name, v in zip(G_CAT, r["g_cat"]):
            if v:
                cnt[name][v] += 1
    vocab = {}
    for k, c in cnt.items():
        items = [v for v, n in c.most_common() if n >= min_count]
        vocab[k] = {v: i + 3 for i, v in enumerate(items)}       # 0 PAD, 1 UNK, 2 MASK
    return vocab
