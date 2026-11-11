"""Render an offline, script-free report from saved evidence only."""

from datetime import datetime, timezone
from html import escape
from urllib.parse import urlsplit


RELATIONS = {"exact": "Дословное совпадение", "paraphrase": "Возможный пересказ",
             "same_topic": "Общая тема", "unrelated": "Разные тексты"}
DECISIONS = {"confirmed": "Совпадение подтверждено редактором",
             "dismissed": "Находка отклонена редактором", "pending": "Решение не принято"}


def _text(value):
    return escape(str(value if value is not None else ""), quote=True)


def _link(url, label):
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
    except ValueError:
        parts = None
    if (parts and parts.scheme in {"http", "https"} and parts.netloc
            and not any(ord(char) < 32 for char in url)):
        return f'<a href="{_text(url)}" rel="noopener noreferrer">{_text(label)}</a>'
    return _text(label)


def _pair(text, source):
    return ('<div class="pair"><div><h4>Проверяемый текст</h4><blockquote>' + _text(text)
            + '</blockquote></div><div><h4>Источник</h4><blockquote>' + _text(source)
            + '</blockquote></div></div>')


def _date(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return _text(value)
        return parsed.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
    except (AttributeError, TypeError, ValueError):
        return _text(value)


def _evidence(judgment, candidate):
    # Old or externally modified records must not turn an invented quote into evidence.
    for name, full in (("text", candidate.get("query_excerpt", "")),
                       ("source", candidate.get("source_excerpt", ""))):
        excerpt = judgment.get(f"evidence_{name}")
        if not isinstance(excerpt, str) or not excerpt or excerpt not in full:
            return '<p class="notice">Выдержки не подтверждены сохранённой парой. Нужна повторная сверка.</p>'
    return _pair(judgment["evidence_text"], judgment["evidence_source"])


def render(report, reviews=None, retrieval=None, outcomes=()):
    """Return standalone HTML. This never fetches sources or invokes a model.

    The caller supplies the latest saved outcome per candidate, including errors.
    A failed recheck therefore cannot export an older positive judgment.
    """
    reviews = reviews or {}
    stats = report.get("stats", {})
    matches = report.get("matches", [])
    chunks = []
    for index, match in enumerate(matches, 1):
        review = reviews.get(match["id"], {})
        chunks.append('<article><h3>' + str(index) + '. ' + _text(match.get("source_title", "Источник"))
                      + '</h3><p class="tag">' + ("Дословно" if match.get("kind") == "exact" else "Похожая формулировка")
                      + '</p>' + _pair(match.get("excerpt", ""), match.get("source_excerpt", ""))
                      + '<p>' + _link(match.get("source_url"), "Открыть источник") + '</p>'
                      + ('<p>Есть признаки оформления цитаты; корректность оформления требует сверки.</p>' if match.get("quoted") else "")
                      + '<p><strong>' + _text(DECISIONS.get(review.get("decision"), "Решение не принято"))
                      + '</strong></p>' + ('<p class="preserve">' + _text(review["comment"]) + '</p>' if review.get("comment") else "")
                      + '</article>')
    lexical = ''.join(chunks) or '<p>Лексических совпадений в выбранной библиотеке не найдено. Это не исключает пересказов и источников за её пределами.</p>'

    semantic = '<p>Поиск кандидатов для этого отчёта не сохранён. Смысловая проверка не подтверждена.</p>'
    if retrieval:
        latest = {item["candidate_id"]: item for item in outcomes}
        cards = []
        for candidate in retrieval.get("candidates", []):
            outcome = latest.get(candidate["id"], {})
            judgment = outcome.get("judgment")
            title = '<h3>' + _text(candidate.get("source_title", "Источник")) + '</h3>'
            meta = '<p>Место ' + _text(candidate.get("rank")) + ' в выдаче для фрагмента. ' + _link(candidate.get("source_url"), "Источник") + '</p>'
            if outcome.get("error"):
                body = '<p class="notice">Последняя проверка не завершена: ' + _text(outcome["error"]) + '</p>'
                body += _pair(candidate.get("query_excerpt", ""), candidate.get("source_excerpt", ""))
            elif judgment:
                relation = RELATIONS.get(judgment.get("relation"))
                body = '<p class="tag">' + _text(relation or "Неизвестная оценка — нужна сверка") + '</p>'
                body += '<p>Модель: ' + _text(judgment.get("model", "не указана")) + '</p>'
                if relation:
                    body += _evidence(judgment, candidate) if judgment.get("relation") in {"exact", "paraphrase"} else _pair(candidate.get("query_excerpt", ""), candidate.get("source_excerpt", ""))
                body += '<p class="preserve">Объяснение модели: ' + _text(judgment.get("explanation", "")) + '</p>'
            else:
                body = '<p>Кандидат найден поиском. Сохранённой оценки Qwen нет.</p>'
                body += _pair(candidate.get("query_excerpt", ""), candidate.get("source_excerpt", ""))
            cards.append('<article>' + title + meta + body + '</article>')
        semantic = ('<p>Режим поиска: ' + _text(retrieval.get("mode", "не указан"))
                    + '. Кандидат сам по себе не является подтверждением заимствования.</p>'
                    + ''.join(cards))
        if not cards:
            semantic += '<p>Кандидатов не найдено. Источники вне библиотеки не проверены.</p>'
        for warning in retrieval.get("warnings", []):
            semantic += '<p class="notice">' + _text(warning) + '</p>'

    bibliography = []
    for source in report.get("sources", []):
        provenance = source.get("provenance") or {}
        bibliography.append('<li><strong>' + _link(source.get("url"), source.get("title", "Источник")) + '</strong>'
                            + ('<p>' + _text(provenance["attribution"]) + '</p>' if provenance.get("attribution") else "")
                            + ('<p>' + _link(provenance.get("license_url"), provenance["license_name"]) + '</p>' if provenance.get("license_name") else "")
                            + '</li>')
    issues = ''.join('<p class="notice">' + _text(issue.get("message", "") if isinstance(issue, dict) else issue) + '</p>'
                     for issue in report.get("issues", []))
    return '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Исток — отчёт для сверки</title><style>
*{box-sizing:border-box}body{margin:0;background:#f6f5ef;color:#213a31;font:16px/1.65 system-ui,sans-serif}
main{max-width:1040px;margin:40px auto;padding:36px;background:white;border-radius:20px}h1{font:42px/1.2 Georgia,serif}h2{margin-top:38px}
article{padding:20px 0;border-bottom:1px solid #dce3dd}h3,h4{margin:8px 0}h4{font-size:14px;color:#54675d}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:24px}blockquote{margin:0;padding:12px 16px;background:#f3f6f2;border-left:3px solid #7c9b87;white-space:pre-wrap;overflow-wrap:anywhere}
a{color:#18553a;overflow-wrap:anywhere}.notice{padding:16px;background:#fff3df;border-radius:8px}.tag{font-weight:650}.preserve{white-space:pre-wrap;overflow-wrap:anywhere}
li{margin-bottom:20px}li p{margin:4px 0}.meta{color:#54675d;font-size:14px}footer{border-top:1px solid #dce3dd;margin-top:32px;padding-top:16px}
@media(max-width:650px){main{margin:0;padding:20px;border-radius:0}.pair{grid-template-columns:1fr}h1{font-size:32px}}
@media print{body{background:white;font-size:11pt}main{margin:0;padding:0;max-width:none}h2,h3,h4{break-after:avoid}blockquote{break-inside:avoid}.notice{border:1px solid #aaa}a::after{content:" (" attr(href) ")";font-size:9pt}}
</style></head><body><main><header><p>ИСТОК · ОТЧЁТ ДЛЯ СВЕРКИ</p><h1>Проверка по библиотеке источников</h1>
<p class="meta">Отчёт ''' + _text(report.get("id")) + ' · Сохранён ' + _date(report.get("created_at")) + '''</p></header>
<p class="notice">Проверена выбранная библиотека, а не весь интернет. Сходство не устанавливает направление копирования или нарушение правил. Корректная цитата может совпадать с источником.</p>
<p><strong>''' + _text(stats.get("similarity_percent", 0)) + '%</strong> текста с лексическими совпадениями. Проверено источников: ' + _text(stats.get("checked_source_count", len(report.get("sources", [])))) + '. Найдено фрагментов: ' + str(len(matches)) + '''. Пересечения учитываются один раз; смысловые оценки в этот процент не входят.</p>
''' + issues + '''<h2>Лексические совпадения и решения редактора</h2>''' + lexical + '''
<h2>Поиск кандидатов и последние оценки Qwen</h2>''' + semantic + '''
<h2>Сохранённая версия проверяемого текста</h2><div class="preserve">''' + _text(report.get("text", "")) + '''</div>
<h2>Библиотека этого отчёта</h2><p>Список соответствует сохранённому отчёту. Изменения текущей библиотеки его не заменяют.</p><ol>''' + ''.join(bibliography) + '''</ol>
<footer class="meta">Автономная копия отчёта. Новые проверки и правки в этом файле не выполняются. Окончательное решение принимает редактор.</footer></main></body></html>'''
