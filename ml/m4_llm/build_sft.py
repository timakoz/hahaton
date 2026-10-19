"""M4 (опционально): датасет для LoRA-дообучения LLM-«пояснителя».

    python -m ml.m4_llm.build_sft --scenarios 300 --out data/sft/train.jsonl [--rewrite-url http://host:8001/v1]

Вход модели — факты решения (JSON), выход — текст обоснования для пояснительной записки.
Целевые тексты: шаблон explain.template (базово) либо его перефразировка более крупной моделью
(--rewrite-url, OpenAI-совместимый сервер) для разнообразия стиля. Позже сюда добавляются тексты,
отредактированные специалистами.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ml.synth import generate as G
from forestmerge.explain import llm_rewrite, template
from forestmerge.settings import PROCESSED_DIR, find_geo_extra

SYSTEM = ("Ты — инженер-таксатор. По фактам решения об объединении лесотаксационных выделов напиши обоснование "
          "для пояснительной записки: 3–5 предложений, официальный стиль, со ссылками на пункты "
          "Лесоустроительной инструкции. Не меняй числа.")


def facts(d: dict) -> dict:
    f = {k: d.get(k) for k in ("type", "fragment_area_ha", "fragment_label", "min_area_ha", "category", "rank",
                               "target_out_vid", "flags")}
    ch = d.get("chosen") or {}
    f["chosen"] = {k: ch.get(k) for k in ("label", "shared_frac", "separation", "restore", "fallback")}
    f["checks"] = ch.get("checks")
    f["alternatives"] = [{"label": c.get("label"), "compatible": c.get("compatible"), "cost": c.get("cost"),
                          "reason": (c.get("compat") or {}).get("text")}
                         for c in d.get("candidates", []) if c.get("uid") != ch.get("uid")][:3]
    return f


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", type=int, default=300)
    ap.add_argument("--out", default="data/sft/train.jsonl")
    ap.add_argument("--rewrite-url", default=None)
    a = ap.parse_args()
    if a.rewrite_url:
        os.environ["FM_LLM_URL"] = a.rewrite_url
    extra = find_geo_extra()
    G._init(str(PROCESSED_DIR), None, [str(p) for p in (extra["lesoseki1"], extra["lesoseki2"]) if p])
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True) if out.suffix == "" else out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    orig_run = G.run

    def run_collect(*args, **kw):
        kw["teacher"] = False
        res = orig_run(*args, **kw)
        run_collect.last = res
        return res

    G.run = run_collect
    with open(out, "w", encoding="utf-8") as fh:
        for seed in range(a.scenarios):
            run_collect.last = None
            G.scenario(10_000_000 + seed)
            res = run_collect.last
            if res is None:
                continue
            for d in res.decisions:
                if d["type"] != "merge":
                    continue
                text = template(d)
                if a.rewrite_url:
                    text = llm_rewrite(d, text) or text
                msg = [{"role": "system", "content": SYSTEM},
                       {"role": "user", "content": json.dumps(facts(d), ensure_ascii=False, default=str)},
                       {"role": "assistant", "content": text}]
                fh.write(json.dumps({"messages": msg}, ensure_ascii=False) + "\n")
                n += 1
    print(f"примеров: {n} → {out}")


if __name__ == "__main__":
    main()
