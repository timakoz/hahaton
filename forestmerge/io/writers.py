"""Выход: XLSX в формате «Пример материалов ТО» + журнал, GeoJSON."""
from __future__ import annotations

import io
import json
import math
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .. import schema as S


def _cell(v, null_token: str):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return null_token or None
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e12:
        return int(v)
    return v


def _sheet(wb: Workbook, title: str, headers: list[str], rows: list[list], null_token: str, first: bool = False):
    ws = wb.active if first else wb.create_sheet()
    ws.title = title
    ws.append(headers)
    for r in rows:
        ws.append([_cell(v, null_token) for v in r])
    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="E8F0E0")
    for i, _ in enumerate(headers, 1):
        c = ws.cell(row=1, column=i)
        c.font, c.fill = bold, fill
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions[get_column_letter(i)].width = 16
    ws.freeze_panes = "B2"
    return ws


def taxation_xlsx(tables: dict[str, pd.DataFrame], decisions: list[dict] | None = None,
                  metrics: dict | None = None, null_token: str = "") -> bytes:
    wb = Workbook()
    g = tables["general"].copy()
    as_int = lambda v: int(v) if isinstance(v, str) and v.isdigit() else v  # noqa: E731
    g[S.VID] = g[S.VID].map(as_int)
    rows = g[S.GENERAL_COLS].values.tolist()
    tot = pd.to_numeric(g[S.STOCK], errors="coerce").sum()
    rows.append([None, None, round(pd.to_numeric(g[S.AREA], errors="coerce").sum(), 4), tot] + [None] * (
        len(S.GENERAL_COLS) - 4))
    _sheet(wb, S.SHEETS["general"], S.GENERAL_COLS, rows, null_token, first=True)
    lay, und = tables["layers"].copy(), tables["undergrowth"].copy()
    lay[S.VID], und[S.VID] = lay[S.VID].map(as_int), und[S.VID].map(as_int)
    _sheet(wb, S.SHEETS["layers"], S.LAYER_HEADERS, lay[S.LAYER_COLS].values.tolist(), null_token)
    _sheet(wb, S.SHEETS["undergrowth"], S.UNDERGROWTH_COLS, und[S.UNDERGROWTH_COLS].values.tolist(), null_token)
    if decisions:
        hdr = ["№", "Тип", "Фрагмент", "Исх. выдел(ы)", "Площадь, га", "Мин. площадь, га", "Решение: выдел №",
               "Флаги", "Обоснование"]
        rows = []
        for d in decisions:
            rows.append([d["id"], {"tech": "техосколок", "merge": "объединение", "keep": "оставлен"}[d["type"]],
                         d.get("fragment"), ", ".join(d.get("fragment_parents") or []), d.get("fragment_area_ha"),
                         d.get("min_area_ha"), d.get("target_out_vid") or d.get("fragment_out_vid"),
                         ", ".join(d.get("flags") or []), d.get("explanation")])
        ws = _sheet(wb, "Журнал решений", hdr, rows, "")
        ws.column_dimensions["I"].width = 120
        for row in ws.iter_rows(min_row=2, min_col=9, max_col=9):
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
    if metrics:
        _sheet(wb, "Баланс и метрики", ["Показатель", "Значение"],
               [[k, json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v] for k, v in metrics.items()],
               "")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def write_xlsx(path: str | Path, *args, **kw) -> None:
    Path(path).write_bytes(taxation_xlsx(*args, **kw))
