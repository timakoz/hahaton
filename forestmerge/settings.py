"""Пути проекта. Всё переопределяется переменными окружения FM_*."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("FM_CONFIG_DIR", ROOT / "config"))
DATA_DIR = Path(os.environ.get("FM_DATA_DIR", ROOT))
MODELS_DIR = Path(os.environ.get("FM_MODELS_DIR", ROOT / "models"))
PROJECTS_DIR = Path(os.environ.get("FM_PROJECTS_DIR", ROOT / "data" / "projects"))
PROCESSED_DIR = Path(os.environ.get("FM_PROCESSED_DIR", ROOT / "data" / "processed"))
DB_PATH = Path(os.environ.get("FM_DB", ROOT / "data" / "app.db"))

# Где искать сырые файлы: корень проекта (как сейчас лежат данные) и data/raw.
_SEARCH = [DATA_DIR, DATA_DIR / "data" / "raw", ROOT / "data" / "raw"]


def _first(patterns: list[str]) -> Path | None:
    for base in _SEARCH:
        for pat in patterns:
            hits = sorted(base.glob(pat))
            if hits:
                return hits[0]
    return None


def find_sample() -> dict[str, Path | None]:
    """Файлы образца задачи 5 (папки 5_1, 5_2, 5_3 или распакованный архив)."""
    pre = ["", "5 ИИ-модель по фрагментам выделов (Обр)/"]
    return {
        "network": _first([p + "5_2*/*.geojson" for p in pre]),
        "taxation": _first([p + "5_2*/*.xlsx" for p in pre]),
        "lesoseki": _first([p + "5_1*/*.geojson" for p in pre]),
        "volumes": _first([p + "5_3*/*.csv" for p in pre]),
    }


def find_big_taxation() -> dict[str, Path | None]:
    """Большие выгрузки ТО (CSV, cp1251, «;»)."""
    return {
        "vydel": _first(["Таксационные описания*(выдел)*.csv"]),
        "yarus": _first(["Таксационные описания*(ярус)*.csv"]),
        "podrost": _first(["Таксационные описания*(подрост)*.csv"]),
    }


def find_geo_extra() -> dict[str, Path | None]:
    """Дополнительные геослои из плана (нужны для бэктеста и синтетики на реальных сетях)."""
    return {
        "lesoseki1": _first(["Лесосеки-1*.geojson"]),
        "lesoseki2": _first(["Лесосеки-2*.geojson"]),
        "network_big": _first(["Выделы под лесосеками*.geojson"]),
    }
