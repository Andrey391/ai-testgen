# AI Test Generator

Студия генерации автотестов с помощью ИИ. Вы описываете сценарий обычным языком, агент проходит его в
настоящем браузере (Playwright) и записывает исполняемый тест: шаги, устойчивые локаторы, проверки.
Тест можно править, прогонять в CI и выгрузить в pytest-playwright или Gherkin.

*English summary: an AI test-authoring studio. You describe a scenario in plain language, an agent walks
through it in a real Playwright browser and records an executable test. Self-hosted, works with any model
behind an OpenAI-compatible API (including a local one in an air-gapped network). The UI is in Russian.*

## Чем отличается от Playwright Test Agents

Генерировать тесты уже бесплатно умеют Playwright Test Agents и связка ИИ-ассистента в IDE и Playwright MCP.
Студия делает упор на тесты, которым можно доверять, и на работу без кода:

- **Проверка проверок.** Мутационное тестирование показывает, ловит ли тест поломку; слабые проверки
  усиливает агент.
- **Самолечение только через ревью.** Если все локаторы шага сломались, ИИ находит элемент заново, но
  новый локатор попадает в тест только после «Принять»: ИИ не «вылечит» тест под баг.
- **Классификация падений.** Дефект продукта, проблема теста, окружение или нестабильность — по шагу,
  скриншоту, событиям браузера и истории прогонов.
- **Требования до тестов.** Ревьюер проверяет ТЗ по стандарту (ГОСТ 34.602, ГОСТ 19.201,
  ISO/IEC/IEEE 29148, user story) и сверяет с моделью приложения; сценарии проектируются только по
  разобранным требованиям.
- **Своя установка.** PostgreSQL, S3, воркеры, SSO (OIDC/LDAP), роли в проектах, журнал действий,
  шифрование секретов; закрытый контур с локальной моделью.
- **Для ручного тестировщика.** Studio с Live View, подтверждением каждого шага, Element Picker и
  Record & Play; код писать не нужно.

## Возможности

- **Studio**: сценарий и чат с агентом, Auto-Pilot или подтверждение шагов, правка и добавление шагов,
  Live View страницы, несколько открытых сессий.
- **Тесты**: история прогонов, Playwright trace, события браузера, нестабильные тесты и карантин,
  наборы по тегам, покрытие экранов, повторное использование шагов модулями.
- **UI- и API-тесты**: API-тест пишется и прогоняется без браузера по каталогу эндпоинтов из трафика.
- **Конвейер**: требования → ревью → сценарии → тесты → прогон → мутации → публикация.
- **Модель приложения**: сущности с жизненным циклом, роли и учётные записи, тестовые данные; строится
  сама из требований, обхода сайта (Planner) и записи тестов.
- **Экспорт**: pytest-playwright (`.py`, локаторы с `.or_()`), Gherkin (`.feature`), проект целиком `.zip`.
- **Интеграции** через MCP: Jira/Confluence, Zephyr Scale, Playwright MCP, любой MCP-сервер.
- **Работа из IDE**: студия сама является MCP-сервером (`/mcp`), см. [docs/mcp.md](docs/mcp.md).
- **CI**: `python -m testgen.run` с отчётами JUnit и Allure, шаблоны в [ci/](ci/).

## Быстрый старт

Нужны Python 3.12, PostgreSQL 17 и модель с OpenAI-совместимым API: облако на ваш выбор или свой сервер (vLLM, Ollama).

```bash
python -m venv ~/.venvs/aitestgen
~/.venvs/aitestgen/bin/python -m pip install -r requirements.txt
~/.venvs/aitestgen/bin/python -m playwright install chromium

# база: роль и база testgen
psql -h 127.0.0.1 -U postgres -c "CREATE ROLE testgen LOGIN CREATEDB PASSWORD 'testgen'" \
     -c "CREATE DATABASE testgen OWNER testgen"

export TESTGEN_DATABASE_URL="postgresql+psycopg://testgen:testgen@127.0.0.1:5432/testgen"
export TESTGEN_LLM_BASE_URL="http://127.0.0.1:8000/v1"   # необязательно: адрес и ключ модели задаются в «Проект → Модель»
~/.venvs/aitestgen/bin/python server.py   # http://127.0.0.1:8765
```

