---
name: allure-import
description: Чтение ручных тест-кейсов Allure TestOps для автоматизации: сценарий, предусловия, ожидаемые результаты.
stage: publish
---

# Импорт ручных кейсов из Allure TestOps

- Для каждого ID прочитай кейс и его сценарий (шаги). Если шаги вложенные — разверни их по порядку.
- action — что делает пользователь; expected — ожидаемый результат шага, если он есть (в Allure он часто
  вложенным шагом или полем expected result).
- preconditions — предусловия кейса одной строкой.
- priority: critical/blocker/high → high, normal → medium, minor/trivial/low → low.
- Ничего не меняй в Allure TestOps. Верни кейсы инструментом cases.
