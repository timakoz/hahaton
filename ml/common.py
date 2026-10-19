"""Общие функции ML-скриптов: загрузка подготовленных таблиц ТО и сборка Stand/Taxation по кварталам."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forestmerge import schema as S  # noqa: E402
from forestmerge.io.readers import Taxation  # noqa: E402
from forestmerge.settings import PROCESSED_DIR  # noqa: E402
from forestmerge.stand import Stand  # noqa: E402


def load_processed(d: Path = PROCESSED_DIR) -> dict[str, pd.DataFrame]:
    if not (d / "general.parquet").exists():
        sys.exit(f"Нет {d / 'general.parquet'} — сначала: python3 -m ml.prepare_data")
    g = pd.read_parquet(d / "general.parquet")
    g.index = g[S.VID].values
    g.index.name = "vid"
    return {"general": g, "layers": pd.read_parquet(d / "layers.parquet"),
            "undergrowth": pd.read_parquet(d / "undergrowth.parquet")}


def group_records(df: pd.DataFrame, key: str = S.VID) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in df.to_dict("records"):          # один проход быстрее, чем groupby + to_dict на 50k групп
        out.setdefault(r[key], []).append(r)
    return out


def iter_stands(data: dict[str, pd.DataFrame]):
    """(vid, Stand) по всем выделам без O(n²)-фильтраций."""
    lay = group_records(data["layers"])
    und = group_records(data["undergrowth"])
    for vid, g in data["general"].iterrows():
        area = g.get(S.AREA)
        st = Stand(g.to_dict(), lay.get(vid, []), und.get(vid, []), float(area) if pd.notna(area) and area > 0 else 0.0)
        yield vid, st


def quarter_taxation(data: dict[str, pd.DataFrame], quarter: str, rename: bool = True) -> tuple[Taxation, dict]:
    """Taxation одного квартала. rename=True — номера выделов 1..n (как лесоустроительные)."""
    g = data["general"][data["general"][S.REG_QUARTER] == quarter].copy()
    vids = list(g.index)
    lay = data["layers"][data["layers"][S.VID].isin(vids)].copy()
    und = data["undergrowth"][data["undergrowth"][S.VID].isin(vids)].copy()
    mapping = {v: str(i + 1) for i, v in enumerate(vids)} if rename else {v: v for v in vids}
    for df in (g, lay, und):
        df[S.VID] = df[S.VID].map(mapping)
    g.index = g[S.VID].values
    g.index.name = "vid"
    return Taxation(g, lay.reset_index(drop=True), und.reset_index(drop=True), {"quarter": quarter}), mapping
