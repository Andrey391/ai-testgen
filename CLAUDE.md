# CLAUDE.md

Руководство для Claude Code по работе с этим репозиторием.

## Что это

AI Test Generator — студия генерации автотестов с помощью ИИ. Пользователь описывает сценарий
обычным языком, агент на Claude проходит его в настоящем браузере (Playwright) и записывает
исполняемый тест-кейс. Веб-студия: FastAPI + одностраничный фронтенд `static/index.html`.

Карта возможностей (держи её в голове, когда добавляешь фичи):

| Возможность | Где в студии |
|---|---|
| Проекты | Стартовая вкладка «Проекты» и мастер нового проекта: приложение → модель ИИ → подключения к ресурсам (с проверкой) → привязка к этапам процесса. У проекта свои тесты, модель, подключения, скиллы, процесс |
| Задачи команды | Вкладка «Задачи»: статус, приоритет, исполнитель, срок, тесты задачи; «Тест в Studio» — тест привязывается к задаче при сохранении |
| Создание теста: URL, проект, название | Форма «Новый тест» |
| Описание сценария агенту | Поле «Сценарий» и чат с агентом |
| Auto-Pilot | Переключатель Auto-Pilot |
| Подтверждение каждого шага | Карточка шага: Continue или «Отклонить» с подсказкой |
| Редактирование и добавление шагов | Studio: редактируемые описания, ▲▼✕, «+ шаг»; сохранённый тест: «Тесты → Подробнее → Шаги → Редактировать» (действие, значение, локатор, порядок; новая версия) или «Править в Studio» (тест воспроизводится, агент ждёт человека) |
| Element Picker / Record & Play | Клик по Live View в режиме Picker/Record |
| Live View | Скриншот страницы в реальном времени (или реальное окно браузера), вписан в экран Studio с масштабом; окно браузера 1920×1080, при запуске — «Экран» во вкладке «Тесты» (`1366x768`, устройство) |
| Сохранение и повторный запуск | Вкладка «Тесты»: прогон в новом браузере, история прогонов, trace, события браузера; «Повторить» упавший прогон из истории (тот же браузер и устройство), «Повторить упавшие» тесты набора |
| Самолечение локаторов | Несколько локаторов на шаг; если все сломались, Claude находит элемент заново — новый локатор идёт на ревью человеку |
| Требования/user stories → сценарии, Gherkin | Вкладка «Требования»: живая лента генерации, история анализов (раскрывающийся список: требования → сценарии с правкой → тесты по каждому), «Сгенерировать все»; число сценариев не ограничено; без ТЗ — «Исследовать сайт» (Planner). На каждую генерацию (и запуск конвейера) выбираются слои UI / API, виды проверок и техники тест-дизайна; умолчания — «Проект → Сценарии» |
| Проверка ТЗ | «Проверить ТЗ» во вкладке «Требования» (и автоматически перед генерацией): разделы по стандарту (ГОСТ 34.602, ГОСТ 19.201, ISO/IEC/IEEE 29148, user story), качество требований, правила команды, вопросы авторам; стандарт — «Проект → Требования» |
| Модель приложения, тестовые данные, память | «Проект → Тестовые данные»: сущности с зависимостями и жизненным циклом, роли с возможностями и ограничениями → учётные записи, данные стенда и «нужные» сценариям, память (агент — инструмент `remember`); строится сама: из требований, из карты сайта (Planner), из сценариев (роль и `test_data` сценария) и при записи тестов (агент — инструмент `test_data`), следующие тесты переиспользуют данные. **Тесты генерируются только по подтверждённому жизненному циклу** («Подтвердить», `requirements.confirm_model`): конвейер ждёт (`awaiting_model`), «Тест в Studio» из сценария — 409; скиллы группы «Тестовые данные» |
| UI- и API-тесты | Слой сценария `ui`/`api`; агент пишет API-шаги инструментом `api_request` (статус, поля ответа), экспорт — в тот же `.py` |
| Экспорт в Selenium/Playwright и Gherkin | `.py` (pytest-playwright, локаторы с `.or_()`), `.feature`, проект целиком `.zip`, API-тесты по трафику |
| Интеграции (Jira, test management) | Проект → Подключения (MCP): Jira/Confluence, Zephyr Scale, Playwright MCP, любой MCP |
| Сквозной процесс генерации | Вкладка «Конвейер»; этапы настраиваются в «Проект → Процесс генерации» и «Скиллы». «Перезапустить сбойные» — заново все сценарии с ошибкой агента; пункт «нужен человек» становится готовым, когда тест его сессии сохранён в Studio |
| Переиспользование сценариев | Новые сценарии сверяются с тестами и сценариями проекта (`reuse.py`): похожий получает `match`, человек решает — создать новый, переиспользовать или доработать прежний тест (строка сценария во вкладке «Требования» или запуск конвейера `awaiting_reuse`) |
| Регрессия, CI | «Запустить набор» по тегам, `python -m testgen.run` (JUnit/Allure), нестабильные тесты и карантин, шаблоны `ci/` |
| Качество тестов | Мутационное тестирование проверок; слабые проверки усиливает агент |
| Скиллы | «Проект → Скиллы»: список с поиском и фильтрами по группам, этапу и состоянию; переключатель вкл/выкл; группа и этапы, на которых работает скилл; встроенные только для чтения — «Клонировать в проект» и флажок «Использовать локальную версию». В настройках этапов скиллы раскрываются на месте |
| Работа из IDE | Студия — MCP-сервер (`/mcp`, `testgen.mcp_server`), см. `docs/mcp.md` |
| Команда и корпоративные требования | «Проект → Участники и доступ»: роли в проектах, группы каталога (OIDC/LDAP), журнал действий; шифрование секретов; PostgreSQL + S3 + воркеры (`docs/deploy.md`) |

