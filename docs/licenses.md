# Лицензии зависимостей

Проверено 18 октября 2026: все пакеты `requirements.txt` вместе с транзитивными (установлены в чистое
окружение, `pip-licenses`) и npm-пакеты, которые ставит `Dockerfile`. Лицензия студии — Apache-2.0
(`LICENSE`); несовместимых с ней зависимостей нет.

## Python

| Лицензия | Пакеты | Как используется |
|---|---|---|
| MIT, BSD-2/3, ISC, Apache-2.0, PSF-2.0, Unlicense, MIT-0 | почти все: anthropic, playwright, fastapi, uvicorn, starlette, pydantic, mcp, httpx, SQLAlchemy, alembic, boto3, cryptography, PyJWT, Faker, pypdf, prometheus-client и их зависимости | импорт; разрешительные лицензии |
| LGPL-3.0 | `psycopg`, `psycopg-binary`, `ldap3` | импорт без изменений; LGPL это допускает при любой лицензии студии, пакеты ставятся из PyPI и заменяемы |
| MPL-2.0 | `certifi`, `pathspec`, часть `orjson` | без изменений; MPL действует пофайлово |
| MIT | `mcp-atlassian` (в метаданных пакета лицензия не указана, файл `LICENSE` в пакете — MIT) | отдельный процесс (stdio MCP-сервер, `testgen/sources.py`) |
| GPL-2.0-or-later | `Unidecode` — зависимость `mcp-atlassian` | только внутри процесса `mcp-atlassian`; студия его не импортирует. Если когда-нибудь импортировать `mcp_atlassian` или `unidecode` из кода студии, лицензию придётся пересмотреть |

Зависимости для разработки (`requirements-dev.txt`: pytest, pytest-xdist, pytest-playwright, moto) в
поставку не входят.

## Docker-образ и внешние программы

| Компонент | Лицензия | Как используется |
|---|---|---|
| `@playwright/mcp` | Apache-2.0 | отдельный процесс (`TESTGEN_PLAYWRIGHT_MCP`) |
| `mcp-zephyr-scale` | MIT | отдельный процесс (`TESTGEN_ZEPHYR_MCP`) |
| axe-core | MPL-2.0 | файл `axe.min.js` без изменений внедряется в страницу теста (`TESTGEN_AXE_JS`/`TESTGEN_AXE_URL`); в репозитории его нет |
| Браузеры Playwright (Chromium, Firefox, WebKit) | BSD / MPL-2.0 / LGPL | отдельные программы |
| Базовый образ `mcr.microsoft.com/playwright/python` | Apache-2.0 и лицензии пакетов Ubuntu | основа образа |
| PostgreSQL, vLLM (`docker-compose.yml`) | PostgreSQL License, Apache-2.0 | отдельные сервисы |
| Веса модели (Qwen2.5-VL и другие) | своя у каждой модели, см. её карточку | скачивает установщик, в образ не входят |

## Как перепроверить

```bash
python -m venv /tmp/lic && /tmp/lic/bin/pip install -r requirements.txt pip-licenses
/tmp/lic/bin/pip-licenses --order=license
```

Новую зависимость под GPL/AGPL в код студии (импортом) не добавляйте без пересмотра лицензии; как
отдельный процесс (MCP-сервер) она допустима.
