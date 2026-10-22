# Полная инструкция: ForestMerge на https://hakaton.softrush.ru

От пустого сервера до работающего сайта с логином и HTTPS, плюс перенос обученных моделей и очистка
обучающего сервера. Команды выполняются по порядку; в каждом шаге указано, **где** её запускать.

---

## 0. Как это устроено (2 минуты теории)

```
браузер ──▶ DNS: «hakaton.softrush.ru = IP_НОВОГО_СЕРВЕРА» ──▶ новый сервер
                                                                   │  nginx :443 (сертификат, HTTPS)
                                                                   │  nginx :80  (только редирект на HTTPS)
                                                                   └─▶ приложение 127.0.0.1:8765 (вход по логину)
```

* **Домен** `softrush.ru` уже ваш. Имена вида `что-то.softrush.ru` (поддомены) настраиваются в **DNS** —
  это «телефонная книга»: имя → IP-адрес сервера. Запись «имя → IPv4» называется **A-запись**.
* DNS домена `softrush.ru` обслуживает **Selectel** (серверы имён `a/b/c/d.ns.selectel.ru`), то есть записи
  меняются в панели Selectel — доступ, видимо, у коллеги.
* Сейчас в DNS есть **«звёздочка»** `*.softrush.ru` → `212.69.86.212`: любое имя, в том числе
  `hakaton.softrush.ru`, ведёт на этот адрес (на него же указывает сам `softrush.ru`). Отдельная A-запись
  `hakaton` важнее звёздочки — её и нужно добавить. Остальные поддомены это не затронет.
* **Сертификат** (замок в браузере) выпускает бесплатно Let's Encrypt. Он проверяет, что имя действительно
  ведёт на ваш сервер, поэтому сначала DNS, потом сертификат. Сертификат живёт 90 дней и продлевается
  автоматически. Проверено: IPv6-записей (AAAA) и CAA-ограничений у домена нет — Let's Encrypt подходит.

Что понадобится:

| | |
|---|---|
| новый сервер | Linux (лучше Ubuntu 22.04/24.04), ≥ 2 CPU, ≥ 4 ГБ RAM, ≥ 5 ГБ диска, **публичный IP**, `sudo` |
| доступ к DNS | человек с доступом к панели Selectel для `softrush.ru` |
| порты | 80 и 443 открыты снаружи к новому серверу |
| GitHub | токен для клонирования приватного репозитория (шаг 5) |
| почта | любая ваша — Let's Encrypt пришлёт предупреждение, если продление сломается |

---

## Шаг 1. Узнать IP нового сервера — **новый сервер**

```bash
curl -4 -s https://api.ipify.org ; echo
```

* Получили **`212.69.86.212`** — это адрес роутера Proxmox (VM 100), через который выходят в интернет все
  ваши VM. Если приложение ставите на **одну из VM за роутером** — DNS трогать **не нужно**, идите по разделу
  **«Сценарий В»** в конце (HTTPS настраивается на роутере). Если ставите **на сам роутер** — «Сценарий Б».
* Получили **другой адрес** (отдельный сервер со своим публичным IP) — запишите его, это `IP_НОВОГО_СЕРВЕРА`. Шаг 2.

## Шаг 2. A-запись в DNS — **коллега с доступом к Selectel**

Сообщение коллеге (подставьте IP):

> Привет! Добавь, пожалуйста, в DNS-зону softrush.ru (Selectel) запись:
> **имя `hakaton`, тип `A`, значение `IP_НОВОГО_СЕРВЕРА`, TTL 300.**
> Звёздочку `*.softrush.ru` не трогай — отдельная запись `hakaton` перекроет её только для этого имени.
> AAAA и CAA для `hakaton` не нужны. Сайт будет на https://hakaton.softrush.ru, сертификат Let's Encrypt
> выпустим сами на своём сервере.

В панели Selectel это раздел **DNS-хостинг** → зона `softrush.ru` → «Добавить запись» → тип A, имя `hakaton`
(полное имя получится `hakaton.softrush.ru`), значение — IP, TTL 300 → сохранить.

