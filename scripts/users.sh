#!/usr/bin/env bash
# Пользователи веб-приложения. Примеры:
#   ./scripts/users.sh add ivanov --admin        # пароль спросит
#   ./scripts/users.sh add petrov --generate     # сгенерирует и покажет один раз
#   ./scripts/users.sh list | passwd ivanov | disable petrov | enable petrov | del petrov | logout [--user ivanov]
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
exec "$PY" -m api.users "$@"
