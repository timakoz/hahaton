"""Формула состава насаждения: разбор «5Е3Б2ОС+С» и сборка из долей пород."""
from __future__ import annotations

import math
import re

TOKEN = re.compile(r"(10|11|\d|\+|ед\.?)([А-ЯЁа-яё]+)")

PLUS_SHARE = 0.03     # условная доля для «+» при разборе
SINGLE_SHARE = 0.01   # условная доля для «ед.» при разборе


def parse(formula: str | None) -> list[tuple[str, float]]:
    """'5Е3Б2ОС+С' → [('Е', 0.5), ('Б', 0.3), ('ОС', 0.2), ('С', 0.03)].

    В выгрузке встречается «11» как запись «ед.» — разбираем так же.
    """
    if not formula or not isinstance(formula, str):
        return []
    out = []
    for coef, abbr in TOKEN.findall(formula.replace(" ", "")):
        if coef == "+":
            share = PLUS_SHARE
        elif coef.startswith("ед") or coef == "11":
            share = SINGLE_SHARE
        else:
            share = int(coef) / 10
        out.append((abbr.upper(), share))
    return out


def build(shares: dict[str, float], plus_below: float = 0.05, single_below: float = 0.02,
          order: list[str] | None = None) -> str:
    """{'Е': 0.62, 'Б': 0.3, 'ОС': 0.06, 'С': 0.02} → '6Е3Б1ОС+С'.

    Доли ≥ plus_below превращаются в целые коэффициенты методом наибольших остатков (сумма = 10),
    меньшие доли пишутся как «+» (или «ед.» при доле < single_below).
    """
    tot = sum(v for v in shares.values() if v and v > 0)
    if tot <= 0:
        return ""
    norm = {k: v / tot for k, v in shares.items() if v and v > 0}
    major = {k: v for k, v in norm.items() if v >= plus_below}
    minor = {k: v for k, v in norm.items() if v < plus_below}
    if not major:   # все доли крошечные: берём самую большую как 10
        k = max(norm, key=norm.get)
        major, minor = {k: 1.0}, {kk: vv for kk, vv in norm.items() if kk != k}
    msum = sum(major.values())
    raw = {k: v / msum * 10 for k, v in major.items()}
    coef = {k: int(math.floor(v)) for k, v in raw.items()}
    rest = 10 - sum(coef.values())
    for k in sorted(raw, key=lambda k: raw[k] - coef[k], reverse=True)[:rest]:
        coef[k] += 1
    # коэффициент 0 после округления → «+»
    for k in [k for k, c in coef.items() if c == 0]:
        minor[k] = major.pop(k)
        coef.pop(k)
    rank = {k: i for i, k in enumerate(order or [])}
    main = sorted(coef, key=lambda k: (-coef[k], rank.get(k, 99), -norm[k]))
    parts = [f"{coef[k]}{k}" for k in main]
    for k in sorted(minor, key=lambda k: -minor[k]):
        parts.append(("ед." if minor[k] < single_below else "+") + k)
    return "".join(parts)


def units_diff(a: dict[str, float], b: dict[str, float]) -> float:
    """Различие составов в единицах (десятых): половина L1-расстояния долей × 10."""
    keys = set(a) | set(b)
    return 10 * 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys)