Проверка (через 1–15 минут, иногда до часа), с любого компьютера:
```bash
nslookup hakaton.softrush.ru 8.8.8.8
```
Должен вернуться `IP_НОВОГО_СЕРВЕРА`, а не `212.69.86.212`. На Windows та же команда работает в PowerShell.

## Шаг 3. Открыть порты 80 и 443 — **новый сервер / панель хостинга**

* Если сервер в облаке (Selectel, Yandex Cloud, VK Cloud…): в панели в **группе безопасности / файрволе**
  сервера разрешите входящие TCP 80 и 443 (и 22 для SSH — он, скорее всего, уже открыт).
* Файрвол самого сервера `ufw` (если включён) откроет скрипт HTTPS на шаге 9.
* Порт 8765 открывать **не нужно**: приложение слушает только `127.0.0.1`.

## Шаг 4. Упаковать модели — **обучающий сервер** (`mblp@44116`)

```bash
cd /opt/hahaton
git pull                                   # нужны новые скрипты (state.sh)
chmod +x scripts/*.sh
./scripts/train.sh stop ; ./scripts/app.sh stop
./scripts/state.sh check                   # все 8 файлов моделей — «ok»
./scripts/state.sh export /tmp/forestmerge_state.tgz
```
Скрипт выведет размер архива и его `sha256` — запомните первые символы суммы.
Если `git pull` ругается на локальные изменения в `scripts/…` (это от `chmod +x`):
`git checkout -- scripts/ && git pull`.

## Шаг 5. Код и зависимости — **новый сервер**

```bash
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip curl dnsutils
sudo mkdir -p /opt/hahaton && sudo chown "$USER": /opt/hahaton
git clone https://github.com/timakoz/hahaton.git /opt/hahaton
cd /opt/hahaton && chmod +x scripts/*.sh
APP_ONLY=1 ./scripts/setup_server.sh       # окружение .venv без torch/GPU, ~0,5 ГБ, несколько минут
```

Репозиторий приватный: `git clone` спросит логин GitHub и **пароль — вместо него вставьте токен**.
Токен: GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** →
Generate new token → Repository access: *Only select repositories* → `hahaton` → Permissions:
*Contents: Read-only* → Generate. Скопируйте сразу, он показывается один раз.
Чтобы не вводить его при каждом `git pull`: `git config --global credential.helper store`
(токен сохранится в `~/.git-credentials` — только на этом сервере).

## Шаг 6. Перенести модели — **с обучающего на новый сервер**

Если с обучающего сервера есть SSH-доступ к новому (запускать на обучающем):
```bash
scp /tmp/forestmerge_state.tgz USER@IP_НОВОГО_СЕРВЕРА:/tmp/
```
Иначе через свой компьютер (две команды на ноутбуке):
```bash
scp mblp@ОБУЧАЮЩИЙ_СЕРВЕР:/tmp/forestmerge_state.tgz .
scp forestmerge_state.tgz USER@IP_НОВОГО_СЕРВЕРА:/tmp/
```
Затем **на новом сервере**:
```bash
cd /opt/hahaton
./scripts/state.sh import /tmp/forestmerge_state.tgz   # sha256 должен совпасть с шагом 4
./scripts/state.sh check                               # все модели «ok»
./scripts/train.sh run test                            # 19 тестов: ядро + авторизация
```
Импорт делайте **до** создания пользователей: он заменяет `data/app.db` (там же пользователи;
старая копия сохраняется в `data/app.db.bak-…`).

## Шаг 7. Пользователи — **новый сервер**

```bash
./scripts/users.sh add ВАШ_ЛОГИН --admin       # пароль спросит дважды: ≥ 10 символов, не только строчные буквы
./scripts/users.sh add jury --generate         # для жюри: пароль сгенерируется и покажется один раз
./scripts/users.sh list
```
Остальные команды: `passwd ЛОГИН` (смена пароля), `disable/enable ЛОГИН`, `del ЛОГИН`,
`logout` (закрыть все сессии, например после показа). Подробности — в конце файла.

## Шаг 8. Приложение как сервис — **новый сервер**

