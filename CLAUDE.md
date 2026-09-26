# CLAUDE.md

Руководство для Claude Code по работе с этим репозиторием.

## Что это

AI Test Generator — открытая реализация подхода из статьи TestGrid «AI Test Case Generation»
(их продукт — CoTester). Пользователь описывает сценарий обычным языком, агент на Claude проходит
его в настоящем браузере (Playwright) и записывает исполняемый тест-кейс. Веб-студия: FastAPI +
одностраничный фронтенд `static/index.html`.

Соответствие функциям CoTester (держи его в голове, когда добавляешь фичи):

| CoTester | Здесь |
|---|---|
| Проекты | Стартовая вкладка «Проекты» и мастер нового проекта: приложение → подключения к ресурсам (с проверкой) → привязка к этапам процесса. У проекта свои тесты, подключения, скиллы, процесс |
| Start Generating Test Case: URL, проект, название | Форма «Новый тест» |
| Описание сценария агенту | Поле «Сценарий» и чат с агентом |
| Auto-Pilot Mode | Переключатель Auto-Pilot |
| «Continue» после каждого шага | Карточка шага: Continue или «Отклонить» с подсказкой |
| Редактирование и добавление шагов | Редактируемые описания, ▲▼✕, «+ шаг» |
| Element Picker / Record & Play | Клик по Live View в режиме Picker/Record |
| Live View | Скриншот страницы в реальном времени (или реальное окно браузера) |
| Сохранение и повторный запуск | Вкладка «Тесты»: прогон в новом браузере |
| AgentRx (self-healing) | Несколько локаторов на шаг; если все сломались, Claude находит элемент заново |
| Требования/user stories → сценарии, Gherkin | Вкладка «Требования»; число сценариев не ограничено |
| Экспорт в Selenium/Playwright и Gherkin | `.py` (pytest-playwright) и `.feature` |
| Интеграции (Jira, test management) | Проект → Подключения (MCP): Jira/Confluence, Zephyr Scale, Playwright MCP, любой MCP |
| Сквозной процесс генерации | Вкладка «Конвейер»; этапы настраиваются в «Проект → Процесс генерации» и «Скиллы» |

## Команды (Windows, PowerShell)

venv держим по короткому пути: драйвер Playwright не запускается, если путь длиннее 260 символов.
Не создавай venv внутри проекта.

```powershell
python -m venv $env:LOCALAPPDATA\aitestgen\venv
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m pip install -r requirements.txt
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m playwright install chromium
$env:ANTHROPIC_API_KEY = "sk-ant-..."
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python server.py   # http://127.0.0.1:8765
```

Для подключений Zephyr Scale (`mcp-zephyr-scale`) и Playwright MCP (`@playwright/mcp`) нужен Node.js:
они запускаются через `npx` при первом использовании. Для локального запуска из Claude Code есть
`.claude/launch.json` (конфигурация `studio`, `TESTGEN_AUTH=off`, свободный порт).

Управление пользователями студии:

```powershell
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.auth adduser ivan   # создать или сменить пароль
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.auth deluser ivan
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.auth list
```

Экспортированный тест: `pip install pytest pytest-playwright`, затем `pytest test_xxx.py`
(логин/пароль берутся из `TESTGEN_USERNAME` / `TESTGEN_PASSWORD`).

Автотестов у самого проекта нет — проверяй изменения запуском студии (для локальной отладки удобно
`TESTGEN_AUTH=off`).

## Переменные окружения

- `ANTHROPIC_API_KEY` — обязательна.
- `TESTGEN_MODEL` (по умолчанию `claude-opus-5`), `TESTGEN_EFFORT` (`medium`; для сложных сайтов `high`).
- `TESTGEN_AUTH=off` — без входа в студию; `TESTGEN_SIGNUP=off` — без саморегистрации.
- `TESTGEN_ADMINS` — администраторы студии через запятую (по умолчанию `admin`): только они задают
  команды, аргументы и переменные окружения MCP-подключений, создают подключения «Другой MCP-сервер»
  и удаляют проекты. При `TESTGEN_AUTH=off` администратор — любой.
- `TESTGEN_ATLASSIAN_MCP` — своя команда запуска MCP-сервера Atlassian
  (по умолчанию `mcp-atlassian` из venv, иначе `uvx mcp-atlassian`).
- `TESTGEN_USERNAME` / `TESTGEN_PASSWORD` — учётные данные по умолчанию для прогонов.
- `PORT` (по умолчанию 8765).

