# Студия как MCP-сервер: работа из IDE

Агенты в IDE (Claude Code, GitHub Copilot, Cursor) могут генерировать и запускать тесты студии, читать
падения и забирать код тестов и trace, не открывая студию.

## Инструменты

| Инструмент | Что делает |
|---|---|
| `list_projects`, `list_tests` | Проекты и их тесты (теги, последний прогон, карантин, оценка мутациями) |
| `generate_test` | Агент студии проходит сценарий в браузере (Auto-Pilot) и сохраняет тест, если проверил результат. Ждёт до `wait_seconds`, дальше — `get_generation` |
| `get_generation` | Статус генерации |
| `run_test`, `get_run` | Прогон теста с самолечением, анализом падения и trace |
| `run_suite`, `get_suite` | Прогон набора (все тесты или по тегам), как в CI |
| `list_failures` | Упавшие и нестабильные тесты с вердиктом Claude |
| `export_test` | Код теста: `playwright` (pytest), `gherkin`, `api` (httpx по записанному трафику) |
| `get_trace` | Путь к Playwright trace прогона и команда `npx playwright show-trace` |

Удаляющих инструментов нет. Генерация идёт по тем же правилам, что и в студии: агент не выполняет
необратимых действий, пароль приложения не попадает к Claude.

## Токен

Токен выдаётся в студии: «Проект → Доступ из IDE (MCP) → Создать токен» (показывается один раз), или
из консоли:

```powershell
& $env:LOCALAPPDATA\aitestgen\venv\Scripts\python -m testgen.auth token ivan "Claude Code"
```

Токен действует с правами своего пользователя; отозвать его можно там же. При `TESTGEN_AUTH=off`
токен не нужен.

## Подключение к запущенной студии (HTTP)

Адрес: `http://127.0.0.1:8765/mcp` (порт — `PORT`). Генерации, запущенные так, видны в Studio вживую.

Claude Code:

```bash
claude mcp add --transport http testgen http://127.0.0.1:8765/mcp --header "Authorization: Bearer tg_…"
```

GitHub Copilot (VS Code), `.vscode/mcp.json`:

```json
{
  "servers": {
    "testgen": {"type": "http", "url": "http://127.0.0.1:8765/mcp",
                "headers": {"Authorization": "Bearer tg_…"}}
  }
}
```

Cursor, `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "testgen": {"url": "http://127.0.0.1:8765/mcp", "headers": {"Authorization": "Bearer tg_…"}}
  }
}
```

## Без запущенной студии (stdio)

Процесс сам запускает браузер и работает с той же папкой данных:

```json
{
  "mcpServers": {
    "testgen": {
      "command": "C:\\Users\\me\\AppData\\Local\\aitestgen\\venv\\Scripts\\python.exe",
      "args": ["-m", "testgen.mcp_server"],
      "cwd": "C:\\path\\to\\ai-testgen",
      "env": {"TESTGEN_TOKEN": "tg_…", "ANTHROPIC_API_KEY": "sk-ant-…"}
    }
  }
}
```

`TESTGEN_STUDIO_URL` (необязательно) — адрес студии для ссылок на trace в ответах.

## Пример

> Сгенерируй в проекте Shop тест «войти и добавить рюкзак в корзину», запусти его и покажи код.

Агент IDE вызовет `generate_test`, затем `run_test` и `export_test`.