Сервис сам поднимется после перезагрузки и перезапустится при падении:
```bash
cd /opt/hahaton
sed "s#__USER__#$USER#g; s#__DIR__#$PWD#g" deploy/forestmerge.service | sudo tee /etc/systemd/system/forestmerge.service
sudo systemctl daemon-reload
sudo systemctl enable --now forestmerge
systemctl status forestmerge --no-pager          # должно быть «active (running)»
curl -s http://127.0.0.1:8765/api/health ; echo  # {"ok":true,...}
```
Логи: `journalctl -u forestmerge -f`. Перезапуск: `sudo systemctl restart forestmerge`.
(Вместо сервиса можно `./scripts/app.sh start`, но не одновременно — займут один порт.)

## Шаг 9. HTTPS для hakaton.softrush.ru — **новый сервер**

Сначала проверка готовности домена (ничего не меняет):
```bash
./scripts/check_domain.sh hakaton.softrush.ru
```
Все пункты «[ok]» → выпускаем сертификат:
```bash
sudo ./scripts/setup_https.sh --domain hakaton.softrush.ru --email ВАША_ПОЧТА
```
Скрипт: ставит nginx и certbot → ещё раз проверяет домен → получает сертификат Let's Encrypt
(принимает их условия от вашего имени) → пишет конфиг nginx (HTTPS на 443, редирект с 80, TLS 1.2/1.3,
HSTS, загрузка до 200 МБ) → включает автопродление. Проверить продление:
```bash
sudo certbot renew --dry-run
```

Если `check_domain.sh` пишет «[СТОП]»:
* «указывает на 212.69.86.212» — A-запись из шага 2 ещё не создана или не разошлась (подождите, проверьте `nslookup`);
* «нет A-записи» — запись создана с опечаткой в имени;
* «есть AAAA» — в Selectel появилась IPv6-запись для `hakaton`, её нужно удалить.

## Шаг 10. Проверка — **с ноутбука и телефона**

1. Открыть **https://hakaton.softrush.ru** → замок в адресной строке, страница входа.
2. Войти → «Открыть образец» → в параметрах галка «подключить обученные модели», оценка «правила + ML» →
   «Рассчитать» → в карточке фрагмента есть столбец ML.
3. `http://hakaton.softrush.ru` (без s) должен перекидывать на https.
4. С телефона через мобильный интернет (не Wi-Fi офиса) — убедиться, что сайт виден снаружи.

## Шаг 11. Очистка обучающего сервера — **обучающий сервер**

Только после шага 10!
```bash
cd /opt/hahaton && ./scripts/train.sh stop ; ./scripts/app.sh stop
du -sh /opt/hahaton ~/.cache/pip 2>/dev/null             # сколько освободится
cd ~ && sudo rm -rf /opt/hahaton                         # проект целиком: .venv, данные, модели, логи
rm -f /tmp/forestmerge_state.tgz
rm -rf ~/.cache/pip                                      # кэш pip (torch и CUDA-библиотеки — несколько ГБ)
sudo du -sh /root/.cache/pip 2>/dev/null && sudo rm -rf /root/.cache/pip   # если ставили через sudo
```
llama-server и чужие файлы это не трогает. Переобучить модели позже: `git clone …`,
`./scripts/setup_server.sh`, `./scripts/train.sh start` (~15 мин на 4090); правки специалистов для M3
забрать с нового сервера: `./scripts/state.sh export`.

---

## Сценарий Б: новый сервер = 212.69.86.212

Если на шаге 1 вышел `212.69.86.212`, имя `hakaton.softrush.ru` уже ведёт на этот сервер (через звёздочку),
DNS менять не нужно. Но на этом сервере, скорее всего, уже работает сайт `softrush.ru` на портах 80/443:
1. Посмотрите, кто их занимает: `sudo ss -ltnp | grep -E ':(80|443) '`.
2. Если это **nginx** — `setup_https.sh` подходит: он добавит **отдельный** файл
   `/etc/nginx/sites-available/forestmerge.conf` только для имени `hakaton.softrush.ru`, существующие сайты
   не трогает и перед применением проверяет конфиг `nginx -t`. Всё же предупредите коллегу, который ведёт сервер.