## Архитектура

- `server.py` — FastAPI: REST API (`/api/projects` с подресурсами `connections`, `skills`, `jobs`,
  `credentials`; `/api/mcp/presets`, `/api/sessions`, `/api/tests`, `/api/runs`, `/api/jobs`,
  `/api/scenarios`, `/api/requirements/fetch`, `/api/auth/*`) и отдача `static/index.html`.
  Middleware `require_login` пускает без входа только пути из `PUBLIC`; `require_admin` — для
  действий, которые запускают код на сервере.
- **Один event loop для браузера.** Объекты Playwright привязаны к циклу, в котором созданы, поэтому
  вся работа с браузером идёт на отдельном фоновом цикле `WORKER`. Из обработчиков используй
  `submit(coro)` (fire-and-forget) или `await call(coro)`; не трогай Playwright напрямую из цикла uvicorn.
  MCP-сессии и задачи конвейера тоже живут на `WORKER`.
- `testgen/projects.py` — проекты: `data/projects/<id>/project.json` (название, базовый URL,
  подключения без секретов, настройки процесса `pipeline`). `DEFAULT_PIPELINE` — этапы процесса и их
  параметры; `normalize_pipeline()` сливает сохранённое с умолчаниями и отбрасывает лишнее — новые
  параметры этапов добавляй туда. `ensure_default()` переносит тесты из старой раскладки
  `data/<проект>/`; пустой проект не создаётся — без проектов студия открывает мастер нового проекта.
- `testgen/steps.py` — шаг теста (`new_step`) — общая единица для агента, рекордера и прогона;
  `perform()` выполняет шаг. Новые действия добавляй в `ELEMENT_ACTIONS`/`ALL_ACTIONS` и в `perform`,
  затем в инструменты агента и экспортеры.
- `testgen/browser.py` — `BrowserSession`: снимок страницы, каждому видимому интерактивному элементу
  присваивается ref (`e12`) и список устойчивых локаторов (`data-testid`/`data-test`, `#id`,
  role+name, label, placeholder, text, CSS-путь). `expand()` подставляет `{{username}}`/`{{password}}`.
  Интерфейс движка для агента: `describe()`, `screenshot_b64()`, `execute(step)`, `url`, `close()`.
- `testgen/mcp_browser.py` — `McpBrowser`: тот же интерфейс поверх Playwright MCP (`@playwright/mcp`).
  Агент и шаги не меняются; снимок — ARIA-снапшот MCP. Локаторы шага: код, сгенерированный MCP
  (`--codegen python`), плюс `ELEMENT_INFO_JS` через `browser_evaluate` (те же поля, что у встроенного
  снимка). По умолчанию берёт Chromium из `playwright install` (`--executable-path`). Picker/Record
  только во встроенном движке. Сохранённые тесты всегда прогоняются встроенным раннером.
- `testgen/mcp_hub.py` — MCP-подключения проекта: пресеты `PRESETS` (atlassian, zephyr, playwright,
  custom) с полями и маппингом в env, секреты в `secrets/projects/<id>/conn-<cid>.json`.
  `public_view()` отдаёт `missing` (незаполненные обязательные поля) и `check` — результат последней
  проверки, который сервер сохраняет в `project.json` и сбрасывает при изменении подключения.
  `connect()` — короткая сессия в одной задаче; `McpClient` — долгая сессия в своей задаче (anyio не
  даёт выйти из cancel scope в чужой задаче); `Toolbox` — инструменты подключений как инструменты
  Claude (`mode="read"` для агента, `"write"` для публикации; инструменты удаления не даются никогда,
  см. `access()`).
- `testgen/skills.py` — скиллы в формате SKILL.md (`name`/`description`/`stage` + Markdown): встроенные
  в `testgen/skills/`, проектные (и переопределения встроенных) в `data/projects/<id>/skills/`.
  `prompt()` добавляет выбранные скиллы этапа в конец системного промпта — после правил, которые они
  не могут отменить.
- `testgen/agent.py` — `StudioSession`, цикл агента: Claude получает скриншот и список элементов и
  вызывает по одному инструменту за ход (`click`, `fill`, `assert_visible`, …, `finish`); каждый вызов
  становится шагом. Старые скриншоты вычищаются через context editing (`clear_tool_uses_20250919`).
  Настройки этапа `authoring` проекта: движок, скиллы, модель, лимит шагов, read-only инструменты
  подключений (их вызовы выполняются сразу и не становятся шагами).
