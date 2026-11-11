"""Independent authored fixtures for an offline zero-evidence fetch fallback.

All text, URLs and page bodies below are synthetic constants. Builders perform
no file reads, network operations or corpus lookups. Expected outcomes stay in
a separate field and must never be passed to a selection or matching planner.
"""
from hashlib import sha256


FIRST9 = "Астроном тщательно настроил телескоп перед наблюдением далёкого звёздного скопления"
SECOND9 = "Архивист разложил старинные рукописи по отдельным коробкам после реставрации"

HOST_BEFORE = (
    "Редактор описывал подготовку городского фестиваля и работу небольшой волонтёрской команды. "
    "Затем записал случайное воспоминание: "
)
HOST_BETWEEN = (
    ". После перерыва команда обсудила расстановку столов, правила регистрации гостей "
    "и порядок вечерней уборки. В конце заметки редактор добавил другую историю: "
)
HOST_AFTER = ". Оставшийся раздел посвятили расписанию выступлений и проверке афиш."
TWO_INSERTION_TEXT = HOST_BEFORE + FIRST9 + HOST_BETWEEN + SECOND9 + HOST_AFTER

UNRELATED_PAGE = (
    "На берегу озера специалисты измеряли влажность почвы и глубину слоя песка. "
    "Карта участка показывала расположение кустарников, камней и заброшенной тропы."
)
FIRST_PAGE = (
    "Научный журнал включил короткий отчёт об оборудовании. " + FIRST9
    + ". Статью завершили измерениями прозрачности воздуха над вершиной холма."
)
SECOND_PAGE = (
    "Сотрудник подготовил заметку о работе хранилища. " + SECOND9
    + ". Последующие абзацы посвящены температуре помещения и состоянию переплётов."
)

NEGATIVE_PAIRS = {
    "astronomy": {
        "target": (
            "Вечером группа любителей неба вышла на пустырь рядом с посёлком. "
            "Руководитель объяснил, как отличать планеты от ярких звёзд, а дети зарисовали "
            "контуры созвездий в тетрадях. Наблюдение закончилось, когда горизонт затянуло облаками."
        ),
        "source": (
            "Для профессиональной обсерватории изготовили чувствительную камеру. "
            "Астрономические снимки позволяют оценивать движение спутников и сравнивать "
            "свечение далёких галактик. Техники проверяют оптику, систему охлаждения и точность приводов."
        ),
    },
    "archive": {
        "target": (
            "В районном читальном зале посетитель искал сведения о жизни своего прадеда. "
            "Он выписал даты из семейных писем, сравнил фамилии в справочнике и попросил "
            "сотрудницу показать опись дореволюционных документов. Нужная запись обнаружилась ближе к закрытию."
        ),
        "source": (
            "Хранилище муниципального архива оборудовали регулируемыми стеллажами. "
            "Перед переносом фонда реставраторы измерили кислотность бумаги, очистили папки "
            "и присвоили коробам новые шифры. Электронный каталог хранит описание коллекций и правила выдачи дел."
        ),
    },
    "print": {
        "target": (
            "Мастерская получила заказ на праздничные приглашения для выпускников. "
            "Дизайнер выбрала спокойные цвета, согласовала расположение эмблемы и принесла "
            "заказчику пробный оттиск. После обсуждения полей весь тираж решили отпечатать на плотном картоне."
        ),
        "source": (
            "В типографском цехе установили машину для рулонной печати газет. "
            "Оператор контролирует натяжение полотна, подачу краски и совмещение цветовых слоёв. "
            "Готовые полосы поступают в сушильную секцию, затем режутся и складываются автоматическим модулем."
        ),
    },
}

PREFIX_PAGES = 12
EXTRA_PAGE_CAP = 12
DISCOVERED_PAGES = 26
FIXTURE_FETCHED_AT = "2026-01-01T00:00:00+00:00"


def page(url, text, title="Authored offline page"):
    return {"url": url, "requested_url": url, "title": title, "text": text, "links": [],
        "text_sha256": sha256(text.encode()).hexdigest(), "fetched_at": FIXTURE_FETCHED_AT,
        "content_type": "text/plain", "extraction": "plain-v1"}


def _pages(name, source_text):
    return [page(f"https://authored-zero.test/{name}/page-{index:02d}",
                 source_text + f" Номер карточки: {index}.")
            for index in range(1, DISCOVERED_PAGES + 1)]


def _bundle(name, text, pages, expected):
    return {"name": name, "text": text, "prefix_pages": pages[:PREFIX_PAGES],
        "pages": {item["url"]: item for item in pages}, "search_order": [item["url"] for item in pages],
        "expected": expected}


def negative_fixture(topic):
    pair = NEGATIVE_PAIRS[topic]
    return _bundle("negative-" + topic, pair["target"], _pages("negative-" + topic, pair["source"]),
        {"initial_grounded_exact_words": 0, "final_grounded_exact_words": 0,
         "new_exact_words": 0, "extra_attempts": EXTRA_PAGE_CAP,
         "total_page_attempts": PREFIX_PAGES + EXTRA_PAGE_CAP, "unread_discovered_urls": 2,
         "gate_open": True})


def two_insertion_fixture(*, initial_positive=False):
    name = "initial-positive" if initial_positive else "two-insertions"
    pages = _pages(name, UNRELATED_PAGE)
    pages[12] = page(pages[12]["url"], FIRST_PAGE, "First nine-word insertion")
    pages[13] = page(pages[13]["url"], SECOND_PAGE, "Second nine-word insertion")
    if initial_positive:
        pages[0] = page(pages[0]["url"], FIRST_PAGE, "First insertion already in prefix")
    return _bundle(name, TWO_INSERTION_TEXT, pages,
        {"initial_grounded_exact_words": 9 if initial_positive else 0,
         "final_grounded_exact_words": 9 if initial_positive else 18,
         "new_exact_words": 0 if initial_positive else 18,
         "extra_attempts": 0 if initial_positive else EXTRA_PAGE_CAP,
         "total_page_attempts": PREFIX_PAGES if initial_positive else PREFIX_PAGES + EXTRA_PAGE_CAP,
         "unread_discovered_urls": DISCOVERED_PAGES - PREFIX_PAGES if initial_positive else 2,
         "gate_open": not initial_positive})
