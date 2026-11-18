# Развёртывание «Аралтыша» на Jino

Рабочая ветка — [production-jino](https://github.com/yasg1988/sos-max-bot/tree/production-jino) в репозитории `yasg1988/sos-max-bot`. После проверки отправьте коммит в эту ветку. Файлы на VPS обновляются отдельным шагом. Публикация релизов из `main` выполняется отдельно.

## Рабочая схема

| Компонент | Конфигурация |
| --- | --- |
| VPS Jino | SSH на `195.161.68.148` с выданным ключом и проверкой ключа хоста |
| Публичный адрес | `https://araltysh.yasg.ru`, вебхук MAX — `https://araltysh.yasg.ru/webhook/max` |
| HTTP на VPS | Caddy, `/etc/caddy/Caddyfile`. Для приложения проксирует запросы на `127.0.0.1:8011`. Публичный HTTPS проверяется с внешней машины |
| Приложение | `araltysh.service` запускает Uvicorn (`app:app`) от пользователя `araltysh` из `/opt/araltysh` через `venv` |
| PostgreSQL | Кластер `18/main`, база `araltysh`, `127.0.0.1:5432` |
| Статистика | `araltysh-stats.timer` запускает `stats_report.py` ежедневно в 06:00 МСК |
| Резервное копирование | `araltysh-backup.timer` запускает `/usr/local/sbin/araltysh-backup` ежедневно в 00:15 UTC (03:15 МСК) |

Приложение и статистика загружают окружение из `/etc/araltysh.env` (`root:root`, права `0600`). Подключение к PostgreSQL задаётся переменными `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` и `DB_SSLMODE`. Для MAX используются `MAX_BOT_TOKEN`, `MAX_WEBHOOK_URL`, `MAX_WEBHOOK_SECRET` и `MAX_REGISTER_WEBHOOK`. Получатель статистики задаётся в `ARALTYSH_STATS_CHAT_ID`. Для доверенных сертификатов используются `SSL_CERT_FILE` и `REQUESTS_CA_BUNDLE`.

Храните вне Git файл окружения, SSH-ключи, рабочую конфигурацию Caddy, адресатов рассылок, журналы обращений и дампы. В репозитории хранятся [описания служб и таймеров](ops/systemd) и [скрипт резервного копирования](ops/sbin/araltysh-backup).

## Обновление на VPS

1. Перед коммитом проверьте рабочее дерево и синтаксис Python.

   ~~~bash
   git status --short --branch
   git diff --check
   python -m py_compile app.py stats_report.py
   ~~~

2. После push подготовьте архив конкретного проверенного коммита. Сохраните хеш предыдущей рабочей версии или копию текущих файлов вне Git. Перед изменением схемы БД подготовьте отдельный дамп.

   ~~~bash
   git fetch origin production-jino
   revision='<хеш проверенного коммита>'
   git merge-base --is-ancestor "$revision" origin/production-jino || exit 1
   git show --stat --oneline "$revision"
   git archive --format=tar -o "araltysh-$revision.tar" "$revision" \
     app.py stats_report.py requirements.txt assets combined-ca.pem certs ops
   scp "araltysh-$revision.tar" root@195.161.68.148:/root/
   ~~~

3. На VPS от `root` подставьте проверенный хеш. Проверьте синтаксис файлов в распакованном архиве. Если изменились зависимости, установите их в `/opt/araltysh/venv` перед копированием файлов.

   ~~~bash
   set -e
   revision='<хеш проверенного коммита>'
   staging=$(mktemp -d)
   tar -xf "/root/araltysh-$revision.tar" -C "$staging"
   /opt/araltysh/venv/bin/python -m py_compile "$staging/app.py" "$staging/stats_report.py"
   if ! cmp -s "$staging/requirements.txt" /opt/araltysh/requirements.txt; then
     /opt/araltysh/venv/bin/python -m pip install -r "$staging/requirements.txt"
   fi
   install -m 0644 "$staging/"{app.py,stats_report.py,requirements.txt,combined-ca.pem} /opt/araltysh/
   cp -a "$staging/"{assets,certs} /opt/araltysh/
   ~~~

   Если изменились unit-файлы, установите их из `$staging/ops/systemd` с правами `0644` в `/etc/systemd/system/`, затем выполните `systemctl daemon-reload`. При изменении расписания перезапустите соответствующий таймер. Если изменился скрипт резервного копирования, установите его из `$staging/ops/sbin` с правами `0700` в `/usr/local/sbin/araltysh-backup`. После установки файлов выполните `systemctl restart araltysh.service`.

4. Выполните проверки ниже и запишите хеш развёрнутой версии. При сбое верните файлы предыдущей рабочей версии, при необходимости восстановите её зависимости и перезапустите службу. Приложение выполняет миграции при старте. Перед откатом кода проверьте совместимость текущей схемы с предыдущей версией.

   После проверки удалите временный каталог и загруженный архив с VPS.

## Проверка после обновления

На VPS проверьте службы, внутренний HTTP, PostgreSQL и расписание.

~~~bash
systemctl is-active araltysh.service caddy.service postgresql@18-main.service
curl -fsS -o /dev/null -w 'Uvicorn HTTP %{http_code}\n' http://127.0.0.1:8011/health
curl -fsS -o /dev/null -w 'Caddy HTTP %{http_code}\n' -H 'Host: araltysh.yasg.ru' http://127.0.0.1/health
pg_lsclusters
runuser -u postgres -- psql -X -v ON_ERROR_STOP=1 -d araltysh -c 'SELECT 1;'
TZ=Europe/Moscow systemctl list-timers --all araltysh-stats.timer araltysh-backup.timer
systemctl show araltysh-stats.service araltysh-backup.service -p Id -p Result -p ExecMainStatus
~~~

Для проверки с учётными данными приложения запустите отчёт от пользователя службы с её файлом окружения. Режим `--dry-run` читает БД и печатает сводку в терминал.

~~~bash
systemd-run --wait --pipe --collect \
  -p User=araltysh -p Group=araltysh \
  -p WorkingDirectory=/opt/araltysh -p EnvironmentFile=/etc/araltysh.env \
  /opt/araltysh/venv/bin/python /opt/araltysh/stats_report.py --dry-run
~~~

С внешней машины откройте [публичный `/health`](https://araltysh.yasg.ru/health). Ожидаются HTTP `200`, `status=ok`, `service=sos-max-bot` и `webhook_url=https://araltysh.yasg.ru/webhook/max`. Доступность БД проверяйте отдельной командой. Для проверки подписки MAX убедитесь, что URL вебхука присутствует в ответе `GET /subscriptions` API MAX.

## Журналы за последние 24 часа

~~~bash
TZ=Europe/Moscow journalctl --since '24 hours ago' --no-pager \
  -u araltysh.service -u araltysh-stats.service -u araltysh-backup.service \
  -u postgresql@18-main.service -u caddy.service
~~~

При проверке вебхука ищите сообщения `Failed to process MAX update`. Обработчик журналирует исключение и может вернуть HTTP `200`. Ошибки SQL ищите в `/var/log/postgresql/postgresql-18-main.log` и ротированных файлах по полю базы `araltysh`. Кластер обслуживает несколько проектов. Перед передачей выдержек из журналов уберите данные обращений и идентификаторы пользователей.

## Резервные копии

Штатный скрипт хранит два последних архива `db-*.dump` в `/var/backups/araltysh`. Команда `pg_restore --list /var/backups/araltysh/<имя-архива>` проверяет, что оглавление читается. Перед работами с данными сохраните отдельный дамп вне каталога автоматической ротации. Очистку рабочих таблиц или семейных связей выполняйте только после явного подтверждения.
