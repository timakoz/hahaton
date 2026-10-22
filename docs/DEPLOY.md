# Перенос приложения на другой сервер и очистка обучающего сервера

Что переносится:

| что | как | размер |
|---|---|---|
| код + данные задачи (образец 5_x, ТО) | `git clone` из GitHub | ~50 МБ |
| обученные модели `models/`, правки специалистов `data/app.db`, проекты `data/projects/` | один архив `./scripts/state.sh export` | десятки МБ |
| окружение `.venv` | **не переносится** — ставится заново (на сервере приложения без torch: `APP_ONLY=1`) | — |
| `data/processed`, `data/synth`, `logs/` | **не нужны** приложению; для переобучения пересоздаются за минуты | — |

GPU на сервере приложения не нужен: модели работают на CPU (onnxruntime, CatBoost, LightGBM).

---

## 1. На обучающем сервере: упаковать модели

```bash
cd /opt/hahaton
./scripts/train.sh stop ; ./scripts/app.sh stop          # если что-то запущено
./scripts/state.sh check                                 # все 8 файлов моделей «ok» + сколько места занято
./scripts/state.sh export /tmp/forestmerge_state.tgz     # архив + его sha256
```

## 2. Скопировать архив на новый сервер

Если с обучающего сервера есть SSH-доступ к новому:
```bash
scp /tmp/forestmerge_state.tgz USER@NEW_SERVER:/tmp/
```
Если нет — через свой компьютер (две команды на ноутбуке):
```bash
scp mblp@OLD_SERVER:/tmp/forestmerge_state.tgz .
scp forestmerge_state.tgz USER@NEW_SERVER:/tmp/
```

## 3. На новом сервере: код, зависимости, модели

```bash
sudo mkdir -p /opt/hahaton && sudo chown "$USER": /opt/hahaton
git clone https://github.com/timakoz/hahaton.git /opt/hahaton      # приватный репо: логин GitHub + токен
cd /opt/hahaton
chmod +x scripts/*.sh
APP_ONLY=1 ./scripts/setup_server.sh                # .venv без torch (если нет venv: sudo apt install python3-venv)
./scripts/state.sh import /tmp/forestmerge_state.tgz   # sha256 должен совпасть с шагом 1
./scripts/state.sh check                            # все модели «ok»
./scripts/train.sh run test                         # 19 тестов: ядро на образце + авторизация
```

