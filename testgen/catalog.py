"""Catalogs of test design shared by the scenarios, the project settings and the studio's UI:
kinds of checks (by the principles of testing), test design techniques, test layers and the
standards a specification is validated against (validation.py).
"""
from __future__ import annotations

# Kinds of checks, by the principles of testing: key -> (label, what such a scenario checks).
TYPES = {
    "positive": ("позитивный", "the main flow with valid data reaches the expected result"),
    "negative": ("негативный", "invalid input or a forbidden action is rejected with a clear message and no change of data"),
    "boundary": ("граничные значения", "values at the limits: minimum, maximum, just outside them, empty"),
    "edge": ("крайний случай", "unusual but valid situations: empty lists, very long values, special characters, repeats"),
    "validation": ("валидация ввода", "required fields, formats, lengths and allowed values of every input"),
    "error_handling": ("обработка ошибок", "how the system reports failures: server errors, timeouts, missing data"),
    "state_transition": ("жизненный цикл", "allowed and forbidden transitions between states of an object"),
    "roles": ("роли и права", "what each role may and may not see or do; access without the right role is refused"),
    "security": ("безопасность", "authentication, authorization bypass, session handling, injection in inputs (XSS, SQL), "
                                 "exposure of data of other users"),
    "accessibility": ("доступность", "keyboard use, labels and names of controls, contrast, WCAG rules"),
    "usability": ("удобство", "the user understands what happened: messages, hints, focus, confirmations"),
    "data_integrity": ("целостность данных", "created and changed data is saved, shown the same everywhere, not duplicated"),
    "integration": ("интеграция", "the flow across modules or systems: the result of one is the input of another"),
    "compatibility": ("совместимость", "the same flow on other browsers, screen sizes and devices"),
    "localization": ("локализация", "texts, dates, numbers and currencies of the locale"),
    "performance": ("быстродействие", "the action completes within the expected time; lists with many items work"),
}
DEFAULT_TYPES = ["positive", "negative", "boundary", "edge", "validation", "error_handling", "state_transition",
                 "roles", "security", "accessibility"]
# Test design techniques: key -> (label, how to apply it).
TECHNIQUES = {
    "equivalence": ("классы эквивалентности", "split every input into classes the system treats alike; one value per "
                                              "valid and invalid class"),
    "boundary": ("анализ граничных значений", "test at the boundaries of every range: min, min-1, max, max+1, empty"),
    "decision_table": ("таблица решений", "for rules with several conditions, one scenario per meaningful combination "
                                         "of conditions and its expected action"),
    "state_transition": ("диаграмма состояний", "from the lifecycle of an object: every allowed transition once, and the "
                                                "important forbidden ones"),
    "pairwise": ("попарное тестирование", "for many independent parameters, cover every pair of their values instead of "
                                          "every combination"),
    "use_case": ("сценарии использования", "the main and alternative flows of every user story end to end"),
    "error_guessing": ("предугадывание ошибок", "typical defects from experience: double submit, back button, stale data, "
                                                "concurrent edits, special characters"),
    "cause_effect": ("причина — следствие", "causes (inputs, conditions) linked to effects (outputs); a scenario per "
                                            "effect"),
    "checklist": ("чек-лист", "a short check per requirement: nothing in the requirements stays without a scenario"),
}
DEFAULT_TECHNIQUES = ["equivalence", "boundary", "state_transition", "use_case", "error_guessing"]
LAYERS = {"ui": "UI — в браузере", "api": "API — бэкенд"}

# Standards of software documentation a specification is validated against (validation.py):
# key -> (title, the sections the document must have).
STANDARDS = {
    "gost34": ("ГОСТ 34.602-2020 — ТЗ на автоматизированную систему", [
        "Общие сведения (наименование, заказчик, сроки, основания)", "Цели и назначение создания системы",
        "Характеристика объекта автоматизации", "Требования к системе в целом",
        "Требования к функциям (задачам)", "Требования к видам обеспечения (информационное, программное, техническое)",
        "Требования к надёжности, безопасности и защите информации", "Состав и содержание работ по созданию системы",
        "Порядок разработки, контроля и приёмки системы", "Требования к документированию", "Источники разработки"]),
    "gost19": ("ГОСТ 19.201-78 — ТЗ на программу (ЕСПД)", [
        "Введение", "Основания для разработки", "Назначение разработки", "Требования к функциональным характеристикам",
        "Требования к надёжности", "Условия эксплуатации", "Требования к составу и параметрам технических средств",
        "Требования к информационной и программной совместимости", "Требования к программной документации",
        "Технико-экономические показатели", "Стадии и этапы разработки", "Порядок контроля и приёмки"]),
    "iso29148": ("ISO/IEC/IEEE 29148 — спецификация требований (SRS)", [
        "Purpose and scope", "Product perspective and context", "User classes and characteristics",
        "Functional requirements", "Usability requirements", "Performance requirements",
        "Logical database / data requirements", "Design constraints", "Security, safety and privacy requirements",
        "External interfaces", "Assumptions and dependencies", "Verification (acceptance criteria)"]),
    "agile": ("User story / Agile (INVEST, критерии приёмки)", [
        "Роль, цель и ценность (Как … я хочу … чтобы …)", "Критерии приёмки (Given/When/Then или список)",
        "Бизнес-правила и ограничения", "Нефункциональные требования", "Зависимости и предусловия",
        "Определение готовности (DoD)"]),
}
# Quality of every requirement (ISO/IEC/IEEE 29148): key -> (label, what is checked).
QUALITY = {
    "complete": ("полнота", "nothing needed to implement and test it is missing: inputs, outputs, errors, roles"),
    "unambiguous": ("однозначность", "one interpretation only: no 'fast', 'convenient', 'etc.', 'as usual'"),
    "consistent": ("непротиворечивость", "does not contradict other requirements or terms"),
    "verifiable": ("проверяемость", "a test can decide pass or fail: measurable values, explicit expected results"),
    "traceable": ("трассируемость", "identified (a number or key) and linked to its source and goal"),
    "feasible": ("реализуемость", "can be built within the stated constraints"),
    "singular": ("атомарность", "one requirement per statement"),
}