## Команды (Windows, PowerShell)

venv держим по короткому пути: драйвер Playwright не запускается, если путь длиннее 260 символов.
Не создавай venv внутри проекта.

```powershell
python -m venv $env:LOCALAPPDATA\aitestgen\venv
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m pip install -r requirements.txt
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m playwright install chromium
$env:TESTGEN_DATABASE_URL = "postgresql+psycopg://testgen:testgen@127.0.0.1:5432/testgen"   # обязательно
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # необязательно: ключ можно задать в «Проект → Модель»
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python server.py   # http://127.0.0.1:8765
```

Данные студии хранятся только в PostgreSQL (`docs/deploy.md`): без `TESTGEN_DATABASE_URL` студия, воркер
и `testgen.run` не запускаются. Локально — служба PostgreSQL 17 на `127.0.0.1:5432`, роль и база
`testgen` (пароль `testgen`, у роли `CREATEDB` — для тестов). Секреты в базе всегда зашифрованы: без
`TESTGEN_SECRET_KEY` при базе на этом компьютере ключ один раз создаётся в
`%LOCALAPPDATA%\aitestgen\secret.key` (потеряешь файл — потеряешь секреты), для другой базы ключ обязателен.
На диске только кэш файлов браузера (`TESTGEN_CACHE_DIR`, по умолчанию `%LOCALAPPDATA%\aitestgen\cache`),
поэтому все worktree и копии кода видят одни и те же данные.

Модель студия не выбирает сама: каждый проект задаёт модель, effort, API-ключ, адрес API и цены в
«Проект → Модель» (запросы идут по Anthropic Messages API — в облако или на свой сервер/прокси с
этим API). Контур без интернета — `docker-compose.yml` и `docs/offline.md`.

Команды запуска MCP-серверов Zephyr Scale и Playwright MCP задаются в `TESTGEN_ZEPHYR_MCP` и
`TESTGEN_PLAYWRIGHT_MCP` (или администратором в настройках подключения): студия не скачивает пакеты
сама. Для локального запуска из Claude Code есть
`.claude/launch.json` (конфигурация `studio`, `TESTGEN_AUTH=off`, локальная база, свободный порт).

Управление пользователями студии:

```powershell
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.auth adduser ivan   # создать или сменить пароль
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.auth deluser ivan
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.auth list
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.auth token ivan "Claude Code"   # API-токен для MCP
```

Установка на команду (`docs/deploy.md`): та же база PostgreSQL, S3 для больших файлов, прогоны в
воркерах, несколько экземпляров студии; Helm-чарт `helm/ai-testgen`, `docker-compose.team.yml`.

```powershell
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.worker            # воркер
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.db upgrade        # схема базы (обычно сама при старте)
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.db import-files .\data .\secrets   # перенос из папок прежних версий
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.db export-files .\backup          # выгрузка базы в папки
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.audit verify      # цепочка журнала действий
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.vault rotate      # перешифровать секреты новым ключом
```

Прогон тестов проекта без студии (CI): код выхода 0 — всё прошло (нестабильные и упавшие в карантине
не считаются), 1 — падения, 2 — ошибка аргументов. Шаблоны для GitHub Actions и GitLab — `ci/`.

```powershell
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.run --project Shop --tag smoke --junit report.xml
```

Экспортированный тест: `pip install pytest pytest-playwright faker`, затем `pytest test_xxx.py`
(логин/пароль — `TESTGEN_USERNAME` / `TESTGEN_PASSWORD`, адрес стенда — `TESTGEN_BASE_URL`).

Тесты самой студии — pytest на локальном стенде `tests/site/` (`tests/stand.py`, вариант `v2` —
изменённая вёрстка) с заглушкой LLM (`tests/fakes.py`: `FakeClient` вместо клиента API — фикстура `fake_llm`;
фикстура `project` задаёт проекту модель `test-model`), без ключей API и затрат. Данные тестов — в
PostgreSQL: `tests/conftest.py` создаёт базу на каждый поток xdist на сервере `TESTGEN_TEST_DB` (по
умолчанию локальный `testgen:testgen@127.0.0.1:5432`) и удаляет её в конце; `conftest.new_database()` —
пустая база для отдельного теста. Кэш — во временной папке (`TESTGEN_CACHE_DIR`).
CI — `.github/workflows/tests.yml`. Бинарные файлы в S3: `TESTGEN_TEST_S3=moto`;
`tests/test_cluster.py` поднимает 2 экземпляра студии и 3 воркера процессами.

**Тесты прогоняй только перед созданием PR** — не после каждой правки и не в процессе работы.
Всегда гоняй только быстрые тесты (`pytest -q -m "not browser"`); PR открывай, только когда они зелёные.
Полный прогон (`pytest -q -n 4`) — только по прямой команде «прогнать все тесты».

```powershell
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m pip install -r requirements-dev.txt
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m pytest -q -n 4
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m pytest -q -m "not browser"   # быстрая часть; тесты со стендом помечаются browser автоматически
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m pytest -q tests/test_units.py -k имя_теста   # один тест
$env:TESTGEN_TEST_S3 = "moto"; & $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m pytest -q -n 4
```

Ручная проверка — запуском студии (для локальной отладки удобно `TESTGEN_AUTH=off`); стенд для
экспериментов поднимает конфигурация `stand` из `.claude/launch.json`.

## Переменные окружения

- Модель в коде не задана: модель, effort, API-ключ, адрес API и цены выбираются в «Проект → Модель»
  (шаг 2 мастера нового проекта); этап процесса может переопределить модель и effort.
- `TESTGEN_DATABASE_URL` — обязательный адрес PostgreSQL (`postgresql+psycopg://…`): единственное
  хранилище данных; `TESTGEN_DB_MIGRATE`, `TESTGEN_DB_POOL`.
