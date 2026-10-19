#!/usr/bin/env bash
# Веб-приложение в фоне.
#
#   ./scripts/app.sh start      # http://127.0.0.1:8765 (с ноутбука: ssh -L 8765:localhost:8765 user@server)
#   ./scripts/app.sh status | log | stop
#   PORT=8800 ./scripts/app.sh start
#   HOST=0.0.0.0 ./scripts/app.sh start    # открыть наружу (авторизации в приложении нет!)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

PORT="${PORT:-8765}"
HOST="${HOST:-127.0.0.1}"
PID_FILE="$LOG_DIR/app.pid"
LOG="$LOG_DIR/app.log"
is_running() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

case "${1:-}" in
  start)
    if is_running; then echo "Уже запущено (PID $(cat "$PID_FILE"))"; exit 1; fi
    nohup $SETSID "$PY" -m uvicorn api.main:app --host "$HOST" --port "$PORT" >> "$LOG" 2>&1 < /dev/null &
    echo $! > "$PID_FILE"
    sleep 2
    if is_running; then
      echo "Приложение: http://$HOST:$PORT   (PID $(cat "$PID_FILE"), лог $LOG)"
      [[ "$HOST" == "127.0.0.1" ]] && echo "С вашего компьютера: ssh -L $PORT:localhost:$PORT $(whoami)@<сервер>, затем http://localhost:$PORT"
    else
      echo "Не запустилось, см. $LOG"; tail -n 20 "$LOG"; exit 1
    fi
    ;;
  status) if is_running; then echo "работает (PID $(cat "$PID_FILE")), порт $PORT"; else echo "не запущено"; fi ;;
  log)    tail -n 100 -F "$LOG" ;;
  stop)
    if is_running; then kill -TERM "$(cat "$PID_FILE")"; echo "Остановлено"; else echo "Не запущено"; fi
    rm -f "$PID_FILE"
    ;;
  *) sed -n '2,8p' "$0"; exit 1 ;;
esac
