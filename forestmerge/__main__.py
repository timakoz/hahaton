"""CLI: python3 -m forestmerge run --sample --out out/sample
        python3 -m forestmerge run --network сеть.geojson --taxation ТО.xlsx --lesoseki лс.geojson [--volumes v.csv]
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .ml_hooks import get_models
from .pipeline import load_inputs, load_sample, save_all, solve


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="forestmerge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="обработать квартал")
    r.add_argument("--sample", action="store_true", help="взять файлы образца 5_1/5_2/5_3")
    r.add_argument("--network")
    r.add_argument("--taxation", help="XLSX в формате образца")
    r.add_argument("--lesoseki")
    r.add_argument("--volumes")
    r.add_argument("--rank", type=int)
    r.add_argument("--params", help="JSON с переопределениями норм, например '{\"rank\": 1}'")
    r.add_argument("--models", action="store_true", help="подключить обученные модели из models/")
    r.add_argument("--out", default="out/run")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    inp = load_sample() if a.sample else load_inputs(a.network, a.taxation, a.lesoseki, a.volumes)
    params = json.loads(a.params) if a.params else {}
    if a.rank:
        params["rank"] = a.rank
    t0 = time.time()
    res = solve(inp, params, models=get_models() if a.models else None)
    dt = time.time() - t0
    paths = save_all(res, a.out)
    print(json.dumps(res.metrics, ensure_ascii=False, indent=1, default=str))
    for w in res.warnings:
        print("!", w)
    print(f"время: {dt:.2f} с")
    for k, p in paths.items():
        print(f"{k}: {p}")


if __name__ == "__main__":
    main()