- `ANTHROPIC_API_KEY` — ключ для проектов без своего ключа (удобно для CI).
- `TESTGEN_USD_RUB` — курс для пересчёта расходов в рубли (по умолчанию 80).
- `TESTGEN_OFFLINE=on` — без обращений в интернет: без CDN и `npx` из сети, модель только по адресу API
  проекта (во внутренней сети).
- `TESTGEN_AUTH=off` — без входа в студию; `TESTGEN_SIGNUP=off` — без саморегистрации.
- `TESTGEN_ADMINS` — администраторы студии через запятую (по умолчанию `admin`; ещё — группы каталога
  из «Участники и доступ»): владельцы всех проектов; только они задают команды, аргументы и переменные
  окружения MCP-подключений, создают подключения «Другой MCP-сервер», меняют адрес API модели и группы
  и удаляют проекты. При `TESTGEN_AUTH=off` администратор — любой.
- Вход через каталог (`testgen/sso.py`): `TESTGEN_OIDC_ISSUER`, `TESTGEN_OIDC_CLIENT_ID`,
  `TESTGEN_OIDC_CLIENT_SECRET` (+ `_SCOPES`, `_USERNAME_CLAIM`, `_GROUPS_CLAIM`, `_TITLE`, `_REDIRECT_URL`,
  `_CACERT`); `TESTGEN_LDAP_URL`, `TESTGEN_LDAP_BASE_DN`, `TESTGEN_LDAP_BIND_DN`, `TESTGEN_LDAP_BIND_PASSWORD`
  (+ `_USER_FILTER`, `_GROUP_ATTR`, `_START_TLS`, `_CACERT`). С ними `TESTGEN_SIGNUP` и `TESTGEN_LOCAL_LOGIN`
  по умолчанию `off`; `TESTGEN_SSO_SESSION_HOURS` (12).
- `TESTGEN_SECRET_KEY` — шифрование секретов (AES-256-GCM; `TESTGEN_SECRET_KEY_OLD` для `vault rotate`);
  обязателен, кроме базы на этом компьютере (ключ `%LOCALAPPDATA%\aitestgen\secret.key`, `vault.secret_key()`);
  `TESTGEN_VAULT_ADDR`, `TESTGEN_VAULT_TOKEN`, `TESTGEN_VAULT_MOUNT`, `TESTGEN_VAULT_PREFIX` — HashiCorp Vault.
- `TESTGEN_AUDIT_SYSLOG=host:port[/tcp]`, `TESTGEN_AUDIT_FILE` — копия журнала действий в SIEM.
- Команда (`docs/deploy.md`): `TESTGEN_S3_BUCKET` / `_ENDPOINT` / `_ACCESS_KEY` /
  `_SECRET_KEY` / `_REGION` / `_PREFIX`, `TESTGEN_INSTANCE_URL` (адрес экземпляра для других экземпляров),
  `TESTGEN_EMBEDDED_WORKER` (`on`), `TESTGEN_QUEUE` (`on`), `TESTGEN_WORKER_CONCURRENCY` (2),
  `TESTGEN_METRICS_PORT` (воркер), `TESTGEN_METRICS_TOKEN` (`/metrics`).
- `TESTGEN_ATLASSIAN_MCP`, `TESTGEN_ZEPHYR_MCP`, `TESTGEN_PLAYWRIGHT_MCP` — команды запуска
  MCP-серверов пресетов (Atlassian без неё ищется в venv из `requirements.txt`). Не задана и не
  задана в подключении — подключение сообщает, что команды нет; студия ничего не скачивает сама.
- `TESTGEN_USERNAME` / `TESTGEN_PASSWORD` — учётные данные по умолчанию для прогонов.
- `TESTGEN_CACHE_DIR` — папка локального кэша файлов браузера (по умолчанию `%LOCALAPPDATA%\aitestgen\cache`).
- `TESTGEN_PROMPT_CACHE=off` — без кэширования промптов (сравнить стоимость; расход виден в Studio,
  прогонах и конвейере).
- `TESTGEN_AXE_JS` — путь к `axe.min.js` или `TESTGEN_AXE_URL` — адрес, откуда его скачать (кэш в
  `<TESTGEN_CACHE_DIR>/data/cache/`). Без них `assert_accessible` падает с подсказкой; адресов CDN в коде нет.
- `TESTGEN_FAKER_LOCALE` — локаль тестовых данных `{{faker.*}}` (по умолчанию `en_US`).
- `TESTGEN_TOKEN`, `TESTGEN_STUDIO_URL` — для `python -m testgen.mcp_server` (stdio).
- `PORT` (по умолчанию 8765).

## Архитектура

- `server.py` — FastAPI: REST API (`/api/projects` с подресурсами `access`, `audit`, `connections`, `skills`, `jobs`, `tasks`, `sessions` (прерванные),
  `credentials`, `runs` (прогон набора), `suites`, `export` (.zip), `tags`, `explore`, `coverage`, `knowledge`;
  `/api/mcp/presets`, `/api/sessions`, `/api/tests` (+ `meta`, `runs`, `proposals`, `verify`,
  `strengthen`, `traffic`, `mock`, `baselines`), `/api/runs` (+ `files`, `baseline`), `/api/suites`,
  `/api/jobs`, `/api/tasks`, `/api/scenarios`, `/api/requirements/fetch` и `validate`, `/api/auth/*` (+ `tokens`, `oidc`),
  `/api/users`, `/api/sso`, `/api/audit`, `/api/ready`, `/metrics`), MCP-сервер на `/mcp` (смонтирован
  последним) и отдача `static/index.html`. Middleware `require_login` пускает без входа только пути из
  `PUBLIC`, `/mcp` — по API-токену; передаёт запрос экземпляру-владельцу сессии Studio (`_elsewhere`),
  считает метрики и пишет журнал действий (`_audit`, `AUDIT_ACTIONS`). **Доступ к проекту** проверяет
  одна зависимость `guard` для всех маршрутов: ресурс по префиксу пути (`RESOURCES`) → проект → роль
  (`ROLE_RULES`; по умолчанию GET — наблюдатель, остальное — редактор); без доступа 404. Новый маршрут
  проектного ресурса клади под существующий префикс или добавь префикс в `RESOURCES`; проект из тела
  запроса проверяй `project(pid, need)`. `require_admin` — для действий, которые запускают код на сервере.
