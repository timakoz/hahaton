#!/usr/bin/env bash
# Обучение моделей ForestMerge. Фоновый режим переживает закрытие SSH-консоли.
#
#   ./scripts/train.sh start                 # все шаги в фоне: prepare m2 m1 synth m3
#   ./scripts/train.sh start m1 synth m3     # только выбранные шаги, по порядку
#   ./scripts/train.sh status                # идёт ли обучение, шаг, хвост лога, GPU
#   ./scripts/train.sh log                   # смотреть лог (Ctrl+C — выйти, обучение продолжится)
#   ./scripts/train.sh stop                  # остановить
#   ./scripts/train.sh run m2                # на переднем плане (без фона)
#   ./scripts/train.sh run test              # тесты на образце
#
# Параметры — переменными окружения (по умолчанию в скобках):
#   GPU_WAIT_MIN (0)  — сколько минут ждать, пока на общем GPU освободится память (иначе сразу CPU)
#   M2_DEVICE (auto)  M2_ITERS (5000)  M2_GPU_MB (4096)
#   M1_DEVICE (auto)  M1_EPOCHS (40)   M1_BATCH (512)  M1_DMODEL (256)  M1_LAYERS (6)  M1_GPU_MB (6000)
#   SYNTH_N (4000)    SYNTH_WORKERS (ядра − 1)  NETWORK_FILE (реальная сеть ФГИС, если есть)
#   FEEDBACK_DB (data/app.db)
# Пример:  GPU_WAIT_MIN=120 M1_DMODEL=384 M1_LAYERS=8 ./scripts/train.sh start
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

PID_FILE="$LOG_DIR/train.pid"
STATUS_FILE="$LOG_DIR/train.status"
LATEST_PTR="$LOG_DIR/train_latest.txt"     # путь к последнему логу
ALL_STEPS=(prepare m2 m1 synth m3)
OPTIONAL_STEPS=" m2 m1 "                   # при ошибке этих шагов обучение продолжается
ML_PATTERN='ml\.(prepare_data|m2_impute|m1_encoder|synth|ranker|backtest|m4_llm)'

latest_log() { [[ -f "$LATEST_PTR" ]] && cat "$LATEST_PTR"; }
nproc_1() { local n; n=$(nproc 2>/dev/null || echo 2); echo $(( n > 1 ? n - 1 : 1 )); }

m2() {
  "$PY" -m ml.m2_impute.train --device "$1" --iterations "${M2_ITERS:-5000}" \
        --gpu-mem-mb "${M2_GPU_MB:-4096}" --wait-gpu "${GPU_WAIT_MIN:-0}"
}

run_step() {
  case "$1" in
    prepare) "$PY" -m ml.prepare_data ;;
    m2)
      # CatBoost при нехватке видеопамяти падает целиком (C++ abort) — повторяем на CPU
      if ! m2 "${M2_DEVICE:-auto}"; then
        [[ "${M2_DEVICE:-auto}" == "CPU" ]] && return 1
        echo "!!! M2 на GPU не удался — повтор на CPU"
        m2 CPU
      fi ;;
    m1)      "$PY" -m ml.m1_encoder.train --device "${M1_DEVICE:-auto}" --epochs "${M1_EPOCHS:-40}" \
                     --batch "${M1_BATCH:-512}" --d-model "${M1_DMODEL:-256}" --layers "${M1_LAYERS:-6}" \
                     --gpu-mem-mb "${M1_GPU_MB:-6000}" --wait-gpu "${GPU_WAIT_MIN:-0}" ;;
    synth)   "$PY" -m ml.synth.generate --n "${SYNTH_N:-4000}" --workers "${SYNTH_WORKERS:-$(nproc_1)}" \
                     --out data/synth ${NETWORK_FILE:+--network-file "$NETWORK_FILE"} ;;
    m3)      "$PY" -m ml.ranker.train --synth data/synth --feedback "${FEEDBACK_DB:-data/app.db}" ;;
    test)    "$PY" -m pytest -q ;;
    *)       echo "Неизвестный шаг: $1 (есть: ${ALL_STEPS[*]} test)"; return 2 ;;
  esac
}

