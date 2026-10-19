"""Собирает config/species.yaml (справочник пород) из таблицы ярусов.

Сокращения пород («Е», «ОС», «ОЛС»…) нигде не даны явно. Их восстанавливаем по самим
формулам состава: строки пород яруса идут в том же порядке, что и токены формулы.
Продолжительность класса возраста и возраст спелости берутся по правилам п.233 ЛИ-2022 и
типовым возрастам рубок. Это значения по умолчанию, их можно править в YAML.

    python scripts/build_species.py [--yarus PATH] [--out config/species.yaml]
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forestmerge.settings import find_big_taxation  # noqa: E402

TOKEN = re.compile(r"(10|11|\d|\+|ед\.?)([А-ЯЁа-яё]+)")

# Явные правила по коду и названию: (продолжительность класса, возраст спелости, группа)
CLASS40 = {"100500", "100320", "100210"}  # кедр, пихта кавказская, ель восточная (п.233а)
MATURITY = {
    "100100": 81, "100150": 81, "100200": 81, "100300": 81, "100320": 121,
    "100400": 101, "100435": 101, "100500": 161,
    "302600": 61, "302620": 61, "302624": 61, "999004": 61, "202605": 61,
    "304000": 41, "304100": 41, "304101": 41, "304105": 41, "304110": 61,
    "304200": 61, "304260": 61, "304400": 31, "304300": 31, "304345": 31, "304344": 21,
    "201081": 101, "201082": 61, "202000": 101, "202080": 101, "202085": 61,
    "202300": 81, "202325": 81, "202400": 61, "202430": 61, "202500": 81,
    "202100": 101, "202105": 101, "202205": 61,
}


def group_of(code: str, name: str) -> str:
    """conifer | hard | soft | shrub по первой цифре кода НСИ и названию."""
    n = name.lower()
    if code.startswith("1"):
        return "conifer"
    if code.startswith("8") or "кустарн" in n or "тальник" in n:
        return "shrub"
    if code.startswith("3") or code == "999004" or "береза" in n or "осина" in n or "ольха" in n:
        return "soft"
    return "hard"


def class_len(code: str, name: str, group: str) -> int:
    n = name.lower()
    if code in CLASS40 or "кедр" in n:
        return 40
    if group == "shrub":
        return 5
    if group == "conifer":
        return 20
    if "медонос" in n:          # липа медоносная (дальневосточная) — 20 лет (п.233б)
        return 20
    if group == "hard":
        # порослевые твердолиственные (дуб низкоствольный) — 10 лет (п.233в)
        return 10 if "низкостволь" in n else 20
    return 10


def default_maturity(group: str, clen: int) -> int:
    return {"conifer": 81, "hard": 101 if clen == 20 else 61, "soft": 61, "shrub": 11}[group]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yarus", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=ROOT / "config" / "species.yaml")
    ap.add_argument("--min-count", type=int, default=3)
    args = ap.parse_args()

    path = args.yarus or find_big_taxation()["yarus"]
    if path is None:
        sys.exit("Не найден CSV ярусов. Укажите --yarus")
    y = pd.read_csv(path, sep=";", encoding="cp1251", dtype=str)
    code_c = "Код древесной породы"
    name_c = "Наименование древесной породы либо кустарниковой породы"

    pairs: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for _, g in y.groupby(["Регистрационный номер выдела", "Номер лесного яруса"], sort=False):
        f = g["Состав лесного насаждения"].iloc[0]
        if not isinstance(f, str):
            continue
        toks = TOKEN.findall(f)
        codes = g[code_c].tolist()
        if len(toks) == len(codes):
            for (_, ab), code in zip(toks, codes):
                pairs[code][ab.upper()] += 1

    names = y.groupby(code_c)[name_c].agg(lambda s: s.value_counts().index[0])
    counts = y[code_c].value_counts()
    out = {}
    used_abbr: dict[str, str] = {}
    for code, n in counts.items():
        if n < args.min_count or code not in names:
            continue
        name = str(names[code])
        best = pairs[code].most_common(1)
        abbr = best[0][0] if best and best[0][1] >= max(2, 0.5 * sum(pairs[code].values())) else None
        if abbr is None or abbr in used_abbr:
            abbr = f"{name[:3].upper()}"  # запасной вариант; правится вручную
        used_abbr.setdefault(abbr, code)
        grp = group_of(code, name)
        clen = class_len(code, name, grp)
        out[str(code)] = {
            "name": name,
            "abbr": abbr,
            "group": grp,
            "age_class_years": clen,
            "maturity_age": MATURITY.get(code, default_maturity(grp, clen)),
            "rows_in_data": int(n),
        }

    doc = {
        "_note": ("Сгенерировано scripts/build_species.py. Сокращения восстановлены по формулам состава. "
                  "age_class_years: п.233 ЛИ-2022. maturity_age: типовой возраст рубки (нижняя граница спелых) "
                  "в эксплуатационных лесах; регион и хозсекция могут менять его, правьте здесь."),
        "age_groups": {
            "young_classes": 2,        # молодняки: I–II классы возраста
            "mature_classes": {"conifer": 2, "hard": 2, "soft": 2, "shrub": 1},
        },
        "species": out,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
    print(f"species: {len(out)} -> {args.out}")


if __name__ == "__main__":
    main()