- `testgen/runner.py` — прогон с самолечением: если все локаторы шага сломались, `heal()` просит Claude
  выбрать элемент по описанию шага; новый локатор сохраняется в тест. `analyze()` классифицирует
  падение (дефект продукта / проблема теста / окружение) по шагу, ошибке и скриншоту.
- `testgen/pipeline.py` — конвейер `Job`: требования → сценарии (ручной или автоматический отбор) →
  по каждому сценарию генерация (`StudioSession` в Auto-Pilot, видна в Studio) → сохранение → прогон
  (`run_and_record`, он же для кнопки «Запустить») → публикация. Состояние в
  `data/projects/<id>/jobs/<job>.json`.
- `testgen/publisher.py` — публикация в Zephyr Scale через MCP: Claude получает инструменты
  подключения (кроме удаления) и скиллы публикации и заканчивает инструментом `done`; ключ кейса
  хранится в `test["external"]["zephyr"]`, повторная публикация обновляет тот же кейс.
- `testgen/scenarios.py` — требования → сценарии (structured output: тип, приоритет, инструкции для
  агента, ожидаемый результат, Gherkin). Лимита на количество нет: сначала компактный план всех
  сценариев, затем детализация параллельными пакетами (`BATCH`, `PARALLEL`) — так число сценариев не
  упирается в `max_tokens` одного ответа. Не возвращай ограничение количеством.
- `testgen/exporters.py` — экспорт в pytest-playwright и Gherkin.
- `testgen/sources.py` — загрузка ТЗ из Jira/Confluence через Atlassian-подключение проекта
  ([mcp-atlassian](https://github.com/sooperset/mcp-atlassian), stdio-процесс на каждый запрос,
  `READ_ONLY_MODE`).
- `testgen/llm.py` — общий клиент Anthropic и `common_params(stage)` (модель, effort,
  `fallbacks: "default"`; `stage` — настройки этапа проекта, переопределяют модель и effort).
  Все запросы к Claude должны использовать `common_params()`.
- `testgen/auth.py` — вход в студию; `testgen/vault.py` — хранилище секретов; `testgen/storage.py` —
  тесты в `data/projects/<id>/tests/<test>.json` и выбор логина для прогона (свой у теста → проекта →
  `TESTGEN_*`).

## Секреты и авторизация

- Пользователи студии: `secrets/users.json` (PBKDF2-хэши), сессия — подписанная HttpOnly cookie на
  7 дней. При первом запуске создаётся `admin`, пароль печатается в консоль.
- Учётные данные тестируемого приложения (открытым текстом): проекта — `secrets/projects/<id>/app.json`,
  свои у теста — `secrets/projects/<id>/test-<test>.json`.
- Токены MCP-подключений: `secrets/projects/<id>/conn-<cid>.json`; в `project.json` и API только
  признак «задан» (`secrets_set`).
- Команда, аргументы и env MCP-сервера — это запуск кода на сервере: менять их может только
  администратор (`auth.is_admin`); обычный пользователь заполняет лишь поля, объявленные пресетом.
- `secrets/` в `.gitignore` — никогда не коммить и не выводи его содержимое.

## Инварианты — не ломать

- **Пароль не попадает к Claude и в артефакты.** Агент видит только плейсхолдеры `{{username}}` и
  `{{password}}` (плюс сам логин); реальное значение подставляется в момент выполнения шага
  (`BrowserSession.expand`). В шагах, экспорте и истории чата пароля быть не должно
  (`StudioSession._mask`). Экспорт читает значения из `os.environ["TESTGEN_*"]`.
- **Никаких необратимых действий.** Системный промпт агента запрещает реальную оплату, заказы,
  отправку сообщений и удаление данных: агент доходит до этой точки, ставит проверку и завершает сценарий.
  Инструменты MCP с удалением (`mcp_hub.access() == "destructive"`) не передаются Claude ни на одном этапе;
  Atlassian работает только на чтение. Скиллы добавляются после правил и не могут их отменить.
- В режиме Playwright MCP пароль подставляется прямо перед вызовом инструмента и маскируется во всём,
  что вернул сервер (`McpBrowser._mask`).
- Запросы идут с `fallbacks: "default"`: если классификатор безопасности отклонит запрос, API повторит
  его на резервной модели.
- Интерфейс и сообщения пользователю — на русском.
