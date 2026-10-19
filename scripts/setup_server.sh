#!/usr/bin/env bash
# Установка зависимостей на Linux-сервере (один раз).
#
#   ./scripts/setup_server.sh                 # окружение .venv в папке проекта (рекомендуется)
#   USE_VENV=0 ./scripts/setup_server.sh      # без окружения, в пользовательский site-packages
#   PYTHON_BIN=python3.11 ./scripts/setup_server.sh
#   TORCH_INDEX=https://download.pytorch.org/whl/cu124 ./scripts/setup_server.sh   # своя сборка torch
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
USE_VENV="${USE_VENV:-1}"

command -v "$PYTHON_BIN" >/dev/null || { echo "Нет $PYTHON_BIN. Установите: sudo apt install python3 python3-venv python3-pip"; exit 1; }
"$PYTHON_BIN" - <<'EOF' || exit 1
import sys
if sys.version_info < (3, 10):
    sys.exit(f"Нужен Python ≥ 3.10, сейчас {sys.version.split()[0]}. Задайте PYTHON_BIN=python3.11 (или новее)")
print("Python", sys.version.split()[0])
EOF

PIP_FLAGS=()
if [[ "$USE_VENV" == "1" ]]; then
  if [[ ! -x .venv/bin/python ]]; then
    "$PYTHON_BIN" -m venv .venv || {
      echo "Не удалось создать окружение. Обычно не хватает пакета venv:"
      echo "  sudo apt install python3-venv     (или python3.X-venv под вашу версию)"
      echo "Либо запустите без окружения: USE_VENV=0 $0"
      exit 1; }
  fi
  PY="$ROOT/.venv/bin/python"
  echo "Окружение: $ROOT/.venv"
else
  PY="$(command -v "$PYTHON_BIN")"
  PIP_FLAGS=(--user)
  # Ubuntu 23.04+/Debian 12 запрещают pip в системный Python (PEP 668)
  if "$PY" -c "import sys, sysconfig, os; sys.exit(0 if os.path.exists(os.path.join(sysconfig.get_paths()['stdlib'], 'EXTERNALLY-MANAGED')) else 1)"; then
    PIP_FLAGS+=(--break-system-packages)
    echo "Внимание: системный Python помечен EXTERNALLY-MANAGED, ставлю с --break-system-packages в ~/.local"
  fi
  echo "Без окружения: $PY"
fi

"$PY" -m pip install -U pip wheel "${PIP_FLAGS[@]}"
"$PY" -m pip install -r requirements.txt "${PIP_FLAGS[@]}"

if command -v nvidia-smi >/dev/null; then
  nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader || true
  nvidia-smi | grep -o "CUDA Version: [0-9.]*" || true
else
  echo "nvidia-smi не найден — GPU-обучение будет недоступно (CatBoost: M2_DEVICE=CPU)"
fi
# На Linux колёса torch с PyPI уже собраны с CUDA 12.x. Свою сборку можно указать через TORCH_INDEX.
if [[ -n "${TORCH_INDEX:-}" ]]; then
  "$PY" -m pip install torch --index-url "$TORCH_INDEX" "${PIP_FLAGS[@]}"
else
  "$PY" -m pip install torch "${PIP_FLAGS[@]}"
fi
"$PY" -m pip install -r requirements-ml.txt "${PIP_FLAGS[@]}"

"$PY" - <<'EOF'
import torch, catboost, lightgbm, geopandas
print("torch", torch.__version__, "| CUDA:", torch.cuda.is_available(),
      "|", torch.cuda.get_device_name(0) if torch.cuda.is_available() else "-")
try:
    from catboost.utils import get_gpu_device_count
    print("catboost GPU:", get_gpu_device_count())
except Exception as e:
    print("catboost GPU: ?", e)
print("lightgbm", lightgbm.__version__, "| geopandas", geopandas.__version__)
EOF
echo
echo "Готово. Проверка ядра:  ./scripts/train.sh run test"
echo "Обучение в фоне:        ./scripts/train.sh start"
