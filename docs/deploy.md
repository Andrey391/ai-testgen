# Хранилище и установка: PostgreSQL, S3, воркеры

Все данные студии хранятся только в PostgreSQL, и при одном экземпляре, и при установке на команду.
Без базы студия, воркер и `python -m testgen.run` не запускаются. Для команды на 20–50 человек к той же
базе добавляются воркеры, несколько экземпляров студии и, при желании, S3 для больших файлов.

| | Один экземпляр | Команда |
|---|---|---|
| Проекты, тесты, задачи, прогоны, пользователи, журнал действий | PostgreSQL (`TESTGEN_DATABASE_URL`) | та же база |
| Скриншоты, trace, эталоны, файлы для загрузки | в той же базе | S3: MinIO, Yandex Object Storage (`TESTGEN_S3_*`) или в базе |
| Секреты (логины приложения, токены подключений, ключи API) | в базе, только зашифрованные (`TESTGEN_SECRET_KEY`) | то же или HashiCorp Vault |
| Прогоны, наборы, мутации, Planner | очередь в базе, встроенный воркер студии | очередь в базе, воркеры `python -m testgen.worker` |
| Сессии Studio (Live View), конвейер | в процессе студии | в экземпляре, который их начал; остальные экземпляры передают запросы ему |

На диске остаётся только кэш (`TESTGEN_CACHE_DIR`, по умолчанию `%LOCALAPPDATA%\aitestgen\cache`, на
Linux — `~/.cache/aitestgen`): браузеру файлы нужны локально. Потеря кэша ничего не теряет.

## Локальный запуск

PostgreSQL на этом же компьютере (служба Windows, `postgres:17` в Docker — любой):

```powershell
& "C:\Program Files\PostgreSQL\17\bin\psql.exe" -h 127.0.0.1 -U postgres -c "CREATE ROLE testgen LOGIN CREATEDB PASSWORD 'testgen'" -c "CREATE DATABASE testgen OWNER testgen"
$env:TESTGEN_DATABASE_URL = "postgresql+psycopg://testgen:testgen@127.0.0.1:5432/testgen"
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python server.py
```

Схема создаётся при первом запуске. Если база на этом компьютере (`localhost`, `127.0.0.1`), а
`TESTGEN_SECRET_KEY` не задан, студия один раз создаёт ключ шифрования секретов
`%LOCALAPPDATA%\aitestgen\secret.key` и дальше берёт его оттуда. Сохраните копию этого файла: без него
секреты в базе не прочитать. Для базы на другом сервере ключ задаётся только переменной.

## Как это устроено

- **Хранилище** (`testgen/fs.py`). Модули обращаются к данным по путям вида
  `data/projects/<id>/project.json`. Путь — это ключ строки в таблице `docs` (JSON-документ целиком,
  колонки `project_id` и `updated` для отчётов). У тестов, задач, прогонов и расхода модели свои таблицы
  с колонками для фильтров (`testgen/repo/`). Бинарные файлы с `TESTGEN_S3_BUCKET` уходят в бакет под
  тем же ключом. Скриншоты шагов пишутся в локальный кэш и по ходу прогона отправляются в базу, а при
  чтении из другого экземпляра скачиваются.
- **Блокировки.** Изменения одного документа (тест, пользователи, журнал действий) сериализуются между
  процессами advisory lock PostgreSQL, строки тестов и задач — `SELECT … FOR UPDATE`.
- **Очередь** (`testgen/workqueue.py`). «Запустить», наборы, проверка мутациями и Planner ставят
  задание в таблицу `work`; воркер берёт его атомарным `UPDATE … WHERE status = 'queued'`. Набор
  раскладывается на задания по тестам: один воркер делает вход (состояние входа общее через хранилище
  секретов), потом тесты расходятся по всем воркерам. Воркер шлёт heartbeat; задания воркера, который
  молчит 90 секунд, завершаются с ошибкой, прогон и набор получают её же.
- **Экземпляры студии.** Сессия Studio и задание конвейера живут в памяти экземпляра, который их
  начал (там браузер Live View и агент). Экземпляр записывает себя владельцем в таблицу `owners`;
  запрос к сессии, пришедший в другой экземпляр, передаётся владельцу (`TESTGEN_INSTANCE_URL`).
  Балансировщику не нужна привязка к экземпляру (sticky sessions).
- **Схема базы** — миграции Alembic в `testgen/migrations`; применяются при старте под блокировкой
  (первый экземпляр мигрирует, остальные ждут) или вручную: `python -m testgen.db upgrade`
  (`TESTGEN_DB_MIGRATE=off` отключает автоматический запуск).

## Переменные окружения