На Windows держите venv по короткому пути (`$env:LOCALAPPDATA\aitestgen\venv`) и не внутри проекта:
драйвер Playwright не запускается при пути длиннее 260 символов. Команды PowerShell — в
[CLAUDE.md](CLAUDE.md#команды-windows-powershell).

Проще — `.\studio.cmd` (или двойной щелчок по нему): создаст venv, поставит зависимости и
Chromium (заново — когда меняется `requirements.txt`), проверит PostgreSQL и откроет студию в браузере.
`.\scripts\studio.ps1` напрямую работает, только если политика PowerShell разрешает скрипты (по умолчанию —
`Restricted`); `studio.cmd` запускает его с `-ExecutionPolicy Bypass`.
Параметры: `-NoAuth` (без входа), `-Port 8800`, `-NoBrowser`, `-Setup` (переустановить зависимости).
База — `TESTGEN_DATABASE_URL`, без неё — локальная `testgen:testgen@127.0.0.1:5432/testgen`.

При первом запуске создаётся пользователь `admin`, пароль печатается в консоль. Мастер нового проекта
спросит адрес приложения, модель ИИ и подключения. Модель в коде не задана: каждый проект выбирает
модель, effort, ключ и адрес API в «Проект → Модель».

Данные хранятся только в PostgreSQL; на диске остаётся лишь кэш файлов браузера. Секреты в базе всегда
зашифрованы: для базы на этом компьютере ключ создаётся сам (`%LOCALAPPDATA%\aitestgen\secret.key`, на Linux —
`~/.cache/aitestgen/secret.key`; сохраните его копию), для другой базы задайте `TESTGEN_SECRET_KEY`.

### Docker

```bash
docker build -t ai-testgen .
docker compose up -d            # студия + PostgreSQL + локальная модель (vLLM)
docker compose logs studio      # пароль admin
```

Образ содержит браузеры, axe-core и MCP-серверы пресетов, поэтому работает без интернета.

## Установка

| Документ | О чём |
|---|---|
| [docs/deploy.md](docs/deploy.md) | Установка на команду: PostgreSQL, S3, воркеры, несколько экземпляров, Helm-чарт `helm/ai-testgen`, `docker-compose.team.yml` |
| [docs/offline.md](docs/offline.md) | Закрытый контур: локальная модель (vLLM, Qwen2.5-VL), требования к GPU |
| [docs/security.md](docs/security.md) | Для службы безопасности: какие данные уходят в модель, где хранятся секреты |
| [docs/test-stand.md](docs/test-stand.md) | CAPTCHA на тестовом стенде: как настроить стенд для автотестов |
| [docs/mcp.md](docs/mcp.md) | Студия как MCP-сервер для Copilot, Cursor и других клиентов MCP |
| [docs/economics.md](docs/economics.md) | Юнит-экономика: сколько стоит тест |
| [docs/glossary.md](docs/glossary.md) | Термины студии |
| [docs/licenses.md](docs/licenses.md) | Лицензии зависимостей |

Пользователи: `python -m testgen.auth adduser|deluser|list`, API-токен для MCP —
`python -m testgen.auth token <имя> "<название>"`. Вход через каталог — переменные `TESTGEN_OIDC_*` и
`TESTGEN_LDAP_*`. Полный список переменных окружения — в [CLAUDE.md](CLAUDE.md#переменные-окружения).

## Прогон в CI

```bash
python -m testgen.run --project Shop --tag smoke --junit report.xml
```

Код выхода 0 — всё прошло (нестабильные и упавшие в карантине не считаются), 1 — есть падения,
2 — ошибка аргументов. Шаблоны для GitHub Actions, GitLab CI, Jenkins и TeamCity — в [ci/](ci/).

Экспортированный тест запускается без студии: `pip install pytest pytest-playwright faker`, затем
`pytest test_xxx.py`. Логин и пароль — `TESTGEN_USERNAME` / `TESTGEN_PASSWORD`, адрес стенда —
`TESTGEN_BASE_URL`.

## Безопасность

- Пароль тестируемого приложения не попадает к модели и в артефакты: агент видит только
  `{{password}}`, значение подставляется в браузер в момент шага; в trace и трафике оно маскируется.
- Агент не выполняет необратимых действий (оплата, заказы, отправка сообщений, удаление данных):
  доходит до этой точки, ставит проверку и завершает сценарий. Инструменты MCP с удалением моделям
  не передаются.
- Чужой проект не виден: без доступа любой ресурс проекта отвечает 404 и в API, и в MCP-сервере.

Подробности — [docs/security.md](docs/security.md).

## Разработка

Устройство кода и инварианты описаны в [CLAUDE.md](CLAUDE.md). Тесты студии работают на локальном
стенде `tests/site/` с заглушкой модели, без ключей API:

```bash
pip install -r requirements-dev.txt
pytest -q -m "not browser"   # быстрая часть
pytest -q -n 4               # всё, с браузерными тестами
```

Тестам нужен PostgreSQL (`TESTGEN_TEST_DB`, по умолчанию `testgen:testgen@127.0.0.1:5432`): на каждый
поток создаётся своя база. Дорожная карта — [docs/plan.md](docs/plan.md).

## Лицензия

[Apache License 2.0](LICENSE). Лицензии зависимостей — [docs/licenses.md](docs/licenses.md).