run_all() {
  local steps=("$@") failed=()
  [[ ${#steps[@]} -eq 0 ]] && steps=("${ALL_STEPS[@]}")
  if [[ "${TRAIN_BG:-0}" == "1" ]]; then echo $$ > "$PID_FILE"; fi     # настоящий PID фонового процесса
  echo "python: $PY ($("$PY" --version 2>&1)); шаги: ${steps[*]}; PID $$"
  command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader || true
  for s in "${steps[@]}"; do
    echo "running $s $(date '+%F %T')" > "$STATUS_FILE"
    echo
    echo "===== [$(date '+%F %T')] шаг: $s ====="
    local t0; t0=$(date +%s)
    if run_step "$s"; then
      echo "===== шаг $s готов за $(( $(date +%s) - t0 )) с ====="
    else
      failed+=("$s")
      if [[ "$OPTIONAL_STEPS" == *" $s "* ]]; then
        echo "===== ОШИБКА на шаге $s — шаг необязательный, продолжаю ====="
      else
        echo "failed $s $(date '+%F %T')" > "$STATUS_FILE"
        echo "===== ОШИБКА на шаге $s — остановка ====="
        return 1
      fi
    fi
  done
  if [[ ${#failed[@]} -gt 0 ]]; then
    echo "done_with_errors (${failed[*]}) $(date '+%F %T')" > "$STATUS_FILE"
    echo; echo "===== ГОТОВО С ОШИБКАМИ в шагах: ${failed[*]} — см. лог выше ====="
  else
    echo "done ${steps[*]} $(date '+%F %T')" > "$STATUS_FILE"
    echo; echo "===== ВСЁ ГОТОВО [$(date '+%F %T')]. Модели: $ROOT/models ====="
  fi
}

is_running() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }
ml_procs() { pgrep -af "$ML_PATTERN" 2>/dev/null | grep -v pgrep || true; }

case "${1:-}" in
  start)
    shift
    if is_running; then echo "Уже идёт обучение (PID $(cat "$PID_FILE")). ./scripts/train.sh status"; exit 1; fi
    if [[ -n "$(ml_procs)" ]]; then
      echo "Внимание: уже работают процессы обучения (возможно, от прошлого запуска):"; ml_procs
      echo "Остановите их: ./scripts/train.sh stop   — или запустите заново после завершения."; exit 1
    fi
    LOG="$LOG_DIR/train_$(date +%Y%m%d_%H%M%S).log"
    echo "$LOG" > "$LATEST_PTR"
    rm -f "$PID_FILE"
    # setsid — новая сессия без управляющего терминала, nohup — игнор SIGHUP: закрытие SSH не убьёт процесс
    TRAIN_BG=1 nohup $SETSID bash "$ROOT/scripts/train.sh" run "$@" > "$LOG" 2>&1 < /dev/null &
    for _ in 1 2 3 4 5; do [[ -s "$PID_FILE" ]] && break; sleep 1; done
    if is_running; then
      echo "Обучение запущено в фоне. PID $(cat "$PID_FILE")"
    else
      echo "Процесс не стартовал или сразу завершился — см. лог:"; tail -n 20 "$LOG"; exit 1
    fi
    echo "Лог:        $LOG"
    echo "Смотреть:   ./scripts/train.sh log      (Ctrl+C выходит из просмотра, обучение продолжается)"
    echo "Состояние:  ./scripts/train.sh status"
    echo "Остановить: ./scripts/train.sh stop"
    ;;
  run)
    shift
    run_all "$@"
    ;;
  log)
    L="$(latest_log || true)"
    [[ -n "$L" && -e "$L" ]] || { echo "Логов ещё нет"; exit 1; }
    tail -n 200 -F "$L"
    ;;
  status)
    if is_running; then echo "ИДЁТ (PID $(cat "$PID_FILE"))"; else echo "фоновый процесс не запущен"; fi
    [[ -f "$STATUS_FILE" ]] && echo "статус: $(cat "$STATUS_FILE")"
    P="$(ml_procs)"; [[ -n "$P" ]] && { echo "процессы обучения:"; echo "$P"; }
    L="$(latest_log || true)"
    [[ -n "$L" && -e "$L" ]] && { echo "--- последние строки $L ---"; tail -n 15 "$L"; }
    if command -v nvidia-smi >/dev/null; then
      nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv
      echo "процессы на GPU:"; nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader || true
    fi
    ;;
  stop)
    if is_running; then
      PID=$(cat "$PID_FILE")
      PGID=$(ps -o pgid= -p "$PID" | tr -d ' ')
      kill -TERM -- "-$PGID" 2>/dev/null || kill -TERM "$PID" 2>/dev/null || true
    fi
    sleep 2
    # добить оставшиеся процессы обучения этого проекта (например, от старого запуска)
    pkill -TERM -f "$ML_PATTERN" 2>/dev/null || true
    sleep 2
    pkill -KILL -f "$ML_PATTERN" 2>/dev/null || true
    echo "stopped $(date '+%F %T')" > "$STATUS_FILE"
    rm -f "$PID_FILE"
    echo "Остановлено"
    ;;
  *)
    sed -n '2,20p' "$0"
    exit 1
    ;;
esac
