# Установка на команду: PostgreSQL, S3, воркеры

По умолчанию студия хранит всё в папках `data/` и `secrets/` и работает одним процессом — так удобно
одному человеку и в CI. Для команды на 20–50 человек данные переезжают в общую базу, а прогоны — в
воркеры. Код тот же: режим выбирается переменными окружения.

| | Один процесс (по умолчанию) | Команда |
|---|---|---|
| Проекты, тесты, задачи, прогоны, пользователи | JSON-файлы в `data/`, `secrets/` | PostgreSQL (`TESTGEN_DATABASE_URL`) |
| Скриншоты, trace, эталоны, файлы для загрузки | `data/projects/<id>/…` | S3: MinIO, Yandex Object Storage (`TESTGEN_S3_*`); без S3 — в той же базе |
| Секреты (логины приложения, токены подключений) | `secrets/`, шифрование по `TESTGEN_SECRET_KEY` | В базе, только зашифрованные; или HashiCorp Vault |
| Прогоны, наборы, мутации, Planner | В процессе студии | Очередь в базе, воркеры `python -m testgen.worker` |
| Сессии Studio (Live View), конвейер | В процессе студии | В экземпляре, который их начал; остальные экземпляры передают запросы ему |
| Журнал действий | `data/audit/*.jsonl` | Таблица `audit_log` |

## Как это устроено

- **Хранилище** (`testgen/fs.py`). Модули по-прежнему обращаются к данным по путям
  `data/projects/<id>/tests/<test>.json`; с `TESTGEN_DATABASE_URL` путь становится ключом строки в
  таблице `docs` (JSON-документ целиком, колонки `project_id` и `updated` для отчётов). Бинарные файлы
  с `TESTGEN_S3_BUCKET` уходят в бакет под тем же ключом. Браузеру файлы нужны на диске, поэтому
  локальная папка остаётся кэшем: скриншоты шагов пишутся локально и отправляются в хранилище по ходу
  прогона, а при чтении из другого экземпляра скачиваются.
- **Блокировки.** Изменения одного документа (тест, индекс прогонов, пользователи, журнал расходов)
  сериализуются между процессами advisory lock PostgreSQL.
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
| `TESTGEN_DATABASE_URL` | `postgresql+psycopg://testgen:***@db:5432/testgen` (поддерживается только PostgreSQL) |
| `TESTGEN_DB_POOL` | размер пула соединений (10) |
| `TESTGEN_S3_BUCKET`, `TESTGEN_S3_ENDPOINT`, `TESTGEN_S3_ACCESS_KEY`, `TESTGEN_S3_SECRET_KEY`, `TESTGEN_S3_REGION`, `TESTGEN_S3_PREFIX` | объектное хранилище; для Yandex Object Storage — `https://storage.yandexcloud.net`, регион `ru-central1` |
| `TESTGEN_SECRET_KEY` | ключ шифрования секретов; в общей базе обязателен (или `TESTGEN_VAULT_ADDR`). Храните копию: без него секреты не прочитать |
| `TESTGEN_INSTANCE_URL` | адрес этого экземпляра для других экземпляров, например `http://10.1.2.3:8765` (в Helm — IP пода) |
| `TESTGEN_EMBEDDED_WORKER` | `on` (по умолчанию) — экземпляр студии тоже берёт задания из очереди; `off` — только воркеры |
| `TESTGEN_QUEUE` | `off` — прогоны в процессе студии даже с общей базой |
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

## Docker Compose

`docker-compose.team.yml` — то же без Kubernetes: два экземпляра студии за nginx, три воркера,
PostgreSQL и MinIO. Инструкция — в начале файла. Воркеров добавляет `--scale worker=6`.

## Перенос существующей установки

```bash
python -m testgen.db import-files ./data ./secrets     # с TESTGEN_DATABASE_URL, TESTGEN_S3_*, TESTGEN_SECRET_KEY
```

Команда копирует файлы как есть (проекты, тесты, историю, скриншоты, пользователей) и сразу шифрует
секреты ключом `TESTGEN_SECRET_KEY` (без ключа отказывается). Обратно в папки (резервная копия, уход с
общей базы): `python -m testgen.db export-files <папка>`.

## Мониторинг

`/metrics` студии и `:TESTGEN_METRICS_PORT/metrics` воркеров (Prometheus):

- `testgen_http_requests_total{method,route,status}`, `testgen_http_request_duration_seconds{route}` —
  запросы к API (маршруты шаблонами, без идентификаторов);
- `testgen_runs_total{status,trigger}` — завершённые прогоны; `testgen_llm_requests_total{stage,provider,model}`;
- `testgen_queue_items{status}`, `testgen_queue_oldest_seconds` — очередь; растущее ожидание — сигнал
  добавить воркеров; `testgen_workers`, `testgen_worker_slots{state}` — живые воркеры и их слоты;
- `testgen_studio_sessions` — открытые сессии Studio экземпляра.

## Проверка

Тесты студии проходят в обоих режимах:

```bash
python -m pytest -q -n 4                                             # файлы
TESTGEN_TEST_DB=postgresql+psycopg://testgen:testgen@127.0.0.1:5432/testgen   TESTGEN_TEST_S3=moto python -m pytest -q -n 4                      # PostgreSQL + S3 (moto)
```

`TESTGEN_TEST_DB` — адрес сервера PostgreSQL: каждому потоку pytest создаётся своя база, тестам
`tests/test_scale.py` — своя на каждый тест (так работает job `shared-storage` в CI). Без него тесты
общего хранилища пропускаются. `tests/test_cluster.py` поднимает отдельными процессами два
экземпляра студии и три воркера на одной базе и проверяет, что набор расходится по воркерам, а
сессия Studio доступна через любой экземпляр.

## Ограничения

- MCP-сервер студии (`/mcp`) не передаёт `get_generation` другому экземпляру: генерацию, начатую через
  MCP, опрашивайте через тот же адрес или включите привязку клиента на балансировщике.
- Прогон из CI (`python -m testgen.run`) с общей базой идёт в процессе CI-агента, а не в воркерах:
  агент сам становится исполнителем, результаты попадают в общую историю.
