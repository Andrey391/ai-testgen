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
    "edge": ("нестандартные ситуации", "unusual but valid situations: empty lists, very long values, special characters, repeats"),
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
# What every kind of check means, for people (tooltips of the studio): key -> description.
TYPE_INFO = {
    "positive": "Основной путь с корректными данными: пользователь выполняет действие и получает ожидаемый результат. "
                "Пример: заказ с правильно заполненными полями оформляется.",
    "negative": "Неверные данные или запрещённое действие: система отказывает с понятным сообщением и ничего не меняет. "
                "Пример: вход с неверным паролем.",
    "boundary": "Значения на границах допустимого диапазона и сразу за ними: минимум, максимум, минимум − 1, "
                "максимум + 1, пусто. Пример: возраст 18–99 — проверяются 17, 18, 99 и 100. Отличие от "
                "нестандартных ситуаций: у значения есть граница (число, длина, количество), заданная требованиями.",
    "edge": "Необычные, но допустимые ситуации без явной границы: пустой список, ровно один элемент, очень длинное "
            "имя, спецсимволы и эмодзи, повторное нажатие, одинаковые записи. Пример: корзина без товаров, фамилия "
            "с дефисом и апострофом.",
    "validation": "Каждое поле ввода: обязательность, формат, длина, допустимые значения и текст ошибки под полем.",
    "error_handling": "Как система сообщает о сбоях: ошибка сервера, тайм-аут, нет данных — пользователь видит "
                      "понятное сообщение, введённое не теряется.",
    "state_transition": "Переходы объекта между статусами: разрешённые выполняются, запрещённые недоступны. Пример: "
                        "заказ «новый → оплачен → отправлен», отменить отправленный нельзя.",
    "roles": "Что каждая роль видит и может сделать; без нужной роли доступ запрещён. Пример: обычный пользователь "
             "не открывает администрирование по прямой ссылке.",
    "security": "Вход и сессии, обход прав, внедрение кода в поля ввода (XSS, SQL), доступ к данным других "
                "пользователей.",
    "accessibility": "Работа с клавиатуры, подписи и названия элементов для экранного диктора, контраст, правила WCAG.",
    "usability": "Пользователь понимает, что произошло: сообщения, подсказки, фокус, подтверждения действий.",
    "data_integrity": "Созданные и изменённые данные сохраняются, везде показываются одинаково и не дублируются.",
    "integration": "Сквозной путь через модули или системы: результат одного — вход другого. Пример: заказ с сайта "
                   "появляется в личном кабинете и в письме.",
    "compatibility": "Тот же путь в других браузерах, на других размерах экрана и устройствах.",
    "localization": "Тексты, даты, числа и валюты нужного языка и региона.",
    "performance": "Действие выполняется за ожидаемое время; списки с большим числом записей работают.",
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
# What every technique means, for people (tooltips of the studio): key -> description.
TECHNIQUE_INFO = {
    "equivalence": "Значения каждого поля делятся на группы, которые система обрабатывает одинаково; из каждой "
                   "допустимой и недопустимой группы берётся по одному значению. Пример: возраст меньше 18, "
                   "18–99, больше 99 — три значения вместо сотни.",
    "boundary": "Проверка на границах каждого диапазона: минимум, минимум − 1, максимум, максимум + 1, пусто. "
                "Дополняет классы эквивалентности: ошибки чаще всего именно на границах.",
    "decision_table": "Для правил с несколькими условиями — по сценарию на каждое значимое сочетание условий и "
                      "ожидаемое действие. Пример: скидка зависит от суммы заказа и статуса клиента.",
    "state_transition": "По жизненному циклу объекта: каждый разрешённый переход между статусами один раз и важные "
                        "запрещённые.",
    "pairwise": "Для многих независимых параметров покрывается каждая пара значений, а не все комбинации — "
                "сценариев в разы меньше. Пример: браузер × язык × способ оплаты.",
    "use_case": "Основной и альтернативные пути каждой пользовательской истории от начала до конца.",
    "error_guessing": "Типичные дефекты по опыту: двойная отправка формы, кнопка «Назад», устаревшие данные, "
                      "одновременная правка, спецсимволы.",
    "cause_effect": "Причины (входные данные, условия) связываются со следствиями (результатами); по сценарию на "
                    "каждое следствие.",
    "checklist": "Короткая проверка на каждое требование: ни одно требование не остаётся без сценария.",
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
# What every standard is for and how it differs from the others (shown under the choice of the standard).
STANDARD_INFO = {
    "gost34": "Для автоматизированной системы целиком — государственные заказчики, крупные корпоративные системы, "
              "внедрение «под ключ». Самый подробный: кроме функций описывает объект автоматизации, виды "
              "обеспечения (информационное, программное, техническое), работы по созданию системы и её приёмку.",
    "gost19": "ЕСПД — для отдельной программы, а не системы. Короче ГОСТ 34: функции, надёжность, условия "
              "эксплуатации, технические средства, совместимость, документация и порядок приёмки — без описания "
              "объекта автоматизации и организационной части.",
    "iso29148": "Международный стандарт спецификации требований (SRS), привычный продуктовым и зарубежным командам. "
                "Сосредоточен на самих требованиях: контекст продукта, классы пользователей, функциональные и "
                "нефункциональные требования, интерфейсы, данные, критерии приёмки.",
    "agile": "Не стандарт, а практика гибких команд: пользовательские истории «Как … я хочу … чтобы …» с "
             "критериями приёмки (INVEST) — для задач в трекере и итеративной разработки. Самый короткий формат.",
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
