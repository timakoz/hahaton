"""Выбор устройства по фактически свободной видеопамяти (сервер общий: память могут занимать другие)."""
from __future__ import annotations

import os
import shutil
import subprocess
import time


def gpu_memory_mb(index: int | None = None) -> tuple[int, int] | None:
    """(total, free) МБ для GPU index (по умолчанию первый из CUDA_VISIBLE_DEVICES) или None, если GPU нет."""
    if shutil.which("nvidia-smi") is None:
        return None
    if index is None:
        vis = os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",")[0].strip()
        index = int(vis) if vis.isdigit() else 0
    try:
        out = subprocess.run(["nvidia-smi", f"--id={index}", "--query-gpu=memory.total,memory.free",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=20).stdout
        total, free = (int(x) for x in out.strip().split(",")[:2])
        return total, free
    except Exception:  # noqa: BLE001
        return None


def gpu_processes() -> str:
    if shutil.which("nvidia-smi") is None:
        return ""
    try:
        return subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                               "--format=csv,noheader"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def wait_for_gpu(need_mb: int, wait_min: float) -> tuple[int, int] | None:
    """Ждать, пока освободится need_mb видеопамяти, не дольше wait_min минут. Возвращает (total, free)."""
    mem = gpu_memory_mb()
    deadline = time.time() + wait_min * 60
    while mem is not None and mem[1] < need_mb and time.time() < deadline:
        print(f"[gpu] свободно {mem[1]} МБ из {mem[0]}, нужно {need_mb} — жду освобождения "
              f"(ещё до {max(0, (deadline - time.time()) / 60):.0f} мин). Занято процессами:\n{gpu_processes()}",
              flush=True)
        time.sleep(60)
        mem = gpu_memory_mb()
    return mem


def choose(requested: str, need_mb: int, wait_min: float = 0.0) -> tuple[str, dict]:
    """requested: auto | GPU | CPU (или cuda/cpu). Возвращает ('GPU'|'CPU', info)."""
    req = requested.upper()
    if req in ("CPU",):
        return "CPU", {"reason": "задано явно"}
    mem = wait_for_gpu(need_mb, wait_min) if wait_min > 0 else gpu_memory_mb()
    if mem is None:
        return "CPU", {"reason": "GPU не найден (нет nvidia-smi)"}
    total, free = mem
    info = {"total_mb": total, "free_mb": free, "need_mb": need_mb}
    if free >= need_mb:
        return "GPU", info
    msg = f"свободно {free} МБ видеопамяти из {total}, нужно ≥ {need_mb}"
    print(f"[gpu] {msg}. Процессы на GPU:\n{gpu_processes()}", flush=True)
    if req == "GPU":
        raise SystemExit(f"[gpu] {msg}. Запустите с --device auto/CPU или --wait-gpu N (минут)")
    print("[gpu] → обучение на CPU", flush=True)
    return "CPU", {**info, "reason": msg}