- `testgen/access.py` — роли в проекте (viewer/editor/owner), видимость (`members`/`open`), группы каталога.
  `testgen/sso.py` — OIDC (code + PKCE, проверка ID-токена) и LDAP. `testgen/audit.py` — журнал действий
  (цепочка хэшей, SIEM). `testgen/monitoring.py` — метрики Prometheus.
- **Хранилище — только PostgreSQL** (`testgen/fs.py`, `testgen/db.py`, `testgen/repo/`). Все данные под
  `DATA`/`SECRETS` читай и пиши через `fs` (`read_json`, `write_json`, `glob`, `documents`, `rmtree`, `lock`
  для read-modify-write, `local_path`/`push` для файлов браузера), не через `Path` напрямую: путь — ключ
  строки таблицы `docs`, бинарные файлы — в той же строке или в S3. `DATA`/`SECRETS` на диске
  (`paths.CACHE`) — только кэш для браузера. Тесты, задачи, прогоны и расход модели — свои таблицы с
  колонками для фильтров: `repo/<сущность>.py`, класс `Sql` (read-modify-write строки — `repo.held`,
  `SELECT … FOR UPDATE`). Новую сущность с фильтрами клади в `repo/` и миграцию, остальное — документом
  через `fs`. При старте `db.check()` проверяет базу и ключ секретов. Схема — миграции Alembic в
  `testgen/migrations`. Старые папки `data/`/`secrets/` переносит `db import-files`.
- **Очередь и воркеры** (`testgen/workqueue.py`, `testgen/worker.py`). «Запустить», наборы,
  мутации и Planner — задания очереди (`worker.start_*`); набор раскладывается на задания по
  тестам (`suite.distribute`, `item_done`). Сессии Studio и конвейер остаются в экземпляре-владельце
  (таблица `owners`).
- **Один event loop для браузера.** Объекты Playwright привязаны к циклу, в котором созданы, поэтому
  вся работа с браузером идёт на отдельном фоновом цикле `WORKER`. Из обработчиков используй
  `submit(coro)` (fire-and-forget) или `await call(coro)`; не трогай Playwright напрямую из цикла uvicorn.
  MCP-сессии, задачи конвейера, прогоны, наборы, мутации и Planner тоже живут на `WORKER`.
- `testgen/projects.py` — проекты: `data/projects/<id>/project.json` (название, базовый URL,
  подключения без секретов, настройки процесса `pipeline`). `DEFAULT_PIPELINE` — этапы процесса и их
  параметры; `normalize_pipeline()` сливает сохранённое с умолчаниями и отбрасывает лишнее — новые
  параметры этапов добавляй туда. `ensure_default()` переносит тесты из старой раскладки
  `data/<проект>/` (после `db import-files`); пустой проект не создаётся — без проектов студия открывает мастер нового проекта.
- `testgen/tasks.py` — задачи проекта: таблица `tasks` (`repo/tasks.py`; статус `todo`/`in_progress`/
  `review`/`done`, приоритет, исполнитель, срок, `test_ids`). Сессия Studio, начатая из задачи
  (`task_id`), при сохранении привязывает тест (`link_test`, `todo` → `in_progress`); удаление теста
  отвязывает его (`storage.delete` → `unlink_test`).
- `testgen/catalog.py` — каталоги тест-дизайна (без зависимостей): виды проверок `TYPES`, техники `TECHNIQUES`,
  слои `LAYERS`, стандарты документации `STANDARDS` и критерии качества требований `QUALITY`. Новый вид
  проверки или технику добавляй сюда — их получают модели, настройки и интерфейс (`CATALOG` в `_project_view`).
  Описания для моделей — на английском в самих словарях, для людей (подсказки, описание стандарта) — на
  русском в `TYPE_INFO`, `TECHNIQUE_INFO`, `STANDARD_INFO`; новый ключ добавляй в оба места.
- `testgen/knowledge.py` — модель приложения проекта (`data/projects/<id>/knowledge.json`): сущности
  (`depends_on`, жизненный цикл, кто создаёт, правила), роли → учётные записи, данные стенда, память
  (`remember`). `prompt()` получают сценарии (предусловия), агент (`_context_note`) и анализ падений.
  Модель строится сама (настройка `requirements.learn_model`): `extract()` — из требований и из карты сайта
  (`source="explore"`, `explorer.learn_model` после обхода: сущности и объекты на страницах), `record()` —
  инструмент агента `test_data` (найденные и созданные тестом данные, зависимости, жизненный цикл).
  `merge()` не перезаписывает написанное людьми; запись данных с `source` (найдена автоматически)
  обновляется новыми находками. `account_for()` — учётная запись роли сценария (`role`) или из предусловий (конвейер).
  У ролей `capabilities`/`restrictions`; `need()` записывает тестовые данные сценариев (`status: "needed"`,
  `needed_by`) — `record()` переводит их в данные стенда. **Подтверждение:** `confirm()` хранит подпись
  (`signature()`: сущности, зависимости, жизненный цикл, роли и возможности); изменение подписанного —
  `confirmation()["state"] == "changed"`, нужно подтвердить заново; `save()` подтверждение не меняет.
  Конвейер перед генерацией тестов ждёт `is_confirmed()` (`Job._await_model`, настройка
  `requirements.confirm_model`; в тестах студии выключена фикстурой `lifecycle_confirmed`). Скиллы модели —
  слот `requirements.model_skills` (`application-model`), в сценариях — `test-data-roles`.
