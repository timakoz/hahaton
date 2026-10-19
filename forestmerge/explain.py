"""Человекочитаемые обоснования решений.

По умолчанию — детерминированные шаблоны со ссылками на пункты ЛИ. Если задан FM_LLM_URL
(OpenAI-совместимый сервер: vLLM, Ollama, llama.cpp), текст можно переписать локальной LLM.
LLM только пересказывает решение; сами решения и числа принимает решатель.
"""
from __future__ import annotations

import json
import os
import urllib.request

from .rules import CATEGORY_RU, SEPARATION_PARA, TERM_RU


def _fmt_area(a) -> str:
    return f"{a:.4f} га" if a < 0.1 else f"{a:.2f} га"


def template(d: dict) -> str:
    if d["type"] == "tech":
        zone = "внутри контура лесосеки" if d.get("ls_id") else "вне лесосеки"
        return (f"Технологический осколок {_fmt_area(d['fragment_area_ha'])} выдела {d['fragment_parents'][0]} "
                f"({zone}, лесосека {d.get('ls_id') or '—'}): {d['reason']} "
                f"(общая граница {d['shared_border_m']:.0f} м). Это ошибка совмещения контуров, "
                f"таксационные показатели не сравниваются.")
    cat = CATEGORY_RU.get(d.get("category"), d.get("category"))
    head = (f"Фрагмент {d['fragment']} ({d['fragment_label']}, {_fmt_area(d['fragment_area_ha'])}) меньше минимальной "
            f"площади {d['min_area_ha']:g} га для категории «{cat}» при {d['rank']}-м разряде (п.196).")
    if d["type"] == "keep":
        r = d.get("reason", "")
        return head + " " + (r[:1].upper() + r[1:]) + "."
    ch = d["chosen"]
    tgt = d.get("target_out_vid") or ch.get("vid") or ch["uid"]
    parts = [head]
    if ch.get("restore"):
        parts.append(f"Присоединён к соседней части того же исходного выдела (№ {tgt}): характеристики не меняются.")
    elif ch.get("fallback") and "Вырубка" in (ch.get("label") or "") or ch.get("fallback") and "Лесосека" in (
            ch.get("label") or ""):
        parts.append(f"Смежных насаждений, к которым фрагмент можно присоединить без нарушения допусков, нет. "
                     f"Он присоединён к выделу № {tgt} ({ch['label']}) как недоруб: запас древостоя фрагмента "
                     f"учтён в графе «запас единичных деревьев» (ослабленное правило совместимости — требуется "
                     f"подтверждение специалиста).")
    else:
        why = []
        if ch.get("same_parent"):
            why.append("это часть того же исходного выдела")
        if ch["shared_frac"] >= 0.3:
            why.append(f"общая граница {ch['shared_frac'] * 100:.0f} % периметра")
        sep = ch.get("separation") or []
        close = [TERM_RU[k] for k, v in (ch.get("terms") or {}).items() if v is not None and v < 0.5]
        if close:
            why.append("близки по: " + ", ".join(close[:5]))
        parts.append(f"Присоединён к выделу № {tgt} ({ch['label']}) — наиболее близкому по таксационной "
                     f"характеристике среди смежных (п.218)" + (": " + "; ".join(why) if why else "") + ".")
        if sep:
            parts.append("Различия выше порогов обособления: " + ", ".join(
                f"{TERM_RU[k]} ({SEPARATION_PARA[k]})" for k in sep) + " — допустимо, т.к. площадь фрагмента "
                "меньше минимальной (п.218).")
        bad = [c for c in ch.get("checks", []) if not c["ok"]]
        okc = [c for c in ch.get("checks", []) if c["ok"]]
        if okc:
            parts.append("Допуски п.215 соблюдены: " + "; ".join(c["text"] for c in okc) + ".")
        if bad:
            parts.append("ВНИМАНИЕ, нарушены допуски: " + "; ".join(c["text"] for c in bad)
                         + ". Других допустимых соседей нет — требуется проверка специалистом.")
    alts = [c for c in d["candidates"] if c["uid"] != ch["uid"]]
    if alts:
        alt_txt = []
        for c in sorted(alts, key=lambda c: c.get("cost", 99))[:3]:
            if not c["compatible"]:
                alt_txt.append(f"№ {c.get('vid') or c['uid']}: {c['compat']['text']}")
            else:
                alt_txt.append(f"№ {c.get('vid') or c['uid']}: балл {c['cost']:.2f} против {ch['cost']:.2f}")
        parts.append("Отклонённые варианты: " + "; ".join(alt_txt) + ".")
    if "user_override" in d.get("flags", []):
        parts.append("Решение задано специалистом вручную.")
    return " ".join(parts)


def llm_rewrite(d: dict, text: str, timeout: float = 30.0) -> str | None:
    """Переписать обоснование локальной LLM (если сервер настроен). Ошибки → None (остаётся шаблон)."""
    url = os.environ.get("FM_LLM_URL")
    if not url:
        return None
    model = os.environ.get("FM_LLM_MODEL", "qwen2.5-7b-instruct")
    facts = {k: d.get(k) for k in ("fragment", "fragment_area_ha", "min_area_ha", "category", "rank", "target_out_vid")}
    if d.get("chosen"):
        ch = d["chosen"]
        facts["chosen"] = {k: ch.get(k) for k in ("label", "shared_frac", "checks", "separation", "terms")}
    prompt = ("Ты — инженер-таксатор. Перепиши обоснование решения официальным деловым языком для пояснительной "
              "записки, 3–5 предложений. Не меняй числа и номера пунктов, ничего не добавляй от себя.\n\n"
              f"Факты (JSON): {json.dumps(facts, ensure_ascii=False, default=str)}\n\nЧерновик: {text}")
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2,
                       "max_tokens": 400}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"].strip()
    except Exception:  # noqa: BLE001
        return None
