"""M3: ранжировщик соседей для объединения (LightGBM LambdaRank).

    python3 -m ml.ranker.train --synth data/synth [--feedback data/app.db --feedback-weight 20]

Данные: строки «фрагмент × кандидат» из генератора синтетики (метка — учитель-перебор) и правки
специалистов из веб-приложения (метка — выбор специалиста, вес выше). Валидация — по сценариям.
Метрики: top-1 совпадение с учителем, средний regret (насколько итоговая цель хуже лучшей) — против
базлайна «argmin стоимости правил». Выход: models/ranker/{model.txt, features.json, metrics.json}
"""
from __future__ import annotations

import argparse
import glob
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from ml.common import ROOT
from forestmerge.features import FEATURES
from forestmerge.settings import DB_PATH, MODELS_DIR


def load_feedback(db: Path, weight: int) -> pd.DataFrame:
    if not db.exists():
        return pd.DataFrame()
    con = sqlite3.connect(db)
    rows = con.execute("SELECT id, fragment, action, chosen, model_choice, candidates FROM feedback "
                       "WHERE action IN ('accept','merge')").fetchall()
    con.close()
    out = []
    for fid, frag, action, chosen, model_choice, cands in rows:
        cands = json.loads(cands or "[]")
        if not chosen or len(cands) < 2 or not any(c["uid"] == chosen for c in cands):
            continue
        for rep in range(weight):
            for c in cands:
                if not c.get("features"):
                    continue
                out.append({"group": f"fb{fid}-{rep}", "scenario": f"fb{fid}", "label": 3 if c["uid"] == chosen else 0,
                            "regret": 0.0 if c["uid"] == chosen else 1.0, "cost_rules": c.get("cost") or 0.0,
                            "rule_pick": int(c["uid"] == model_choice), "source": "feedback", **c["features"]})
    return pd.DataFrame(out)


def group_metrics(df: pd.DataFrame, score: np.ndarray) -> dict:
    d = df.assign(score=score)
    top1, regret, rtop1, rregret = [], [], [], []
    for _, g in d.groupby("group", sort=False):
        best = g["label"].max()
        pick = g.loc[g["score"].idxmax()]
        rpick = g.loc[g["cost_rules"].idxmin()]
        top1.append(pick["label"] == best)
        rtop1.append(rpick["label"] == best)
        regret.append(pick["regret"])
        rregret.append(rpick["regret"])
    return {"groups": len(top1), "top1": round(float(np.mean(top1)), 4), "regret": round(float(np.mean(regret)), 4),
            "rules_top1": round(float(np.mean(rtop1)), 4), "rules_regret": round(float(np.mean(rregret)), 4)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synth", default=str(ROOT / "data" / "synth"))
    ap.add_argument("--feedback", default=str(DB_PATH))
    ap.add_argument("--feedback-weight", type=int, default=20)
    ap.add_argument("--n-estimators", type=int, default=2000)
    ap.add_argument("--lr", type=float, default=0.03)
    ap.add_argument("--leaves", type=int, default=63)
    ap.add_argument("--out", default=str(MODELS_DIR / "ranker"))
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    import lightgbm as lgb

    parts = sorted(glob.glob(str(Path(a.synth) / "*.parquet")))
    if not parts:
        raise SystemExit(f"нет данных в {a.synth}: сначала python3 -m ml.synth.generate")
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    df["source"] = "synth"
    fb = load_feedback(Path(a.feedback), a.feedback_weight)
    print(f"синтетика: {len(df)} строк, {df['group'].nunique()} групп; правки специалистов: "
          f"{fb['scenario'].nunique() if len(fb) else 0}")
    # сценарии → train/val
    rng = np.random.default_rng(a.seed)
    scen = df["scenario"].astype(str).unique()
    val_s = set(rng.choice(scen, size=max(1, len(scen) // 5), replace=False))
    val = df[df["scenario"].astype(str).isin(val_s)]
    tr = df[~df["scenario"].astype(str).isin(val_s)]
    if len(fb):
        fb["scenario"] = fb["scenario"].astype(str)
        tr = pd.concat([tr, fb], ignore_index=True)
    for c in FEATURES:
        for d in (tr, val):
            if c not in d:
                d[c] = -1.0
    tr = tr.sort_values("group", kind="stable")
    val = val.sort_values("group", kind="stable")
    gtr = tr.groupby("group", sort=False).size().loc[tr["group"].unique()].values
    gva = val.groupby("group", sort=False).size().loc[val["group"].unique()].values
    model = lgb.LGBMRanker(objective="lambdarank", n_estimators=a.n_estimators, learning_rate=a.lr,
                           num_leaves=a.leaves, min_child_samples=20, subsample=0.9, subsample_freq=1,
                           colsample_bytree=0.9, random_state=a.seed, verbose=-1)
    model.fit(tr[FEATURES], tr["label"], group=gtr, eval_set=[(val[FEATURES], val["label"])], eval_group=[gva],
              eval_at=[1, 3], callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(100)])
    m_val = group_metrics(val, model.predict(val[FEATURES]))
    m_tr = group_metrics(tr[tr["source"] == "synth"], model.predict(tr[tr["source"] == "synth"][FEATURES]))
    imp = sorted(zip(FEATURES, model.booster_.feature_importance("gain")), key=lambda x: -x[1])[:15]
    metrics = {"val": m_val, "train": m_tr, "best_iteration": int(model.best_iteration_ or 0),
               "n_train_rows": int(len(tr)), "n_val_rows": int(len(val)),
               "feedback_groups": int(fb["scenario"].nunique()) if len(fb) else 0,
               "top_features": [(k, round(float(v), 1)) for k, v in imp]}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    model.booster_.save_model(str(out / "model.txt"))
    (out / "features.json").write_text(json.dumps(FEATURES), "utf-8")
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), "utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
