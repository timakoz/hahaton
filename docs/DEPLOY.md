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
./scripts/train.sh run test                         # 10 тестов на образце
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

Доступ к приложению:
* **SSH-туннель** (безопасно, для себя): на ноутбуке `ssh -L 8765:localhost:8765 USER@NEW_SERVER` → http://localhost:8765
* **Открыть наружу** (для жюри/команды): в приложении нет авторизации, поэтому лучше nginx с паролем перед ним:
  ```nginx
  # /etc/nginx/sites-available/forestmerge     (пароль: sudo htpasswd -c /etc/nginx/.htpasswd team)
  server {
      listen 80;
      server_name _;
      client_max_body_size 200m;                 # загрузка выгрузок ТО
      location / {
          auth_basic "ForestMerge";
          auth_basic_user_file /etc/nginx/.htpasswd;
          proxy_pass http://127.0.0.1:8765;
          proxy_read_timeout 300;
      }
  }
  ```
  Без nginx (быстро, но открыто всем): `HOST=0.0.0.0 ./scripts/app.sh start` и открыть порт 8765 в файрволе.

Проверка: открыть страницу → «Открыть образец» → в параметрах галка «подключить обученные модели»,
оценка «правила + ML» → «Рассчитать». В карточке фрагмента появится столбец ML.

---

## 5. Очистка обучающего сервера

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