- `testgen/validation.py` — проверка ТЗ на соответствие стандарту (`requirements.standard`, `checklist`,
  скиллы этапа `requirements`): разделы, замечания, вопросы, оценка; хранится в анализе (`validation`).
- `testgen/steps.py` — шаг теста (`new_step`) — общая единица для агента, рекордера и прогона;
  `perform()` выполняет шаг. Новые действия добавляй в `ELEMENT_ACTIONS`/`ALL_ACTIONS` и в `perform`,
  затем в инструменты агента (`agent.TOOLS`, `tool_to_step`), `McpBrowser.execute`, экспортеры и
  варианты Element Picker / «+ шаг» в `index.html`. Проверки: `assert_visible`, `assert_text_present`,
  `assert_url_contains`, `assert_value`, `assert_checked`, `assert_enabled`, `assert_count` (группа
  однотипных элементов, `browser.group_candidates`), `assert_element_text`, `assert_no_console_errors`,
  `assert_accessible`, `assert_screenshot` (последние три — `AUXILIARY_ASSERTIONS`, не проверка
  результата); `mock_route` — подмена ответа запроса; `api_request` — запрос к API приложения (блоки
  «до/после» и шаги API-теста: `expect_status`, `expect` — поля ответа, `save`; DELETE только «после»).
  Сохранённые шаги правит человек: `storage.edit_steps` (`PUT /api/tests/{tid}/steps`; локаторы строками
  `storage.parse_locator`, секрет в значении → плейсхолдер, неизменённое поле шага сохраняется).
- `testgen/checks.py` — `assert_accessible` (axe-core из `TESTGEN_AXE_JS` или `TESTGEN_AXE_URL`,
  порог `run.a11y_impact`) и `assert_screenshot` (эталон при первом прогоне в
  `data/projects/<id>/baselines/`, сравнение на canvas в отдельной странице, маски `step["masks"]`,
  допуск `run.visual_threshold`). Провал — `CheckFailed` с `details` для отчёта.
- `testgen/browser.py` — `BrowserSession`: снимок страницы, каждому видимому интерактивному элементу
  (и до 40 элементам содержимого: строки списков, заголовки, сообщения, `data-testid`) присваивается ref
  (`e12`) и список устойчивых локаторов (`data-testid`/`data-test`, `#id`, role+name, label, placeholder,
  text, CSS-путь). `expand()` подставляет `{{username}}`/`{{password}}` и тестовые данные.
  Окно — `VIEWPORT` 1920×1080; устройство вида `1366x768` (`screen_size`) задаёт размер окна прогона
  (`run.devices`, `device` запуска теста и набора, `testgen.run --screen`, `authoring.device`).
  Сессия собирает `events` (ошибки консоли, исключения страницы, 4xx/5xx, упавшие запросы) и при
  `record_traffic` — `traffic` (XHR/fetch). `launch(browser=...)` открывает контекст в общем браузере
  (наборы). `find(locator, wait=...)` ждёт появления элемента, прежде чем прогон уйдёт в самолечение.
  Интерфейс движка для агента: `describe()`, `screenshot_b64()`, `execute(step)`, `url`, `close()`.
- `testgen/testdata.py` — плейсхолдеры `{{unique}}`, `{{today}}`, `{{faker.email}}`, … : новое значение
  на каждый прогон, одно и то же внутри прогона (`DataValues`); экспорт встраивает тот же генератор.
- `testgen/mcp_browser.py` — `McpBrowser`: тот же интерфейс поверх Playwright MCP (`TESTGEN_PLAYWRIGHT_MCP`).
  Агент и шаги не меняются; снимок — ARIA-снапшот MCP. Локаторы шага: код, сгенерированный MCP
  (`--codegen python`), плюс `ELEMENT_INFO_JS` через `browser_evaluate` (те же поля, что у встроенного
  снимка). Берёт Chromium из `playwright install` (`--executable-path`) при любой команде stdio, если в
  аргументах нет своего браузера, и `--no-sandbox`, как встроенный движок (без `--sandbox` в аргументах). Инструкция подключения — `setup` пресета (видит администратор). Picker/Record
  только во встроенном движке. Сохранённые тесты всегда прогоняются встроенным раннером.
- `testgen/mcp_hub.py` — MCP-подключения проекта: пресеты `PRESETS` (atlassian, zephyr, playwright,
  custom) с полями и маппингом в env, секреты в `secrets/projects/<id>/conn-<cid>.json`.
  `public_view()` отдаёт `missing` (незаполненные обязательные поля) и `check` — результат последней
  проверки, который сервер сохраняет в `project.json` и сбрасывает при изменении подключения.
  `connect()` — короткая сессия в одной задаче; `McpClient` — долгая сессия в своей задаче (anyio не
  даёт выйти из cancel scope в чужой задаче); `Toolbox` — инструменты подключений как инструменты
  Claude (`mode="read"` для агента, `"write"` для публикации; инструменты удаления не даются никогда,
  см. `access()`).
- `testgen/skills.py` — скиллы в формате SKILL.md (`name`/`description`/`stage`/`group` + Markdown): встроенные
  в `testgen/skills/` только для чтения; проектные и локальные копии встроенных (`clone`) — в
  `data/projects/<id>/skills/`; какую версию встроенного использует проект, решает флажок (`use_local`,
  выключенные копии — `skills-local.json`, `off`). Скилл, выключенный в проекте (`set_enabled`, там же `disabled`), не получает
  ни один этап, хотя остаётся в их списках; `attach()` — на каких этапах (`SLOTS` — списки скиллов в `pipeline`) он используется.
  `prompt()` добавляет выбранные скиллы этапа в конец системного промпта — после правил, которые они
  не могут отменить.