Для `git clone` приватного репозитория нужен вход: при запросе пароля вставьте
[personal access token](https://github.com/settings/tokens) (права `repo`, можно fine-grained на один репозиторий),
либо добавьте SSH-ключ сервера в GitHub и клонируйте `git@github.com:timakoz/hahaton.git`.

## 4. Запуск приложения

Вариант А — вручную в фоне:
```bash
./scripts/app.sh start                 # http://127.0.0.1:8765
./scripts/app.sh status | log | stop
```

Вариант Б — как сервис (автозапуск после перезагрузки, перезапуск при падении):
```bash
sed "s#__USER__#$USER#g; s#__DIR__#$PWD#g" deploy/forestmerge.service | sudo tee /etc/systemd/system/forestmerge.service
sudo systemctl daemon-reload && sudo systemctl enable --now forestmerge
systemctl status forestmerge            # логи: journalctl -u forestmerge -f
```
(Используйте что-то одно: А или Б, иначе порт 8765 будет занят.)

Приложение слушает только `127.0.0.1:8765` — снаружи к нему доступ через nginx с HTTPS (шаг 6).
Для себя без HTTPS можно через SSH-туннель: на ноутбуке `ssh -L 8765:localhost:8765 USER@NEW_SERVER`
→ http://localhost:8765.

## 5. Пользователи

Вход в приложение — по логину и паролю (все страницы и API закрыты; см. `api/auth.py`).
Пользователей создаёт администратор сервера из консоли:

```bash
./scripts/users.sh add ivanov --admin          # пароль спросит дважды (≥ 10 символов, не только строчные буквы)
./scripts/users.sh add jury --generate         # сгенерирует пароль и покажет один раз — удобно для жюри
./scripts/users.sh list                        # кто есть, роли, активные сессии
./scripts/users.sh passwd ivanov               # сменить пароль (сессии пользователя закроются)
./scripts/users.sh disable jury | enable jury | del jury
./scripts/users.sh logout                      # выкинуть всех (например, после показа)
```

* Роль `admin` дополнительно видит журнал правок (`/api/feedback`) и перезагружает модели; остальное — как у `user`.
* Сессия живёт 12 часов (`FM_SESSION_HOURS`), cookie `HttpOnly`, `SameSite=Lax`, при HTTPS — `Secure`.
* После 8 неверных паролей за 15 минут вход для этой пары «IP + логин» блокируется на 15 минут.
* Каждая правка решения в журнале хранит автора — это видно в данных для дообучения M3.
* Пользователи и сессии лежат в `data/app.db` и переносятся вместе с моделями (`./scripts/state.sh export`).

## 6. HTTPS на порту 443

Нужно: доменное имя с A-записью на IP сервера (например, `forest.example.ru`), открытые снаружи порты 80 и 443
(80 — для выдачи и продления сертификата Let's Encrypt и редиректа на HTTPS).

```bash
sudo ./scripts/setup_https.sh --domain forest.example.ru --email you@example.ru
```

Скрипт ставит nginx и certbot, получает бесплатный сертификат Let's Encrypt (принимает их условия от вашего имени),
пишет конфиг из [deploy/nginx-forestmerge.conf](../deploy/nginx-forestmerge.conf) — TLS 1.2/1.3, HTTP→HTTPS,
HSTS, защитные заголовки, лимит загрузки 200 МБ, таймауты 5 мин — и включает автопродление (таймер certbot,
после продления nginx перезагружается). Проверить продление: `sudo certbot renew --dry-run`.

Домена нет — самоподписанный сертификат (шифрование есть, но браузер покажет предупреждение, его нужно
принять вручную; для показа жюри лучше всё же домен — подойдёт и бесплатный поддомен):
```bash
sudo ./scripts/setup_https.sh --self-signed --domain 203.0.113.10       # IP сервера
```

После настройки: https://forest.example.ru → страница входа. Порт 8765 наружу не открывайте
(приложение и так слушает только 127.0.0.1). Если включён ufw, скрипт сам откроет 80 и 443;
в облаке (Yandex Cloud, Selectel, VK Cloud…) порты 80/443 открываются ещё и в группе безопасности.

Проверка: войти → «Открыть образец» → в параметрах галка «подключить обученные модели»,
оценка «правила + ML» → «Рассчитать». В карточке фрагмента появится столбец ML.

---

## 7. Очистка обучающего сервера

Делайте **только после** того, как на новом сервере `./scripts/state.sh check` показал все модели «ok»
и приложение открылось с моделями.

```bash
cd /opt/hahaton
./scripts/train.sh stop ; ./scripts/app.sh stop      # ничего не должно работать
du -sh /opt/hahaton ~/.cache/pip 2>/dev/null          # сколько освободится
cd ~ && sudo rm -rf /opt/hahaton                    # проект целиком: .venv, данные, модели, логи
rm -f /tmp/forestmerge_state.tgz
```

Дополнительно (кэш скачанных пакетов — torch и CUDA-библиотеки занимают несколько ГБ):
```bash
rm -rf ~/.cache/pip                    # кэш pip пользователя (безопасно: только ускоряет повторные установки)
sudo du -sh /root/.cache/pip 2>/dev/null && sudo rm -rf /root/.cache/pip   # если что-то ставили через sudo
```
Если сервис systemd ставили на обучающем сервере: `sudo systemctl disable --now forestmerge &&
sudo rm /etc/systemd/system/forestmerge.service && sudo systemctl daemon-reload`.

Если позже понадобится переобучить модели — всё восстанавливается из GitHub:
`git clone … && ./scripts/setup_server.sh && ./scripts/train.sh start` (~15 мин на 4090),
правки специалистов для M3 берутся из `data/app.db` с сервера приложения (`./scripts/state.sh export`).
