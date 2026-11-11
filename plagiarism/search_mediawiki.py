"""Bounded anonymous Wikipedia discovery; only fetched pages are evidence.

Public read API: https://www.mediawiki.org/wiki/API:Search
No key, authentication, credits, automatic retry, or API redirects are used.
"""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener

from plagiarism.auth_provider import NoRedirect
from plagiarism.web_search import WebSearchError

ROOT = Path(__file__).resolve().parent.parent
ENDPOINT = "https://ru.wikipedia.org/w/api.php"
USER_AGENT = "IstokResearchBot/0.1 (https://github.com/zhanlaiman/antiplag; bounded source discovery)"
MAX_RESPONSE_BYTES = 256 * 1024
DAILY_LIMIT = 200


def _retry_delay(headers, now):
    """Untrusted optional headers must never prevent persisting a cooldown."""
    value = headers.get("Retry-After", "") if hasattr(headers, "get") else ""
    if not isinstance(value, str):
        return 900
    value = value.strip()
    if value.isascii() and value.isdecimal():
        return 86400 if len(value) > 6 else min(86400, max(900, int(value)))
    try:
        date = parsedate_to_datetime(value)
        return min(86400, max(900, date.timestamp() - now))
    except (ValueError, TypeError, OverflowError, IndexError):
        return 900


class MediaWikiSearch:
    provider = "mediawiki"

    def __init__(self, path=None, *, opener=None, now=None, sleep=None):
        self.path = Path(path) if path is not None else ROOT / "data/private/mediawiki.sqlite3"
        self.opener = opener or build_opener(NoRedirect())
        self.now, self.sleep = now or time.time, sleep or time.sleep
        self.halt_code = None
        self.last_response = self.last_usage = None

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WebSearchError("Дополнительный поиск занят; основной результат сохранён.", "search_rate_limited") from None
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            try:
                with closing(sqlite3.connect(self.path, timeout=1)) as db:
                    db.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, query_sha TEXT NOT NULL, at REAL NOT NULL, state TEXT NOT NULL, results TEXT)")
                    db.execute("CREATE TABLE IF NOT EXISTS settings (id TEXT PRIMARY KEY, value REAL NOT NULL)")
                    yield db
            except sqlite3.Error:
                raise WebSearchError("Журнал дополнительного поиска недоступен; повтор исключён.", "search_provider_error") from None

    def _parse(self, raw):
        value = json.loads(raw)
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if not isinstance(value, dict) or value.get("error"):
            code = value.get("error", {}).get("code") if isinstance(value, dict) else None
            raise WebSearchError("Дополнительный поиск недоступен.", "search_rate_limited" if code == "maxlag" else "search_provider_error")
        rows = value.get("query", {}).get("search")
        if not isinstance(rows, list) or len(rows) > 5:
            raise ValueError("invalid search response")
        results, seen = [], set()
        for row in rows:
            if (not isinstance(row, dict) or type(row.get("ns")) is not int or row["ns"] != 0
                    or type(row.get("pageid")) is not int or row["pageid"] < 1
                    or not isinstance(row.get("title"), str) or not 1 <= len(row["title"]) <= 512
                    or any(ord(char) < 32 or ord(char) == 127 for char in row["title"])):
                raise ValueError("invalid search item")
            url = "https://ru.wikipedia.org/wiki/" + quote(row["title"].replace(" ", "_"), safe="")
            if url not in seen:
                results.append({"url": url})
                seen.add(url)
        return value, results

    def search(self, query, request_id):
        if (not isinstance(query, str) or not query.strip() or len(query) > 400
                or any(ord(char) < 32 for char in query) or not isinstance(request_id, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id) is None):
            raise WebSearchError("Некорректный дополнительный запрос.", "invalid_search_query")
        try:
            query_sha = sha256(query.encode()).hexdigest()
        except UnicodeError:
            raise WebSearchError("Некорректный дополнительный запрос.", "invalid_search_query") from None
        self.last_response = self.last_usage = None
        if self.halt_code:
            raise WebSearchError("Дополнительный поиск остановлен без повторов.", self.halt_code)
        with self._locked() as db:
            previous = db.execute("SELECT query_sha,state,results FROM calls WHERE id=?", (request_id,)).fetchone()
            if previous:
                if previous[0] == query_sha and previous[1] == "complete":
                    results = json.loads(previous[2])
                    self.last_usage = {"public_queries": 0, "cost_kzt": 0, "cached": True}
                    return results
                raise WebSearchError("Запрос уже отправлялся; повтор исключён.", "search_already_attempted")
            now = self.now()
            pause = db.execute("SELECT value FROM settings WHERE id='pause_until'").fetchone()
            if pause and pause[0] > now:
                raise WebSearchError("Дополнительный поиск временно ограничен.", "search_rate_limited")
            day = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            count = db.execute("SELECT count(*) FROM calls WHERE at>=?", (day,)).fetchone()[0]
            if count >= DAILY_LIMIT:
                raise WebSearchError("Достигнут дневной предел дополнительного поиска.", "search_budget_exhausted")
            last = db.execute("SELECT max(at) FROM calls").fetchone()[0]
            if last is not None and now - last < 1:
                self.sleep(min(1, max(0, 1 - (now - last))))
            db.execute("INSERT INTO calls VALUES (?,?,?,?,NULL)", (request_id, query_sha, self.now(), "submitted"))
            db.commit()  # Durable intent before any network I/O, even if the process dies.
            self.last_usage = {"public_queries": 1, "cost_kzt": 0, "cached": False}
            parameters = {"action": "query", "format": "json", "formatversion": 2, "list": "search",
                          "srnamespace": 0, "srlimit": 5, "srprop": "", "maxlag": 5, "srsearch": query}
            request = Request(ENDPOINT + "?" + urlencode(parameters), headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            try:
                with self.opener.open(request, timeout=15) as response:
                    status = response.status
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if status != 200 or len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("invalid response")
                decoded, results = self._parse(raw)
                db.execute("UPDATE calls SET state='complete', results=? WHERE id=?", (json.dumps(results, ensure_ascii=False), request_id))
                db.commit()
                self.last_response = {"full_api_response": decoded, "response_sha256": sha256(raw).hexdigest(), "results": results}
                self.last_usage = {"public_queries": 1, "cost_kzt": 0, "cached": False}
                return results
            except (HTTPError, URLError, OSError, ValueError, UnicodeError, TypeError, AttributeError, RecursionError, WebSearchError) as error:
                http_status = error.code if isinstance(error, HTTPError) else None
                delay = _retry_delay(error.headers, self.now()) if isinstance(error, HTTPError) else 900
                if isinstance(error, HTTPError):
                    error.close()
                code = ("search_rate_limited" if http_status in (429, 503)
                        else error.code if isinstance(error, WebSearchError) else "search_provider_error")
                self.halt_code = code
                db.execute("UPDATE calls SET state='failed_or_unknown' WHERE id=?", (request_id,))
                if code == "search_rate_limited":
                    db.execute("INSERT OR REPLACE INTO settings VALUES ('pause_until',?)", (self.now() + delay,))
                db.commit()
                raise WebSearchError("Дополнительный поиск недоступен; результаты основного прохода сохранены.", code) from None