- `testgen/agent.py` — `StudioSession`, цикл агента: модель получает скриншот и список элементов и
  вызывает по одному инструменту за ход (`click`, `fill`, `assert_*`, …, `finish`); каждый вызов
  становится шагом. Старые скриншоты вычищаются (`keep_images`: context editing
  `clear_tool_uses_20250919`, а если модель его не поддерживает — `providers.base.trim_history`). Для
  моделей слабее — настройки этапа: компактный промпт и примеры ходов (`authoring.prompt`, скилл
  `authoring-examples`), текстовый режим (`authoring.screenshots`: скриншот по инструменту `look` или
  никогда). Неверный ответ возвращается модели на исправление (`check_call`, не больше `MAX_REPAIRS` на шаг).
  **Скорость:** в Auto-Pilot (встроенный движок) модель может вернуть несколько действий за ход — поля
  одной формы; они выполняются подряд без снимка между ними, пока действие из `BATCH_SAFE` и страница
  та же (`_execute_pending`), остальные получают «Not executed» (`starts_in_autopilot` — пакет уже с первого
  хода). Снимок сразу после шага не ждёт загрузку повторно (`BrowserSession.just_settled`), скриншот
  делается один раз; `timing` — время модели и браузера (видно в Studio). **Короткие запросы:** разговор,
  выросший больше `FRESH_INPUT_TOKENS` входа или `FRESH_TURNS` ходов, начинается заново на чистом контексте
  (`_start_over`: сценарий, записанные шаги, результат последнего действия текстом, текущая страница; `FRESH_TASK`). Инструмент `api_request` — шаги
  API-теста (ответ показывается агенту), `remember` — факт в память проекта. С `EDIT_TASK`
  («Править в Studio», `POST /api/tests/{tid}/edit`) сессия воспроизводит тест и ждёт человека без запроса к модели.
  Перед каждым ходом агент пишет в чат, что и зачем делает; `finish` содержит `evidence` (какие проверки
  подтверждают результат) — Studio показывает «Сейчас / Цель / Тест будет готов, когда» и итог.
  `stop()` (`/api/sessions/{sid}/stop`) выключает Auto-Pilot и отменяет запрос к модели (ход возвращается
  в очередь, «Продолжить с AI» повторяет его); шаг, уже выполняемый в браузере, доделывается.
  Настройки этапа `authoring` проекта: движок, скиллы, модель и effort, лимит шагов, read-only инструменты
  подключений (их вызовы выполняются сразу и не становятся шагами). `save()` сохраняет тест (сохраняя
  теги, карантин, ключ Zephyr) и трафик; `usage` — расход токенов сессии. С `base_steps` сессия сначала
  воспроизводит сохранённый тест и получает `task` — так агент усиливает слабые проверки.
  **Сессия переживает перезапуск студии:** после каждого шага и реплики `checkpoint()` пишет снимок
  (шаги, чат без пароля, сценарий, `test_id`, `task_id`, `origin`) в `data/projects/<id>/sessions/<sid>.json`,
  свой логин сессии — в хранилище секретов (`session-<sid>`). Разговор с моделью не сохраняется:
  `restored()` продолжает сессию с тем же id через `base_steps` (шаги воспроизводятся, агент получает
  `RESUME_TASK`); если воспроизведение не удалось, шаги остаются в сессии. `save_checkpoint()` сохраняет
  шаги как тест без браузера. Снимок удаляет только `close(discard=True)` (человек закрыл сессию,
  конвейер сохранил тест) или «Удалить» в списке «Прерванные сессии».
- `testgen/runner.py` — одна попытка прогона (`run_test`): trace (`run.trace`, пароль маскируется
  `mask_trace`), события браузера, скриншоты шагов в папку прогона, хуки для мутаций. Самолечение:
  если все локаторы шага сломались, `heal()` просит модель выбрать элемент; в режиме `run.heal_mode =
  review` локатор становится предложением (`report["proposals"]`, в тесте — `heal_proposals`) и
  попадает в тест только после «Принять»; отклонённый (`heal_rejected`) больше не используется.
  `analyze()` классифицирует падение (дефект продукта / проблема теста / окружение / нестабильный)
  по шагу, ошибке, скриншоту, событиям и истории; `judge_visual()` — визуальное расхождение.
- `testgen/runs.py` — история прогонов: таблица `runs` (`repo/runs.py`, сводки — колонки), файлы прогона
  по путям `data/projects/<id>/runs/<test>/<run>/`; ротация `run.keep_runs`; скриншоты — только двух последних прогонов
  теста (`SHOT_RUNS`, `prune_shots`; trace и снимки самолечения остаются); `flip_rate()` — доля смен результата.
- `testgen/pipeline.py` — `run_and_record` (кнопка «Запустить», наборы, конвейер, CLI, MCP): попытка,
  перезапуск упавшего (`run.retry_failed`: упал → прошёл = flaky), анализ, запись в историю и в тест
  (последний прогон, предложения самолечения, авто-карантин). Конвейер `Job`: требования (ссылки,
  текст, карта сайта Planner) → сценарии (ручной или автоматический отбор) → по каждому сценарию
  генерация (`StudioSession` в Auto-Pilot, видна в Studio) → сохранение → прогон → проверка
  мутациями (этап `verify`, при слабых проверках агент их усиливает) → публикация. Состояние в
  `data/projects/<id>/jobs/<job>.json`. Запуск, прерванный перезапуском (или остановленный, упавший),
  продолжает `Job.resumed` (`POST /api/jobs/{jid}/resume`): сценарии и выбор не повторяются, готовые
  пункты (`DONE_ITEMS`) пропускаются, у пункта с тестом — только непройденные этапы, прерванная генерация
  продолжается из снимка сессии. `Job.retried` (`POST /api/jobs/{jid}/retry`) — то же, но сбойные пункты
  (`retryable`: без теста, ошибка/нужен человек/остановлен, не возможный дефект) начинаются с новой сессии.
  Тест, сохранённый из сессии конвейера, закрывает её пункт (`Job.session_saved`, `session_saved` для
  запуска не в памяти, `reconcile` при чтении запуска). Похожие на прежние сценарии (`match`, `reuse.py`)
  ждут решения человека (`_await_reuse`, `POST /api/jobs/{jid}/reuse`): `reuse` — прежний тест становится
  тестом пункта (сценарий без теста — не дублируется), `refine` — агент воспроизводит прежний тест и
  дорабатывает его (`_refine`, `reuse.REFINE_TASK`), `new` — новый тест.