3. Если там **Apache, Caddy, Traefik, Docker** и т.п. — не запускайте `setup_https.sh`; попросите
   владельца сервера добавить проксирование `hakaton.softrush.ru` → `http://127.0.0.1:8765`
   (с заголовками `X-Forwarded-Proto` и `X-Real-IP`, лимит тела запроса 200 МБ). Если у компании есть
   сертификат на `*.softrush.ru`, его можно использовать вместо Let's Encrypt.

---

## Сценарий В: Proxmox — сайты раздаёт роутер-VM (наш случай, если приложение на VM за роутером)

```
интернет ─▶ 212.69.86.212 = VM 100 (router): 80/443, сертификаты, выбор VM по имени сайта
                  │  hakaton.softrush.ru ─▶ http://APP_VM_IP:8765  (внутренняя сеть intranet/localnetwork)
                  └─ другие *.softrush.ru ─▶ свои VM
```
Тогда: DNS менять **не нужно** (звёздочка уже ведёт на роутер), `setup_https.sh` на VM приложения
**не запускать** — HTTPS и сертификат делаются на роутере, рядом с остальными сайтами.

**1. Найти, чем роутер раздаёт сайты** — в консоли VM 100 (Proxmox → 100 → Console, или ssh):
```bash
sudo ss -ltnp | grep -E ':(80|443) '        # кто слушает 80/443: nginx / haproxy / caddy / traefik / docker-proxy
sudo docker ps 2>/dev/null                   # Nginx Proxy Manager, Traefik, Caddy в контейнерах
sudo nginx -T 2>/dev/null | grep -nE 'server_name|proxy_pass|ssl_certificate '   # все сайты nginx разом
ls /etc/nginx/sites-enabled /etc/nginx/conf.d /etc/haproxy /etc/caddy 2>/dev/null
sudo iptables -t nat -S 2>/dev/null | grep DNAT ; sudo nft list ruleset 2>/dev/null | grep -i dnat
```
| увидели | где настраивать |
|---|---|
| `nginx` | файлы в `/etc/nginx/sites-enabled/` или `/etc/nginx/conf.d/` — по одному на сайт; `nginx -T` показывает все |
| контейнер `jc21/nginx-proxy-manager` | веб-интерфейс `http://<IP роутера>:81` → Proxy Hosts → Add |
| `haproxy` | `/etc/haproxy/haproxy.cfg` (правила `acl … hdr(host)` / `req.ssl_sni`) |
| `caddy` | `/etc/caddy/Caddyfile` |
| `traefik` | `traefik.yml` / метки (labels) контейнеров |
| ничего, только DNAT в iptables/nft | роутер просто пересылает порты на одну VM — сайты раздаёт та VM, ищите там |
| pfSense / OPNsense (веб-панель) | Services → HAProxy (или nginx), Firewall → NAT |

**2. VM для приложения.** Возьмите существующую подходящую (например, `106 (app)`) или новую
(`101 (ubuntu-26.04)`), подключённую к той же внутренней сети, что и роутер. Узнайте её адрес: `ip -4 addr`
(это `APP_VM_IP`), и адрес роутера во внутренней сети: `ip route | grep default` (это `ROUTER_IP`).
Дальше на ней — шаги 4–7 основной инструкции (код, модели, пользователи).
На Ubuntu 26.04 Python может быть слишком новым для части колёс (catboost, onnxruntime); если
`setup_server.sh` упадёт на установке — `sudo apt install python3.12 python3.12-venv` и
`PYTHON_BIN=python3.12 APP_ONLY=1 ./scripts/setup_server.sh`.

**3. Сервис приложения слушает внутреннюю сеть и доверяет только роутеру** (вместо шага 8):
```bash
cd /opt/hahaton
ROUTER_IP=10.0.0.1          # ← подставьте свой
sed "s#__USER__#$USER#g; s#__DIR__#$PWD#g; s#FM_HOST=127.0.0.1#FM_HOST=0.0.0.0#; s#FM_FWD=127.0.0.1#FM_FWD=$ROUTER_IP#; s#FM_COOKIE_SECURE=auto#FM_COOKIE_SECURE=1#" \
    deploy/forestmerge.service | sudo tee /etc/systemd/system/forestmerge.service
sudo systemctl daemon-reload && sudo systemctl enable --now forestmerge
# порт 8765 — только для роутера (если на VM включён ufw)
sudo ufw allow from $ROUTER_IP to any port 8765 proto tcp
```
Проверка с роутера: `curl -s http://APP_VM_IP:8765/api/health` → `{"ok":true,...}`.

