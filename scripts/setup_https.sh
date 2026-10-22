#!/usr/bin/env bash
# HTTPS (порт 443) для ForestMerge через nginx. Запускать на сервере приложения через sudo.
#
#   sudo ./scripts/setup_https.sh --domain forest.example.ru --email you@example.ru   # сертификат Let's Encrypt
#   sudo ./scripts/setup_https.sh --self-signed --domain 203.0.113.10                 # без домена: самоподписанный
#
# Let's Encrypt: домен должен указывать (A-запись) на этот сервер, порты 80 и 443 открыты снаружи.
# Скрипт принимает условия Let's Encrypt (--agree-tos) от вашего имени. Сертификат продлевается
# автоматически (таймер certbot), после продления nginx перезагружается.
# Самоподписанный: соединение шифруется, но браузер покажет предупреждение (сертификат не доверенный).
#
# Приложение должно работать на 127.0.0.1:8765:  ./scripts/app.sh start   или сервис systemd (docs/DEPLOY.md)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DOMAIN="" EMAIL="" SELF=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --self-signed) SELF=1; shift ;;
    *) sed -n '2,13p' "$0"; exit 1 ;;
  esac
done
[[ "$(id -u)" == "0" ]] || { echo "Запустите через sudo"; exit 1; }
[[ -n "$DOMAIN" ]] || { echo "Укажите --domain (имя сайта или IP для --self-signed)"; exit 1; }
if [[ "$SELF" == "0" && -z "$EMAIL" ]]; then echo "Для Let's Encrypt укажите --email (уведомления об истечении)"; exit 1; fi

if command -v apt-get >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq nginx openssl
  [[ "$SELF" == "0" ]] && apt-get install -y -qq certbot
elif command -v dnf >/dev/null; then
  dnf install -y nginx openssl
  [[ "$SELF" == "0" ]] && dnf install -y certbot
else
  command -v nginx >/dev/null || { echo "Установите nginx (и certbot) вручную"; exit 1; }
fi

if [[ -d /etc/nginx/sites-available ]]; then
  CONF=/etc/nginx/sites-available/forestmerge.conf
  LINK=/etc/nginx/sites-enabled/forestmerge.conf
else
  CONF=/etc/nginx/conf.d/forestmerge.conf
  LINK=""
fi
enable_conf() { [[ -n "$LINK" ]] && ln -sfn "$CONF" "$LINK"; nginx -t && systemctl reload nginx || systemctl restart nginx; }
mkdir -p /var/www/certbot
systemctl enable --now nginx

# nginx < 1.25.1 не знает директиву «http2 on;» — тогда старый синтаксис
NGX=$(nginx -v 2>&1 | grep -o '[0-9]*\.[0-9]*\.[0-9]*' | head -1)
http2_new() { printf '%s\n%s\n' "1.25.1" "$NGX" | sort -V -C; }

render() {   # $1 cert, $2 key, $3 hsts-строка
  sed -e "s#__DOMAIN__#$DOMAIN#g" -e "s#__CERT__#$1#g" -e "s#__KEY__#$2#g" -e "s#__HSTS__#$3#g" \
      "$ROOT/deploy/nginx-forestmerge.conf" > "$CONF"
  if ! http2_new; then
    sed -i -e '/http2 on;/d' -e 's/listen 443 ssl;/listen 443 ssl http2;/' \
           -e 's/listen \[::\]:443 ssl;/listen [::]:443 ssl http2;/' "$CONF"
  fi
}

if [[ "$SELF" == "1" ]]; then
  D=/etc/ssl/forestmerge
  mkdir -p "$D"
  if [[ "$DOMAIN" =~ ^[0-9.]+$ || "$DOMAIN" == *:* ]]; then SAN="IP:$DOMAIN"; else SAN="DNS:$DOMAIN"; fi
  openssl req -x509 -nodes -newkey rsa:2048 -days 825 -subj "/CN=$DOMAIN" -addext "subjectAltName=$SAN" \
          -keyout "$D/key.pem" -out "$D/cert.pem" 2>/dev/null
  chmod 600 "$D/key.pem"
  render "$D/cert.pem" "$D/key.pem" "# HSTS выключен для самоподписанного сертификата"
  enable_conf
else
  # 1) временно только HTTP — чтобы Let's Encrypt проверил домен
  cat > "$CONF" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 404; }
}
EOF
  enable_conf
  certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" -m "$EMAIL" --agree-tos -n \
          --deploy-hook "systemctl reload nginx"
  # 2) полный конфиг с HTTPS
  render "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" "/etc/letsencrypt/live/$DOMAIN/privkey.pem" \
         'add_header Strict-Transport-Security "max-age=31536000" always;'
  enable_conf
  systemctl list-timers 2>/dev/null | grep -q certbot && echo "Автопродление: таймер certbot активен" \
    || echo "Проверьте автопродление: sudo certbot renew --dry-run"
fi

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  echo "ufw: открыты 80 и 443"
fi

echo
echo "Готово: https://$DOMAIN"
echo "Конфиг nginx: $CONF"
curl -sk -o /dev/null -w "Проверка 127.0.0.1:8765 (приложение): HTTP %{http_code}\n" http://127.0.0.1:8765/api/health \
  || echo "Приложение на 127.0.0.1:8765 не отвечает — запустите: ./scripts/app.sh start  (или сервис forestmerge)"
