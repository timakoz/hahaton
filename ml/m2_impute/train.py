"""M2: восстановление пропусков в ТО (высота, диаметр, возраст породы) — CatBoost, на GPU при наличии.

    python3 -m ml.m2_impute.train [--device GPU] [--iterations 3000] [--targets height diameter age]

Валидация — по кварталам (GroupShuffleSplit), чтобы не было утечки соседних выделов.
Базлайн — медиана по (порода, бонитет, ярус). Выход: models/impute/<target>.cbm + meta.json + metrics.json
"""
from __future__ import annotations

import argparse
import json
import math
import time

import numpy as np
import pandas as pd

from ml.common import load_processed
from ml.m2_impute.features import CAT_FEATURES, FEATURES, TARGETS, row_features
from forestmerge import schema as S
from forestmerge.settings import MODELS_DIR


def top_categories(general: pd.DataFrame, layers: pd.DataFrame, n: int = 200) -> dict:
    def top(s):
        return [str(x) for x in s.dropna().astype(str).value_counts().head(n).index]
    return {"species": top(layers[S.S_CODE]), "region": top(general[S.REGION]), "tlu": top(general[S.TLU]),
            "forest_type": top(general[S.FOREST_TYPE]), "land_kind": top(general[S.LAND_KIND])}


def build_frame(data: dict, meta: dict, target: str, rng: np.random.Generator, drop_known_p: float = 0.3):
    """Строки с известным target. Для устойчивости к пропускам часть «известных» других величин зануляем."""
    col = TARGETS[target]
    lay = data["layers"]
    lay = lay[pd.to_numeric(lay[col], errors="coerce").notna()]
    gen = data["general"]
    X, y, groups = [], [], []
    gdict = {vid: r for vid, r in zip(gen.index, gen.to_dict("records"))}
    for row in lay.to_dict("records"):
        g = gdict.get(row[S.VID])
        if g is None:
            continue
        f = row_features(g, row, meta, drop_target=target)
        for i, name in enumerate(FEATURES):
            if name.endswith("_known") and rng.random() < drop_known_p:
                f[i] = math.nan
        X.append(f)
        y.append(float(row[col]))
        groups.append(g.get(S.REG_QUARTER))
    X = pd.DataFrame(X, columns=FEATURES)
    for c in CAT_FEATURES:
        X[c] = X[c].astype(str)
    return X, np.array(y), np.array(groups)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="CPU", choices=["CPU", "GPU"])
    ap.add_argument("--iterations", type=int, default=3000)
    ap.add_argument("--depth", type=int, default=8)
    ap.add_argument("--lr", type=float, default=0.05)
    ap.add_argument("--targets", nargs="+", default=list(TARGETS))
    ap.add_argument("--out", default=str(MODELS_DIR / "impute"))
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()

    from catboost import CatBoostRegressor, Pool
    from pathlib import Path
    from sklearn.model_selection import GroupShuffleSplit

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    data = load_processed()
    meta = {"targets": {t: TARGETS[t] for t in a.targets}, "features": FEATURES, "cat_features": CAT_FEATURES,
            "top_categories": top_categories(data["general"], data["layers"])}
    rng = np.random.default_rng(a.seed)
    metrics = {}
    for t in a.targets:
        t0 = time.time()
        X, y, groups = build_frame(data, meta, t, rng)
        tr, va = next(GroupShuffleSplit(1, test_size=0.15, random_state=a.seed).split(X, y, groups))
        model = CatBoostRegressor(iterations=a.iterations, depth=a.depth, learning_rate=a.lr, loss_function="RMSE",
                                  eval_metric="MAE", task_type=a.device, random_seed=a.seed, od_type="Iter", od_wait=200,
                                  verbose=200, train_dir=str(out / "catboost_info"))
        model.fit(Pool(X.iloc[tr], y[tr], cat_features=CAT_FEATURES), eval_set=Pool(X.iloc[va], y[va],
                  cat_features=CAT_FEATURES), use_best_model=True)
        pred = model.predict(X.iloc[va])
        # базлайн: медиана по (порода, бонитет, ярус) на train
        key = ["species", "bonitet", "layer"]
        med = pd.DataFrame({**{k: X.iloc[tr][k].values for k in key}, "y": y[tr]}).groupby(key)["y"].median()
        sp_med = pd.DataFrame({"species": X.iloc[tr]["species"].values, "y": y[tr]}).groupby("species")["y"].median()
        base = [med.get(tuple(r), sp_med.get(r[0], np.median(y[tr]))) for r in X.iloc[va][key].values]
        mae, mae_b = float(np.mean(np.abs(pred - y[va]))), float(np.mean(np.abs(np.array(base) - y[va])))
        metrics[t] = {"n_train": int(len(tr)), "n_val": int(len(va)), "mae": round(mae, 3), "mae_baseline": round(mae_b, 3),
                      "gain_pct": round(100 * (1 - mae / mae_b), 1), "best_iter": int(model.get_best_iteration() or 0),
                      "seconds": round(time.time() - t0, 1)}
        model.save_model(str(out / f"{t}.cbm"))
        print(t, metrics[t])
    meta["metrics"] = metrics
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), "utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
