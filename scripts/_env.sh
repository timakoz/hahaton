# Общее для scripts/*.sh: корень проекта и выбор интерпретатора.
# PY можно задать явно:  PY=/usr/bin/python3.11 ./scripts/train.sh start
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ -z "${PY:-}" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PY="$ROOT/.venv/bin/python"          # окружение проекта (scripts/setup_server.sh)
  elif [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then
    PY="$ROOT/.venv/Scripts/python.exe"  # то же окружение под Windows (Git Bash)
  elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"           # системный python3 без окружения
  else
    echo "Не найден python3. Установите: sudo apt install python3 python3-venv python3-pip" >&2
    exit 1
  fi
fi
export PY ROOT
export PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
LOG_DIR="$ROOT/logs"
ME="$(id -un)"
if [[ "$(id -u)" == "0" && -n "${SUDO_USER:-}" ]]; then
  echo "Внимание: запуск через sudo — новые файлы будут принадлежать root, и потом без sudo не запишутся." >&2
  echo "Лучше один раз:  sudo chown -R $SUDO_USER: $ROOT   и запускать без sudo." >&2
fi
mkdir -p "$LOG_DIR" 2>/dev/null || true
if [[ ! -w "$LOG_DIR" ]] || { [[ -e "$LOG_DIR/train_latest.txt" ]] && [[ ! -w "$LOG_DIR/train_latest.txt" ]]; }; then
  echo "Нет прав на запись в $LOG_DIR (файлы создавались через sudo?)." >&2
  echo "Исправить:  sudo chown -R $ME: $ROOT" >&2
  exit 1
fi
# setsid (util-linux) отвязывает процесс от терминала; если его нет — хватает nohup
SETSID="$(command -v setsid || true)"