- `testgen/suite.py` — прогон набора (все тесты / по тегам / список) в одном браузере, `run.parallel`
  контекстов; карантин не валит набор. `testgen/reports.py` — JUnit XML и Allure.
  `testgen/run.py` — CLI `python -m testgen.run`.
- `testgen/mutations.py` — мутационное тестирование проверок: мутанты `noop_action`, `assertion`,
  `api_500` применяются хуками раннера; результат в `test["verify"]`; `improvement_task()` — задача агенту.
- `testgen/explorer.py` — Planner: обход сайта по ссылкам (только GET, без форм; `SKIP` — выход,
  удаление, файлы), карта в `data/projects/<id>/explore/`, `to_requirements()` для сценариев,
  `coverage()` — страницы без тестов.
- `testgen/traffic.py` — XHR/fetch сессии автора: маскирование, HAR в `data/projects/<id>/traffic/`,
  `mock_spec()` для шага `mock_route`.
- `testgen/mcp_server.py` — студия как MCP-сервер для IDE (FastMCP): `generate_test`, `run_test`,
  `run_suite`, `list_failures`, `list_tasks`, `export_test`, `get_trace` и др.; HTTP на `/mcp` в студии или stdio.
- `testgen/publisher.py` — публикация в Zephyr Scale через MCP: Claude получает инструменты
  подключения (кроме удаления) и скиллы публикации и заканчивает инструментом `done`; ключ кейса
  хранится в `test["external"]["zephyr"]`, повторная публикация обновляет тот же кейс.
- `testgen/scenarios.py` — требования → сценарии; что проектировать — `settings()` (виды проверок, слои
  `ui`/`api`, техники; выбор запуска поверх «Проект → Сценарии»), сценарий несёт `layer` (`progress` — промежуточные результаты: план, пакеты,
  лог; `/api/scenarios` со `stream: true` отдаёт их NDJSON-потоком для живой ленты во вкладке «Требования») (structured output: тип, приоритет, инструкции для
  агента, ожидаемый результат, Gherkin). Лимита на количество нет: сначала компактный план всех
  сценариев, затем детализация параллельными пакетами; у сценария роль (`role`) и нужные данные (`test_data`,
  записываются в модель `knowledge.need`) (`BATCH`, `PARALLEL`) — так число сценариев не
  упирается в `max_tokens` одного ответа. Не возвращай ограничение количеством. Все запросы короткие и на
  чистом контексте (требования + одна задача): план частями по `PLAN_PAGE` (следующей части — только названия
  запланированного), детализация — только сценарии своего пакета; из оборванного на лимите ответа готовые
  сценарии сохраняются (`_salvage`), остальное запрашивается с места обрыва меньшими частями.
- `testgen/analyses.py` — история анализов требований: документ `data/projects/<id>/analyses/<id>.json`
  (требования, сценарии с `id`, `test_ids` каждого). Генерация (`/api/scenarios`) заполняет его по событиям
  `progress`; сценарии правят `/api/analyses/{aid}/scenarios`; тест привязывается к сценарию при сохранении
  сессии Studio (`analysis_id`/`scenario_id`) и в конвейере (`/jobs` с `analysis_id` — сразу этап генерации).
- `testgen/exporters.py` — экспорт: pytest-playwright (до `MAX_ALTERNATIVES` локаторов шага через
  `.or_()`, фикстуры `app_url`/`credentials`/`testdata`/…), Gherkin, проект целиком (`bundle()`:
  `conftest.py`, `tests/`, `features/`) и API-тесты pytest + httpx по записанному трафику
  (`to_api_tests()`, без DELETE и чужих сайтов).