| Переменная | Что задаёт |
|---|---|
| `TESTGEN_DATABASE_URL` | обязательна: `postgresql+psycopg://testgen:***@db:5432/testgen` |
| `TESTGEN_DB_POOL` | размер пула соединений (10) |
| `TESTGEN_SECRET_KEY` | ключ шифрования секретов; обязателен, кроме базы на этом же компьютере (тогда `secret.key`) или `TESTGEN_VAULT_ADDR`. Храните копию: без него секреты не прочитать |
| `TESTGEN_CACHE_DIR` | папка локального кэша (скриншоты, trace для браузера) |
| `TESTGEN_S3_BUCKET`, `TESTGEN_S3_ENDPOINT`, `TESTGEN_S3_ACCESS_KEY`, `TESTGEN_S3_SECRET_KEY`, `TESTGEN_S3_REGION`, `TESTGEN_S3_PREFIX` | объектное хранилище для бинарных файлов; для Yandex Object Storage — `https://storage.yandexcloud.net`, регион `ru-central1` |
| `TESTGEN_INSTANCE_URL` | адрес этого экземпляра для других экземпляров, например `http://10.1.2.3:8765` (в Helm — IP пода) |
| `TESTGEN_PUBLIC_URL` | адрес студии для людей, например `https://testgen.company.ru`: ссылки на экраны (набор, прогон, тест) в уведомлениях и дефектах; без неё уведомления приходят без ссылок |
| `TESTGEN_EMBEDDED_WORKER` | `on` (по умолчанию) — экземпляр студии тоже берёт задания из очереди; `off` — только воркеры |
| `TESTGEN_QUEUE` | `off` — прогоны в процессе студии, без очереди |
| `TESTGEN_WORKER_CONCURRENCY` | сколько тестов воркер гоняет одновременно (2): один браузер, контекст на тест |
| `TESTGEN_METRICS_PORT` | порт метрик воркера (`/metrics`) |
| `TESTGEN_METRICS_TOKEN` | если задан, `/metrics` студии требует `Authorization: Bearer <токен>` |
| `FORWARDED_ALLOW_IPS` | `*` за балансировщиком: студия видит `https` (secure-cookie, ссылки OIDC) |

## Kubernetes (Helm)

Чарт `helm/ai-testgen`: Deployment веб-экземпляров (2) и воркеров (3), Service, Ingress, ConfigMap и
Secret, опционально HPA воркеров и ServiceMonitor для Prometheus Operator. PostgreSQL и S3 — ваши.

```bash
helm install testgen ./helm/ai-testgen --namespace qa --create-namespace \
  --set image.repository=registry.company.ru/qa/ai-testgen --set image.tag=0.5.0 \
  --set secrets.databaseUrl='postgresql+psycopg://testgen:***@postgres.db:5432/testgen' \
  --set secrets.secretKey="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  --set s3.endpoint=https://storage.yandexcloud.net --set s3.bucket=qa-testgen \
  --set secrets.s3AccessKey=*** --set secrets.s3SecretKey=*** \
  --set ingress.host=testgen.company.ru
```

Секреты лучше передавать готовым Secret (`existingSecret`) из Vault или Sealed Secrets. Пробы:
`/api/health` — живость, `/api/ready` — база, S3 и очередь. Воркер при остановке не берёт новых
заданий и доделывает взятые (grace period 10 минут).

Поды работают не от root (uid 10001, `podSecurityContext`/`securityContext` в `values.yaml`): без
привилегий и capabilities, seccomp `RuntimeDefault` — это проходит Pod Security `restricted`. Chromium
запускается без своей песочницы (как Playwright по умолчанию) и дополнительных прав не требует. Пишут поды
только в свои `emptyDir` (`/cache`, `/tmp`, домашняя папка, `/dev/shm`), поэтому корневая файловая
система может быть только для чтения (`securityContext.readOnlyRootFilesystem: true`).

### Platform V DropApp

DropApp — дистрибутив Kubernetes, чарт ставится в него как есть, с файлом `values-dropapp.yaml`
(корневая ФС только для чтения, режим без интернета, таймауты ingress для долгих потоков):

```bash
helm install testgen ./helm/ai-testgen --namespace qa \
  -f ./helm/ai-testgen/values-dropapp.yaml -f my-values.yaml
```

В `my-values.yaml` — ваше:

- **Образ** собирается снаружи (`docker build` скачивает браузеры и пакеты) и кладётся во внутренний
  реестр: `image.repository`, `image.tag`, `imagePullSecrets`.
- **Секреты** — готовый Secret `existingSecret` с ключами `TESTGEN_DATABASE_URL`, `TESTGEN_SECRET_KEY`,
  `TESTGEN_S3_ACCESS_KEY`, `TESTGEN_S3_SECRET_KEY` (и ключами входа через каталог, если он есть).
