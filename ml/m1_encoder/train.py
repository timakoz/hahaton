"""M1: самообучение энкодера выделов (masked modeling) на всей выгрузке ТО. Рассчитан на RTX 4090.

    python3 -m ml.m1_encoder.train --epochs 40 --batch 512 --d-model 384 --layers 8     # ~14M параметров
    python3 -m ml.m1_encoder.train --wait-gpu 60      # подождать до часа, пока на общем GPU освободится память
    python3 -m ml.m1_encoder.train --epochs 1 --limit 3000 --out out/smoke_models/encoder # быстрая проверка

Маскируем 25 % известных числовых значений (H, D, возраст, запас, полнота…), 15 % пород и 20 % общих
признаков, учим восстанавливать. Эмбеддинг [CLS] (128-d, L2-норма) используется решателем как признак
сходства emb_cos в ранжировщике M3. Выход: models/encoder/{encoder.pt, encoder.onnx, meta.json}
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

from ml.common import iter_stands, load_processed
from ml.m1_encoder.tokens import G_CAT, MASK, build_vocab, raw_values, stand_to_arrays
from forestmerge.settings import MODELS_DIR

KEYS = ["tok_cat", "tok_num", "tok_nmask", "tok_valid", "g_cat", "g_num", "g_nmask"]


def make_arrays(limit: int | None, seed: int):
    data = load_processed()
    vids, raws, quarters = [], [], []
    for i, (vid, st) in enumerate(iter_stands(data)):
        if limit and i >= limit:
            break
        r = raw_values(st)
        if not r["tokens"]:
            continue
        vids.append(vid)
        raws.append(r)
        quarters.append(st.general.get("Регистрационный номер квартала"))
    vocab = build_vocab(raws)
    meta = {"vocab": vocab}
    arrs = [stand_to_arrays(None, meta, raw=r) for r in raws]
    stacked = {k: np.stack([a[k] for a in arrs]) for k in KEYS}
    # сплит по кварталам
    uq = sorted({q for q in quarters if q})
    rng = np.random.default_rng(seed)
    val_q = set(rng.choice(uq, size=max(1, len(uq) // 10), replace=False)) if uq else set()
    is_val = np.array([q in val_q for q in quarters])
    return stacked, vocab, is_val, vids


def mask_batch(b: dict, rng: np.random.Generator, p_num=0.25, p_sp=0.15, p_g=0.2):
    inp = {k: v.copy() for k, v in b.items()}
    m_num = (rng.random(b["tok_num"].shape) < p_num) & (b["tok_nmask"] > 0)
    inp["tok_num"][m_num] = 0
    inp["tok_nmask"][m_num] = 0
    m_sp = (rng.random(b["tok_valid"].shape) < p_sp) & (b["tok_valid"] > 0)
    inp["tok_cat"][..., 0][m_sp] = MASK
    m_g = (rng.random(b["g_num"].shape) < p_g) & (b["g_nmask"] > 0)
    inp["g_num"][m_g] = 0
    inp["g_nmask"][m_g] = 0
    return inp, m_num, m_sp, m_g


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--d-model", type=int, default=256)
    ap.add_argument("--layers", type=int, default=6)
    ap.add_argument("--heads", type=int, default=8)
    ap.add_argument("--emb-dim", type=int, default=128)
    ap.add_argument("--limit", type=int, default=None, help="взять только N выделов (отладка)")
    ap.add_argument("--out", default=str(MODELS_DIR / "encoder"))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-onnx", action="store_true")
    ap.add_argument("--device", default="auto", choices=["auto", "GPU", "CPU"],
                    help="auto — GPU, если свободно ≥ --gpu-mem-mb, иначе CPU (медленнее, но работает)")
    ap.add_argument("--gpu-mem-mb", type=int, default=6000, help="сколько видеопамяти нужно (с запасом)")
    ap.add_argument("--wait-gpu", type=float, default=0, help="ждать освобождения GPU до N минут")
    a = ap.parse_args()

    import os

    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    import torch
    from torch import nn

    from ml import gpu
    from ml.m1_encoder.model import EmbeddingOnly, VydelEncoder

    torch.manual_seed(a.seed)
    choice, info = gpu.choose(a.device, need_mb=a.gpu_mem_mb, wait_min=a.wait_gpu)
    dev = "cuda" if choice == "GPU" and torch.cuda.is_available() else "cpu"
    if dev == "cpu":
        torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))
    print(f"[m1] устройство: {dev} {info}", flush=True)
    t0 = time.time()
    X, vocab, is_val, _ = make_arrays(a.limit, a.seed)
    n = len(is_val)
    print(f"выделов: {n} (val {is_val.sum()}), устройство: {dev}, подготовка {time.time() - t0:.0f} с")
    sizes = {"species": len(vocab["species"]) + 3, "layer": len(vocab["layer"]) + 3,
             **{k: len(vocab[k]) + 3 for k in G_CAT}}
    cfg = {"d_model": a.d_model, "n_layers": a.layers, "n_heads": a.heads, "ff": 4 * a.d_model, "emb_dim": a.emb_dim}
    model = VydelEncoder(sizes, **cfg).to(dev)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"параметров: {n_params / 1e6:.1f}M")
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=0.01)
    steps = a.epochs * math.ceil((~is_val).sum() / a.batch)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=a.lr, total_steps=max(steps, 1), pct_start=0.05)
    amp = dev == "cuda"
    rng = np.random.default_rng(a.seed)
    tr_idx, va_idx = np.where(~is_val)[0], np.where(is_val)[0]

    def to_t(d):
        return {k: torch.from_numpy(v).to(dev) for k, v in d.items()}

    def losses(b, train: bool, r: np.random.Generator):
        inp, m_num, m_sp, m_g = mask_batch(b, r)
        with torch.autocast(dev, dtype=torch.bfloat16, enabled=amp):
            out = model(**to_t(inp))
        tgt = to_t(b)
        mn, ms, mg = (torch.from_numpy(x).to(dev) for x in (m_num, m_sp, m_g))
        l_num = ((out["tok_num"].float() - tgt["tok_num"]) ** 2)[mn].mean() if mn.any() else torch.tensor(0., device=dev)
        l_sp = nn.functional.cross_entropy(out["species"].float()[ms], tgt["tok_cat"][..., 0][ms]) if ms.any() \
            else torch.tensor(0., device=dev)
        l_g = ((out["g_num"].float() - tgt["g_num"]) ** 2)[mg].mean() if mg.any() else torch.tensor(0., device=dev)
        acc = (out["species"].argmax(-1)[ms] == tgt["tok_cat"][..., 0][ms]).float().mean().item() if ms.any() else 0.
        return l_num + 0.2 * l_sp + l_g, {"num_mse": l_num.item(), "sp_ce": l_sp.item(), "g_mse": l_g.item(),
                                          "sp_acc": acc}

    best, hist = 1e9, []
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for ep in range(a.epochs):
        model.train()
        rng.shuffle(tr_idx)
        tl = []
        for s in range(0, len(tr_idx), a.batch):
            idx = tr_idx[s:s + a.batch]
            loss, _ = losses({k: v[idx] for k, v in X.items()}, True, rng)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            tl.append(loss.item())
        model.eval()
        vr = np.random.default_rng(123)
        agg = []
        with torch.no_grad():
            for s in range(0, len(va_idx), a.batch):
                idx = va_idx[s:s + a.batch]
                l, parts = losses({k: v[idx] for k, v in X.items()}, False, vr)
                agg.append((l.item(), parts))
        vl = float(np.mean([x[0] for x in agg])) if agg else float(np.mean(tl))
        vparts = {k: round(float(np.mean([x[1][k] for x in agg])), 4) for k in agg[0][1]} if agg else {}
        hist.append({"epoch": ep + 1, "train": round(float(np.mean(tl)), 4), "val": round(vl, 4), **vparts})
        print(hist[-1], f"{time.time() - t0:.0f} с")
        if vl < best:
            best = vl
            torch.save({"state": model.state_dict(), "sizes": sizes, "cfg": cfg}, out_dir / "encoder.pt")

    meta = {"vocab": vocab, "sizes": sizes, "cfg": cfg, "history": hist, "n_params": n_params,
            "inputs": KEYS, "best_val": best}
    if not a.no_onnx:
        ck = torch.load(out_dir / "encoder.pt", map_location="cpu")
        m = VydelEncoder(ck["sizes"], **ck["cfg"])
        m.load_state_dict(ck["state"])
        m.eval()
        wrap = EmbeddingOnly(m)
        sample = {k: torch.from_numpy(v[:2]) for k, v in X.items()}
        torch.onnx.export(wrap, tuple(sample[k] for k in KEYS), str(out_dir / "encoder.onnx"), input_names=KEYS,
                          output_names=["emb"], dynamic_axes={k: {0: "batch"} for k in KEYS + ["emb"]},
                          opset_version=17, dynamo=False)
        meta["onnx"] = "encoder.onnx"
        print("ONNX:", out_dir / "encoder.onnx")
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
    print("готово:", out_dir)


if __name__ == "__main__":
    main()
