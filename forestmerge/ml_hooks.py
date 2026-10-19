"""Подключение обученных моделей к решателю. Все модели опциональны: без них работают правила.

models/
  ranker/model.txt + features.json     — M3, LightGBM LambdaRank
  impute/<target>.cbm + meta.json      — M2, CatBoost (восстановление H, D, возраста, запаса пород)
  encoder/encoder.onnx + meta.json     — M1, эмбеддинг выдела
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from .settings import MODELS_DIR
from .stand import Stand

log = logging.getLogger(__name__)


class Models:
    def __init__(self, root: Path = MODELS_DIR):
        self.root = Path(root)
        self.ranker = None
        self.ranker_features: list[str] = []
        self.encoder = None
        self.encoder_meta: dict = {}
        self.imputers: dict = {}
        self.impute_meta: dict = {}
        self.versions: dict[str, str] = {}
        self._load()

    # ---------------- загрузка ----------------
    def _load(self) -> None:
        rk = self.root / "ranker" / "model.txt"
        if rk.exists():
            try:
                import lightgbm as lgb
                self.ranker = lgb.Booster(model_file=str(rk))
                self.ranker_features = json.loads((self.root / "ranker" / "features.json").read_text("utf-8"))
                self.versions["ranker"] = rk.stat().st_mtime_ns.__str__()
            except Exception as e:  # noqa: BLE001
                log.warning("ranker not loaded: %s", e)
        enc = self.root / "encoder" / "encoder.onnx"
        if enc.exists():
            try:
                import onnxruntime as ort
                self.encoder = ort.InferenceSession(str(enc), providers=["CPUExecutionProvider"])
                self.encoder_meta = json.loads((self.root / "encoder" / "meta.json").read_text("utf-8"))
                self.versions["encoder"] = enc.stat().st_mtime_ns.__str__()
            except Exception as e:  # noqa: BLE001
                log.warning("encoder not loaded: %s", e)
        imp = self.root / "impute" / "meta.json"
        if imp.exists():
            try:
                from catboost import CatBoostRegressor
                self.impute_meta = json.loads(imp.read_text("utf-8"))
                for tgt in self.impute_meta.get("targets", {}):
                    m = CatBoostRegressor()
                    m.load_model(str(self.root / "impute" / f"{tgt}.cbm"))
                    self.imputers[tgt] = m
                self.versions["impute"] = imp.stat().st_mtime_ns.__str__()
            except Exception as e:  # noqa: BLE001
                log.warning("imputers not loaded: %s", e)

    def status(self) -> dict:
        return {"ranker": self.ranker is not None, "encoder": self.encoder is not None,
                "impute": sorted(self.imputers), "versions": self.versions}

    # ---------------- M3 ----------------
    def has_ranker(self) -> bool:
        return self.ranker is not None

    def rank(self, feats: list[dict]) -> np.ndarray:
        X = np.array([[f.get(k, -1.0) for k in self.ranker_features] for f in feats], dtype=float)
        return self.ranker.predict(X)

    # ---------------- M1 ----------------
    def embed(self, st: Stand) -> np.ndarray | None:
        if self.encoder is None:
            return None
        from ml.m1_encoder.tokens import stand_to_arrays   # тот же препроцессинг, что при обучении
        arrs = stand_to_arrays(st, self.encoder_meta)
        out = self.encoder.run(None, {k: v[None] for k, v in arrs.items()})[0][0]
        return out / (np.linalg.norm(out) + 1e-9)

    def embedding_cos(self, a: Stand, b: Stand) -> float | None:
        if self.encoder is None:
            return None
        ea, eb = self.embed(a), self.embed(b)
        return None if ea is None or eb is None else float(ea @ eb)

    # ---------------- M2 ----------------
    def impute_stand(self, st: Stand) -> Stand:
        """Заполняет пропуски H/D/возраста/запаса пород в st.imputed (исходные значения не трогает)."""
        if not self.imputers:
            return st
        from ml.m2_impute.features import row_features
        for i, row in enumerate(st.layers):
            for tgt, col in self.impute_meta["targets"].items():
                if tgt not in self.imputers:
                    continue
                v = row.get(col)
                if v is None or (isinstance(v, float) and np.isnan(v)):
                    x = row_features(st.general, row, self.impute_meta)
                    st.imputed[(i, col)] = float(self.imputers[tgt].predict([x])[0])
        return st


_CACHE: dict[str, Models] = {}


def get_models(root: Path = MODELS_DIR, reload: bool = False) -> Models:
    key = str(root)
    if reload or key not in _CACHE:
        _CACHE[key] = Models(root)
    return _CACHE[key]