- `testgen/sources.py` — загрузка ТЗ из Jira/Confluence через Atlassian-подключение проекта
  ([mcp-atlassian](https://github.com/sooperset/mcp-atlassian), stdio-процесс на каждый запрос,
  `READ_ONLY_MODE`).
- `testgen/llm.py` — единственная дверь к модели. Привязки к конкретным моделям в коде нет: настройки
  проекта `project.json` → `llm` (`model`, `effort`, `base_url`, `prices` в $, результат проверки `check` и
  список моделей `models` из Models API с поддерживаемыми effort и недостающими возможностями),
  ключ — `secrets/projects/<id>/llm.json` (`projects.llm_settings`/`update_llm`), без него —
  `ANTHROPIC_API_KEY`. Все запросы идут через `llm.chat(stage, system=..., messages=..., tools=...,
  project_id=...)` или `llm.parse(stage, ..., schema=...)` (ответ в pydantic-модели); `stage` — настройки
  этапа проекта, переопределяют модель и effort (`llm.model(project_id, stage)`). Они проверяют бюджеты
  (`Usage.limit`, месячный лимит проекта `pipeline.budget`, `BudgetExceeded`, предупреждение на 80%) и
  учитывают расход (`track`: `Usage`, `usage_scope()`, журнал проекта — таблица `usage`, строка на запрос, `repo/usage.py`,
  стоимость — по ценам проекта). Без модели — `llm.NotConfigured`; сервер не запускает генерацию без
  модели (`_require_model`). `llm.check()` — проверка подключения. Не обращайся к SDK мимо `llm.py` и не
  добавляй в код идентификаторы моделей, их цены и умолчания.
- `testgen/providers/` — формат запроса (`base.Request`, сообщения в формате Anthropic Messages) и ответа
  (`base.Reply`) и их отправка в Messages API (`anthropic.py`: точка кэша на tools + system, context
  editing, effort, `fallbacks`, structured output). Чего модели не хватает по последней проверке
  (`Model.features`), заменяется в `base.py`: обрезка старых скриншотов, structured output — JSON-схема в
  промпте, проверка pydantic и один повтор.
- `testgen/auth.py` — вход в студию и API-токены (`secrets/tokens.json`, хранится только SHA-256), группы
  каталога и настройки «группа → роль» (`data/sso.json`); `testgen/vault.py` — хранилище секретов; `testgen/storage.py` — тесты в
  таблице `tests` (`repo/tests.py`; теги, карантин, `heal_proposals`, `verify`), `update()` —
  частичное изменение под блокировкой (тест пишут и сервер, и прогоны), выбор логина для прогона
  (свой у теста → проекта → `TESTGEN_*`).

## Секреты и авторизация

- Пользователи студии: `secrets/users.json` (PBKDF2-хэши; у пользователей OIDC/LDAP хэша нет, есть
  `groups` последнего входа), сессия — подписанная HttpOnly cookie на 7 дней (каталог — 12 часов); ключ
  подписи — секрет `studio/session`. При первом запуске создаётся `admin`, пароль печатается в консоль
  (не с SSO).
- Все секреты идут через `testgen/vault.py`: строки базы по путям `secrets/<kind>/<key>.json`, всегда
  зашифрованные (AES-256-GCM, ключ `TESTGEN_SECRET_KEY` или локальный `secret.key`, путь — AAD), или HashiCorp Vault.
- Учётные данные тестируемого приложения: сколько угодно учётных записей проекта в
  `secrets/projects/<id>/app.json` (`projects.save_account`/`account_credentials`; у записи логин, пароль,
  TOTP и параметры авторизации «имя — значение», агент вводит их как `{{auth.<имя>}}`, секретные маскируются
  как пароль — `testdata.secret_pairs`; одна запись — по умолчанию). Тест входит под `test["account"]`,
  иначе под записью по умолчанию; свои у теста — `secrets/projects/<id>/test-<test>.json`. Видят их
  редакторы, в API только логин, несекретные параметры и признаки «задан»; наблюдатель — только названия.
- Токены MCP-подключений: `secrets/projects/<id>/conn-<cid>.json`; в `project.json` и API только
  признак «задан» (`secrets_set`).
- API-ключ модели: `secrets/projects/<id>/llm.json`; в API только `key_set`. Адрес API (`base_url`)
  меняет только администратор: туда уходит ключ.
- Команда, аргументы и env MCP-сервера — это запуск кода на сервере: менять их может только
  администратор (`auth.is_admin`); обычный пользователь заполняет лишь поля, объявленные пресетом.
- `secrets/` (папки прежних версий), `secret.key` и содержимое таблицы `docs` под `secrets/` — никогда не
  коммить и не выводи.

## Инварианты — не ломать

- **Чужой проект не виден.** Любой ресурс проекта (тест, прогон, trace, файл, сессия, задача, запуск
  конвейера) без доступа отвечает 404 — и в API, и в MCP-сервере студии. Проверяется
  `tests/test_access.py` обходом всех маршрутов.
- **Данные только в PostgreSQL.** Никаких файлов данных рядом с кодом и режима «без базы»: всё через
  `fs`/`repo`, на диске — только кэш. Без `TESTGEN_DATABASE_URL` ничего не запускается.
- **Секреты только зашифрованы в базе**, в API — только признак «задан»; наблюдатель не видит
  даже логин приложения. Журнал действий только дописывается, тела запросов в него не попадают.
- **Пароль не попадает к модели и в артефакты.** Агент видит только плейсхолдеры `{{username}}` и
  `{{password}}` (плюс сам логин); реальное значение подставляется в момент выполнения шага
  (`BrowserSession.expand`). В шагах, экспорте и истории чата пароля быть не должно
  (`StudioSession._mask`, в том числе после проверки, взявшей значение со страницы). Экспорт читает
  значения из `os.environ["TESTGEN_*"]`. В trace пароль заменяется на `***` (`runner.mask_trace`), в
  записанном трафике — на `{{password}}`, секретные заголовки и поля — на `***` (`traffic.mask_entry`).
  Проверяется тестами `tests/test_agent_invariants.py`.
- **Самолечение не меняет тест без человека** в режиме `review` (по умолчанию): ИИ не должен «вылечить»
  тест под баг.
- **Никаких необратимых действий.** Системный промпт агента запрещает реальную оплату, заказы,
  отправку сообщений и удаление данных: агент доходит до этой точки, ставит проверку и завершает сценарий.
  Инструменты MCP с удалением (`mcp_hub.access() == "destructive"`) не передаются модели ни на одном этапе;
  Atlassian работает только на чтение. Скиллы добавляются после правил и не могут их отменить.
  Planner ходит только по ссылкам (GET) и пропускает выход/удаление; мутации меняют только DOM и
  ответы запросов в браузере теста; MCP-сервер студии не даёт удалять данные.
- В режиме Playwright MCP пароль подставляется прямо перед вызовом инструмента и маскируется во всём,
  что вернул сервер (`McpBrowser._mask`).
- Запросы идут через `llm.chat`/`llm.parse` с `fallbacks: "default"`: если классификатор безопасности
  отклонит запрос, API повторит его на резервной модели. Инструменты с удалением и правила системного
  промпта одинаковы для любой модели проекта.
- Интерфейс и сообщения пользователю — на русском.