- **PostgreSQL** 14+ или Platform V Pangolin. Расширение `pg_trgm` ускоряет поиск по модели приложения:
  если роли `testgen` нельзя его создать, создайте его заранее (`CREATE EXTENSION pg_trgm`) — без него
  студия работает, только без индекса.
- **S3** — любое S3-совместимое хранилище: `s3.endpoint`, `s3.bucket`.
- **Ingress** — класс контроллера кластера (`ingress.className`) и адрес (`ingress.host`). Аннотации в
  `values-dropapp.yaml` написаны для nginx: у другого контроллера задайте то же самое его средствами —
  тело запроса до 30 МБ, ожидание ответа 10 минут, без буферизации ответа (ревью требований, конвейер и
  Studio присылают ход работы потоком). За шлюзом Istio (Platform V Synapse) — `ingress.enabled: false` и
  свой VirtualService на Service `testgen-ai-testgen`, порт 80, с тем же таймаутом.
- **Квоты namespace**: веб-экземпляр — до 2 CPU и 4 ГБ, воркер с браузерами — до 3 CPU и 6 ГБ памяти плюс
  `/dev/shm` в памяти (`shmSize`, 1 ГБ).

Подключения Playwright MCP и Zephyr Scale запускаются через `npx` из кэша пакетов в образе, а `npx`
пишет в этот кэш. При корневой ФС только для чтения проверьте их кнопкой «Проверить» в «Проект →
Подключения»; если проверка не проходит, оставьте `readOnlyRootFilesystem: false` — встроенный браузер
студии от этого не зависит.

Если политика кластера требует свой uid (диапазон uid namespace, как в OpenShift), задайте
`podSecurityContext.runAsUser` из этого диапазона: образ не зависит от uid, нужна только запись в тома.

## Docker Compose

`docker-compose.yml` — один экземпляр студии, PostgreSQL и локальная модель (контур без интернета).
`docker-compose.team.yml` — то же без Kubernetes для команды: два экземпляра студии за nginx, три
воркера, PostgreSQL и MinIO. Инструкция — в начале каждого файла. Воркеров добавляет `--scale worker=6`.

## Перенос данных из папок

Прежние версии студии хранили данные в папках `data/` и `secrets/` рядом с кодом. Перенос в базу:

```bash
python -m testgen.db import-files ./data ./secrets     # с TESTGEN_DATABASE_URL (и TESTGEN_S3_*, TESTGEN_SECRET_KEY)
```

Команда копирует файлы как есть (проекты, тесты, историю, скриншоты, пользователей), сразу шифрует
секреты, а журнал действий (`data/audit/*.jsonl`) переносит в таблицу `audit_log` вместе с цепочкой
хэшей — только в пустой журнал, иначе цепочка двух установок порвалась бы. Несколько папок (например,
из разных копий кода) переносятся по очереди; при совпадении путей побеждает последняя. Резервная
копия — `pg_dump`; выгрузка в папки для просмотра — `python -m testgen.db export-files <папка>`.

## Мониторинг

`/metrics` студии и `:TESTGEN_METRICS_PORT/metrics` воркеров (Prometheus):

- `testgen_http_requests_total{method,route,status}`, `testgen_http_request_duration_seconds{route}` —
  запросы к API (маршруты шаблонами, без идентификаторов);
- `testgen_runs_total{status,trigger}` — завершённые прогоны; `testgen_llm_requests_total{stage,provider,model}`;
- `testgen_queue_items{status}`, `testgen_queue_oldest_seconds` — очередь; растущее ожидание — сигнал
  добавить воркеров; `testgen_workers`, `testgen_worker_slots{state}` — живые воркеры и их слоты;
- `testgen_studio_sessions` — открытые сессии Studio экземпляра.

## Проверка

Тесты студии работают на PostgreSQL: каждому потоку pytest создаётся своя база на сервере
`TESTGEN_TEST_DB` (по умолчанию `postgresql+psycopg://testgen:testgen@127.0.0.1:5432/testgen`, роли
нужно право `CREATEDB`), после прогона базы удаляются.

```bash
python -m pytest -q -n 4                          # PostgreSQL
TESTGEN_TEST_S3=moto python -m pytest -q -n 4     # + бинарные файлы в S3 (moto)
```

`tests/test_cluster.py` поднимает отдельными процессами два экземпляра студии и три воркера на одной
базе и проверяет, что набор расходится по воркерам, а сессия Studio доступна через любой экземпляр.

## Ограничения

- MCP-сервер студии (`/mcp`) не передаёт `get_generation` другому экземпляру: генерацию, начатую через
  MCP, опрашивайте через тот же адрес или включите привязку клиента на балансировщике.
- Прогон из CI (`python -m testgen.run`) идёт в процессе CI-агента, а не в воркерах: агент сам
  становится исполнителем, результаты попадают в общую историю. Агенту нужен доступ к базе студии.
