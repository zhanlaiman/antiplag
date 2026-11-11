"""Curated, attributed public-source collections for the local library."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from plagiarism.matching import MAX_TEXT_CHARS


PACK_DIRECTORY = Path(__file__).resolve().parent.parent / "data" / "source-packs"
PACK_ID = "economics-ru-v1"
PACK_TITLE = "Экономика · русская Википедия"
PACK_DESCRIPTION = "Открытые статьи об экономике, ресурсах, потребностях и экономических системах. Проверка ограничена текстами установленного набора."
API_URL = "https://ru.wikipedia.org/w/api.php"
USER_AGENT = "IstokResearch/0.2 (https://github.com/zhanlaiman/antiplag; local prototype)"
LICENSE_NAME = "CC BY-SA 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
LICENSE_POLICY_URL = "https://ru.wikipedia.org/wiki/Википедия:Текст_лицензии_Creative_Commons_Attribution-ShareAlike_4.0_International"
MAX_RESPONSE_BYTES = 8_000_000
ECONOMICS_TITLES = (
    "Экономика", "Экономическая теория", "Микроэкономика", "Макроэкономика",
    "Экономические ресурсы", "Потребность", "Альтернативная стоимость", "Факторы производства",
    "Разделение труда", "Спрос и предложение", "Рыночная экономика", "Плановая экономика",
    "Традиционная экономика", "Смешанная экономика", "Собственность",
)


class SourcePackError(ValueError):
    pass


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def _content_key(text):
    return _digest(" ".join(text.split()))


def _wikipedia_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and parsed.netloc == "ru.wikipedia.org" and parsed.path.startswith(("/wiki/", "/w/index.php"))


def validate_pack(pack, expected_id=None):
    if (not isinstance(pack, dict) or not isinstance(pack.get("id"), str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", pack["id"])):
        raise SourcePackError("Некорректный идентификатор набора источников.")
    if expected_id is not None and pack["id"] != expected_id:
        raise SourcePackError("Идентификатор набора не совпадает с именем файла.")
    if not all(isinstance(pack.get(key), str) and pack[key].strip() for key in ("title", "description")):
        raise SourcePackError("У набора отсутствует название или описание.")
    sources = pack.get("sources")
    if not isinstance(sources, list) or not sources or type(pack.get("source_count")) is not int or pack["source_count"] != len(sources):
        raise SourcePackError("Число источников не совпадает с содержимым набора либо набор пуст.")
    identifiers, content_keys = set(), set()
    for source in sources:
        if (not isinstance(source, dict) or not isinstance(source.get("id"), str)
                or not re.fullmatch(r"wiki-ru-[1-9]\d*-[1-9]\d*", source["id"])):
            raise SourcePackError("Источник не содержит идентификатор страницы и редакции.")
        text = source.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise SourcePackError("Источник пуст или превышает допустимый размер.")
        if not isinstance(source.get("title"), str) or not source["title"].strip() or not _wikipedia_url(source.get("url")):
            raise SourcePackError("У источника отсутствует название или ссылка на русскую Википедию.")
        provenance = source.get("provenance")
        if not isinstance(provenance, dict):
            raise SourcePackError("У источника отсутствуют сведения о происхождении.")
        if provenance.get("license_name") != LICENSE_NAME or provenance.get("license_url") != LICENSE_URL:
            raise SourcePackError("У источника отсутствует подтверждённая лицензия CC BY-SA 4.0.")
        if provenance.get("publisher") != "Википедия" or not all(
            isinstance(provenance.get(key), str) and provenance[key].strip()
            for key in ("attribution", "fetched_at", "extraction")
        ):
            raise SourcePackError("У источника отсутствуют авторство, издатель, дата или способ извлечения.")
        try:
            fetched_at = datetime.fromisoformat(provenance["fetched_at"])
        except ValueError as error:
            raise SourcePackError("Дата получения источника некорректна.") from error
        if fetched_at.tzinfo is None:
            raise SourcePackError("Дата получения источника должна содержать часовой пояс.")
        page_id, revision_id = provenance.get("page_id"), provenance.get("revision_id")
        if type(page_id) is not int or type(revision_id) is not int or source["id"] != f"wiki-ru-{page_id}-{revision_id}":
            raise SourcePackError("Редакция и страница не совпадают с идентификатором источника.")
        if not _wikipedia_url(provenance.get("revision_url")) or not _wikipedia_url(provenance.get("history_url")):
            raise SourcePackError("Отсутствуют ссылки на редакцию и историю авторов.")
        if (parse_qs(urlsplit(provenance["revision_url"]).query).get("oldid") != [str(revision_id)]
                or parse_qs(urlsplit(provenance["history_url"]).query).get("action") != ["history"]):
            raise SourcePackError("Ссылка на редакцию или историю не совпадает с метаданными.")
        if provenance.get("revision_binding") != "reported_latest_revision":
            raise SourcePackError("Не указан способ связи текста с сообщённой редакцией.")
        if provenance.get("text_sha256") != _digest(text):
            raise SourcePackError("Контрольная сумма текста источника не совпадает.")
        content_key = _content_key(text)
        if source["id"] in identifiers or content_key in content_keys:
            raise SourcePackError("Набор содержит повторяющийся источник или текст.")
        identifiers.add(source["id"])
        content_keys.add(content_key)
    return pack


def load_pack(pack_id, directory=PACK_DIRECTORY):
    if not isinstance(pack_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", pack_id):
        raise SourcePackError("Некорректный идентификатор набора источников.")
    path = Path(directory) / (pack_id + ".json")
    if not path.is_file():
        raise KeyError(pack_id)
    try:
        pack = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise SourcePackError("Не удалось прочитать сохранённый набор источников.") from error
    return validate_pack(pack, pack_id)


def list_packs(directory=PACK_DIRECTORY):
    packs = []
    for path in sorted(Path(directory).glob("*.json")):
        pack = load_pack(path.stem, directory)
        packs.append({"id": pack["id"], "title": pack["title"], "description": pack["description"],
            "source_count": len(pack["sources"]), "requested_source_count": pack.get("requested_source_count", len(pack["sources"])),
            "omitted": pack.get("omitted", []), "total_characters": sum(len(source["text"]) for source in pack["sources"]),
            "license_name": LICENSE_NAME, "license_url": LICENSE_URL, "language": "ru", "publisher": "Википедия"})
    return packs


def request_json(parameters):
    url = API_URL + "?" + urlencode({"format": "json", "formatversion": "2", "maxlag": "5", **parameters})
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise SourcePackError("Ответ Википедии превышает допустимый размер.")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise SourcePackError("Википедия вернула неожиданный формат ответа.")
            if result.get("error"):
                error = result["error"]
                if error.get("code") == "maxlag" and attempt < 2:
                    time.sleep(5)
                    continue
                raise SourcePackError(f"Ошибка API Википедии — {str(error.get('info', error.get('code')))[:300]}")
            return result
        except HTTPError as error:
            if error.code in {429, 502, 503, 504} and attempt < 2:
                retry_after = error.headers.get("Retry-After", "5")
                delay = min(30, max(1, int(retry_after))) if retry_after.isdigit() else 5
                time.sleep(delay)
                continue
            raise SourcePackError(f"Википедия завершила запрос с ошибкой HTTP {error.code}.") from error
        except (URLError, OSError) as error:
            if attempt < 2:
                time.sleep(2)
                continue
            raise SourcePackError("Не удалось связаться с API Википедии.") from error
        except (json.JSONDecodeError, UnicodeError) as error:
            raise SourcePackError("Не удалось прочитать ответ API Википедии.") from error


def fetch_article(title, fetch_json=request_json):
    response = fetch_json({"action": "query", "prop": "extracts|info|revisions", "titles": title,
        "redirects": "1", "explaintext": "1", "exsectionformat": "plain", "inprop": "url",
        "rvprop": "ids|timestamp", "rvlimit": "1"})
    pages = response.get("query", {}).get("pages", [])
    if len(pages) != 1 or pages[0].get("missing") or pages[0].get("invalid"):
        raise SourcePackError("Статья не найдена по указанному названию.")
    page = pages[0]
    revisions = page.get("revisions", [])
    text = page.get("extract")
    if (type(page.get("pageid")) is not int or page["pageid"] < 1 or not revisions
            or type(revisions[0].get("revid")) is not int or revisions[0]["revid"] < 1):
        raise SourcePackError("API не сообщил страницу и её текущую редакцию.")
    if not isinstance(text, str) or not text.strip():
        raise SourcePackError("API не вернул непустой текст статьи.")
    if len(text) > MAX_TEXT_CHARS:
        raise SourcePackError(f"Статья содержит {len(text)} символов и превышает лимит {MAX_TEXT_CHARS}. Для включения требуется разделение на части.")
    canonical_title = page.get("title")
    if not isinstance(canonical_title, str) or not canonical_title.strip():
        raise SourcePackError("API не сообщил название статьи.")
    canonical_url = page.get("canonicalurl") or page.get("fullurl")
    if not _wikipedia_url(canonical_url):
        raise SourcePackError("API не сообщил корректную ссылку на статью.")
    revision_id = revisions[0]["revid"]
    article_key = quote(canonical_title.replace(" ", "_"), safe="")
    history_url = f"https://ru.wikipedia.org/w/index.php?title={article_key}&action=history"
    revision_url = f"https://ru.wikipedia.org/w/index.php?title={article_key}&oldid={revision_id}"
    return {"id": f"wiki-ru-{page['pageid']}-{revision_id}", "title": canonical_title, "text": text, "url": canonical_url,
        "provenance": {"publisher": "Википедия", "license_name": LICENSE_NAME, "license_url": LICENSE_URL,
            "license_policy_url": LICENSE_POLICY_URL, "page_id": page["pageid"], "revision_id": revision_id,
            "revision_url": revision_url, "revision_timestamp": revisions[0].get("timestamp"),
            "revision_binding": "reported_latest_revision", "history_url": history_url, "fetched_at": _timestamp(),
            "attribution": f"Авторы статьи «{canonical_title}» в русской Википедии указаны в истории правок {history_url}. Текст используется по лицензии {LICENSE_NAME} {LICENSE_URL}",
            "extraction": "MediaWiki TextExtracts API, explaintext, полный полученный ответ без обрезки. API одновременно сообщает последнюю редакцию; тождество извлечения именно этой редакции отдельно не подтверждено. Разметка преобразована API в обычный текст.",
            "text_sha256": _digest(text)}}


def collect_pack(titles=ECONOMICS_TITLES, *, fetch_json=request_json, on_progress=None, pause_seconds=0.2):
    titles = list(titles)
    if not titles or any(not isinstance(title, str) or not title.strip() for title in titles):
        raise SourcePackError("Укажите непустые названия статей.")
    sources, omitted, content_keys = [], [], set()
    for number, title in enumerate(titles, 1):
        try:
            source = fetch_article(title, fetch_json)
            content_key = _content_key(source["text"])
            if content_key in content_keys:
                raise SourcePackError("Текст уже получен по другому названию; повтор исключён.")
            content_keys.add(content_key)
            sources.append(source)
            event = {"title": title, "status": "collected", "source_id": source["id"], "characters": len(source["text"])}
        except ValueError as error:
            failure = {"requested_title": title, "error": str(error)}
            omitted.append(failure)
            event = {"title": title, "status": "omitted", "error": str(error)}
        if on_progress:
            on_progress(event, number, len(titles))
        if pause_seconds and number < len(titles):
            time.sleep(pause_seconds)
    if not sources:
        raise SourcePackError("Не удалось получить ни одного источника. Набор не сохранён. " + "; ".join(item["error"] for item in omitted[:3]))
    pack = {"id": PACK_ID, "title": PACK_TITLE, "description": PACK_DESCRIPTION,
        "language": "ru", "topic": "economics", "created_at": _timestamp(),
        "source_count": len(sources), "requested_source_count": len(titles), "requested_titles": titles,
        "omitted": omitted, "sources": sources,
        "coverage_note": "Область проверки ограничена перечисленными текстами русской Википедии.",
        "license_policy_url": LICENSE_POLICY_URL}
    return validate_pack(pack)