**4. Добавить сайт на роутере.** Для nginx — готовый блок [deploy/router-nginx-hakaton.conf](../deploy/router-nginx-hakaton.conf):
лучше скопировать файл соседнего сайта (в нём уже верные пути к сертификату), поменять `server_name` на
`hakaton.softrush.ru` и `proxy_pass` на `http://APP_VM_IP:8765`, проверить заголовки как в шаблоне, затем
`sudo nginx -t && sudo systemctl reload nginx`. Сертификат: если у соседних сайтов общий `*.softrush.ru` —
те же пути; иначе на роутере `sudo certbot certonly --webroot -w /var/www/certbot -d hakaton.softrush.ru`
(или `sudo certbot --nginx -d hakaton.softrush.ru`, если там так уже делали).
Для Nginx Proxy Manager: Proxy Hosts → Add → Domain `hakaton.softrush.ru`, Scheme `http`,
Forward IP `APP_VM_IP`, Port `8765`; вкладка SSL → Request a new certificate (или существующий wildcard),
Force SSL; Advanced → `client_max_body_size 200m;`.

Обязательно, чтобы прокси передавал `Host` (исходное имя сайта) и `X-Forwarded-Proto` — иначе вход
будет отклонён как «запрос с чужого сайта». `X-Real-IP` нужен для защиты от перебора паролей.

---

## Потом: обновление приложения

```bash
cd /opt/hahaton && git pull && ./scripts/train.sh run test && sudo systemctl restart forestmerge
```
Новые модели после переобучения: на обучающем сервере `./scripts/state.sh export`, на этом —
`tar xzf архив.tgz models` (только модели, пользователи не затрагиваются) и
`sudo systemctl restart forestmerge`. Полный `./scripts/state.sh import` заменит и `data/app.db`.

## Пользователи и безопасность — справка

* Все страницы и API закрыты, кроме страницы входа. Пароли — PBKDF2-SHA256 (600 000 итераций), в базе
  только хеши. Сессия — 12 часов (`FM_SESSION_HOURS`), cookie `HttpOnly`, `SameSite=Lax`, `Secure`.
* После 8 неверных паролей за 15 минут вход для пары «IP + логин» блокируется на 15 минут.
* Роль `admin` дополнительно видит журнал правок и перезагружает модели. Автор каждой правки
  сохраняется — пригодится при дообучении M3.
* Пользователи, сессии и правки — в `/opt/hahaton/data/app.db`; бэкап — `./scripts/state.sh export`.

## Если что-то пошло не так

| Симптом | Что проверить |
|---|---|
| `nslookup` показывает 212.69.86.212 | A-запись `hakaton` ещё не создана / не разошлась (TTL); имя без опечаток |
| certbot: `Timeout during connect` / `Connection refused` | порт 80 закрыт в облачном файрволе или ufw; домен указывает не туда |
| certbot: `too many failed authorizations` | лимит Let's Encrypt — подождите час, сначала добейтесь «[ok]» в `check_domain.sh` |
| браузер: «502 Bad Gateway» | приложение не запущено: `systemctl status forestmerge`, `journalctl -u forestmerge -n 50` |
| браузер: «413 Request Entity Too Large» | файл > 200 МБ — увеличьте `client_max_body_size` в `/etc/nginx/sites-available/forestmerge.conf`, `sudo systemctl reload nginx` |
| страница входа: «пользователей ещё нет» | шаг 7 |
| «слишком много неудачных попыток» | подождите 15 мин или `sudo systemctl restart forestmerge` (сбрасывает счётчик) |
| забыли пароль администратора | `./scripts/users.sh passwd ЛОГИН` на сервере |
| сертификат не продлился | `sudo certbot renew --dry-run`, `systemctl list-timers \| grep certbot` |
