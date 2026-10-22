#!/usr/bin/env bash
# Проверка домена перед выпуском HTTPS-сертификата. Запускать на сервере приложения.
#
#   ./scripts/check_domain.sh hakaton.softrush.ru
#
# Проверяет: куда указывает домен (A/AAAA) и совпадает ли это с публичным IP сервера, нет ли
# CAA-запрета для Let's Encrypt, кто занимает порты 80/443, работает ли приложение на 127.0.0.1:8765.
# Код выхода 0 — можно запускать setup_https.sh, 1 — есть блокирующие проблемы.
set -uo pipefail
D="${1:?укажите домен, например: ./scripts/check_domain.sh hakaton.softrush.ru}"
PROBLEMS=0
ok()   { echo "  [ok]   $*"; }
warn() { echo "  [!]    $*"; }
bad()  { echo "  [СТОП] $*"; PROBLEMS=$((PROBLEMS + 1)); }

resolve() {   # $1 домен, $2 4|6 → адреса через пробел
  python3 - "$1" "$2" <<'EOF'
import socket, sys
fam = socket.AF_INET if sys.argv[2] == "4" else socket.AF_INET6
try:
    addrs = {a[4][0] for a in socket.getaddrinfo(sys.argv[1], None, fam, socket.SOCK_STREAM)}
    print(" ".join(sorted(a for a in addrs if not a.startswith("::ffff:"))))
except socket.gaierror:
    print("")
EOF
}

echo "== Сервер"
PUB4=$(curl -4 -s --max-time 6 https://api.ipify.org 2>/dev/null || true)
PUB6=$(curl -6 -s --max-time 6 https://api6.ipify.org 2>/dev/null || true)
[[ "$PUB6" == *:* ]] || PUB6=""
if [[ -n "$PUB4" ]]; then ok "публичный IPv4: $PUB4"; else warn "не удалось узнать публичный IPv4 (нет выхода в интернет?)"; fi
[[ -n "$PUB6" ]] && ok "публичный IPv6: $PUB6"
LOCAL4=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v ':' | head -3 | tr '\n' ' ')
[[ -n "$PUB4" && -n "$LOCAL4" && " $LOCAL4 " != *" $PUB4 "* ]] && \
  warn "сервер за NAT (локальные адреса: $LOCAL4). Порты 80 и 443 публичного IP $PUB4 должны быть проброшены на этот сервер"

echo "== DNS: $D"
A=$(resolve "$D" 4)
AAAA=$(resolve "$D" 6)
if [[ -z "$A" ]]; then
  bad "у $D нет A-записи (или она ещё не разошлась). Нужна запись: hakaton → A → ${PUB4:-<IP сервера>}"
elif [[ -n "$PUB4" && " $A " != *" $PUB4 "* ]]; then
  bad "$D указывает на $A, а этот сервер — $PUB4. Исправьте A-запись (если это «звёздочка» *.домен — добавьте отдельную запись для этого имени)"
else
  ok "A: $A — это этот сервер"
fi
if [[ -n "$AAAA" ]]; then
  if [[ -n "$PUB6" && " $AAAA " == *" $PUB6 "* ]]; then
    ok "AAAA: $AAAA — это этот сервер"
  else
    bad "есть IPv6-запись AAAA: $AAAA — она не ведёт на этот сервер, Let's Encrypt проверит именно её. Удалите AAAA для этого имени (или укажите IPv6 сервера)"
  fi
else
  ok "AAAA нет (только IPv4) — нормально"
fi

if command -v dig >/dev/null; then
  name="$D"; caa=""
  while [[ "$name" == *.* && -z "$caa" ]]; do
    caa=$(dig +short CAA "$name" 2>/dev/null)
    [[ -z "$caa" ]] && name="${name#*.}"
  done
  if [[ -z "$caa" ]]; then
    ok "CAA-ограничений нет — Let's Encrypt может выдать сертификат"
  elif grep -qi "letsencrypt.org" <<<"$caa"; then
    ok "CAA у $name разрешает Let's Encrypt"
  else
    bad "CAA у $name не разрешает Let's Encrypt: $(echo $caa). Попросите добавить CAA 0 issue \"letsencrypt.org\""
  fi
else
  warn "нет утилиты dig — CAA не проверен (sudo apt install dnsutils)"
fi

echo "== Порты и приложение"
if command -v ss >/dev/null; then
  for p in 80 443; do
    who=$(ss -ltnpH "sport = :$p" 2>/dev/null | grep -o 'users:(("[^"]*' | head -1 | sed 's/users:(("//')
    if [[ -z "$(ss -ltnH "sport = :$p" 2>/dev/null)" ]]; then ok "порт $p свободен"
    elif [[ "$who" == nginx* || -z "$who" ]]; then ok "порт $p слушает ${who:-процесс (имя видно только под sudo)}"
    else warn "порт $p занят: $who — nginx не сможет его открыть"; fi
  done
fi
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  warn "включён файрвол ufw — setup_https.sh сам откроет 80 и 443"
fi
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:8765/api/health || true)
if [[ "$code" == "200" ]]; then ok "приложение отвечает на 127.0.0.1:8765"
else warn "приложение на 127.0.0.1:8765 не отвечает (код ${code:-нет}) — запустите сервис forestmerge"; fi

echo
if [[ "$PROBLEMS" -eq 0 ]]; then
  echo "Можно выпускать сертификат:  sudo ./scripts/setup_https.sh --domain $D --email <почта>"
  echo "(доступность портов 80/443 снаружи проверьте с телефона/ноутбука после установки nginx)"
  exit 0
fi
echo "Блокирующих проблем: $PROBLEMS — исправьте и запустите проверку снова (DNS обновляется от минут до часа)."
exit 1
