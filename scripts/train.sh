#!/usr/bin/env bash
# Обучение моделей ForestMerge. Фоновый режим переживает закрытие SSH-консоли.
#
#   ./scripts/train.sh start                 # все шаги в фоне: prepare m2 m1 synth m3
#   ./scripts/train.sh start m1 synth m3     # только выбранные шаги, по порядку
#   ./scripts/train.sh status                # идёт ли обучение, последний шаг
#   ./scripts/train.sh log                   # смотреть лог (Ctrl+C — выйти, обучение продолжится)
#   ./scripts/train.sh stop                  # остановить
#   ./scripts/train.sh run m2                # на переднем плане (без фона)
#   ./scripts/train.sh run test              # тесты на образце
#
# Параметры — переменными окружения (значения по умолчанию в скобках):
#   M2_DEVICE (GPU)  M2_ITERS (5000)
#   M1_EPOCHS (40)   M1_BATCH (512)  M1_DMODEL (256)  M1_LAYERS (6)
#   SYNTH_N (4000)   SYNTH_WORKERS (число ядер − 1)   NETWORK_FILE (реальная сеть ФГИС, если есть)
#   FEEDBACK_DB (data/app.db)
# Пример:  M1_DMODEL=384 M1_LAYERS=8 SYNTH_N=8000 ./scripts/train.sh start
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

PID_FILE="$LOG_DIR/train.pid"
STATUS_FILE="$LOG_DIR/train.status"
LATEST_PTR="$LOG_DIR/train_latest.txt"     # путь к последнему логу (без симлинков)
latest_log() { [[ -f "$LATEST_PTR" ]] && cat "$LATEST_PTR"; }
ALL_STEPS=(prepare m2 m1 synth m3)

nproc_1() { local n; n=$(nproc 2>/dev/null || echo 2); echo $(( n > 1 ? n - 1 : 1 )); }

run_step() {
  case "$1" in
    prepare) "$PY" -m ml.prepare_data ;;
    m2)      "$PY" -m ml.m2_impute.train --device "${M2_DEVICE:-GPU}" --iterations "${M2_ITERS:-5000}" ;;
    m1)      "$PY" -m ml.m1_encoder.train --epochs "${M1_EPOCHS:-40}" --batch "${M1_BATCH:-512}" \
                     --d-model "${M1_DMODEL:-256}" --layers "${M1_LAYERS:-6}" ;;
    synth)   "$PY" -m ml.synth.generate --n "${SYNTH_N:-4000}" --workers "${SYNTH_WORKERS:-$(nproc_1)}" \
                     --out data/synth ${NETWORK_FILE:+--network-file "$NETWORK_FILE"} ;;
    m3)      "$PY" -m ml.ranker.train --synth data/synth --feedback "${FEEDBACK_DB:-data/app.db}" ;;
    test)    "$PY" -m pytest -q ;;
    *)       echo "Неизвестный шаг: $1 (есть: ${ALL_STEPS[*]} test)"; return 2 ;;
  esac
}

run_all() {
  local steps=("$@")
  [[ ${#steps[@]} -eq 0 ]] && steps=("${ALL_STEPS[@]}")
  echo "python: $PY ($("$PY" --version 2>&1)); шаги: ${steps[*]}"
  command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader || true
  for s in "${steps[@]}"; do
    echo "running $s $(date '+%F %T')" > "$STATUS_FILE"
    echo
    echo "===== [$(date '+%F %T')] шаг: $s ====="
    local t0; t0=$(date +%s)
    if ! run_step "$s"; then
      echo "failed $s $(date '+%F %T')" > "$STATUS_FILE"
      echo "===== ОШИБКА на шаге $s ====="
      return 1
    fi
    echo "===== шаг $s готов за $(( $(date +%s) - t0 )) с ====="
  done
  echo "done ${steps[*]} $(date '+%F %T')" > "$STATUS_FILE"
  echo
  echo "===== ВСЁ ГОТОВО [$(date '+%F %T')]. Модели: $ROOT/models ====="
}

is_running() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

case "${1:-}" in
  start)
    shift
    if is_running; then echo "Уже идёт обучение (PID $(cat "$PID_FILE")). ./scripts/train.sh status"; exit 1; fi
    LOG="$LOG_DIR/train_$(date +%Y%m%d_%H%M%S).log"
    echo "$LOG" > "$LATEST_PTR"
    # setsid — новая сессия без управляющего терминала, nohup — игнор SIGHUP: закрытие SSH не убьёт процесс
    nohup $SETSID bash "$ROOT/scripts/train.sh" run "$@" > "$LOG" 2>&1 < /dev/null &
    echo $! > "$PID_FILE"
    sleep 1
    echo "Обучение запущено в фоне. PID $(cat "$PID_FILE")"
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
    if is_running; then echo "ИДЁТ (PID $(cat "$PID_FILE"))"; else echo "не запущено"; fi
    [[ -f "$STATUS_FILE" ]] && echo "статус: $(cat "$STATUS_FILE")"
    L="$(latest_log || true)"
    [[ -n "$L" && -e "$L" ]] && { echo "--- последние строки $L ---"; tail -n 15 "$L"; }
    command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv || true
    ;;
  stop)
    if ! is_running; then echo "Не запущено"; exit 0; fi
    PID=$(cat "$PID_FILE")
    PGID=$(ps -o pgid= -p "$PID" | tr -d ' ')
    kill -TERM -- "-$PGID" 2>/dev/null || kill -TERM "$PID"
    sleep 2
    kill -0 "$PID" 2>/dev/null && kill -KILL -- "-$PGID" 2>/dev/null || true
    echo "stopped $(date '+%F %T')" > "$STATUS_FILE"
    echo "Остановлено (группа процессов $PGID)"
    ;;
  *)
    sed -n '2,20p' "$0"
    exit 1
    ;;
esac
