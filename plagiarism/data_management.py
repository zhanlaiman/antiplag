"""Reversible tenant-local lifecycle and streaming portable data exports."""
from datetime import datetime, timezone
import json
import re
import tempfile
import zipfile


# Only score metadata is read for lists; source text, URLs and search errors stay
# in the saved report and are not hydrated for the dashboard or history.
REPORT_SCORE_COLUMNS = """
    json_extract(r.data,'$.stats') AS stats,
    CASE WHEN json_type(r.data,'$.web')='object' THEN json_object(
        'status',json_extract(r.data,'$.web.status'),
        'sources_checked',json_extract(r.data,'$.web.sources_checked'),
        'errors_count',coalesce(json_array_length(r.data,'$.web.errors'),0)) END AS web,
    coalesce(json_array_length(r.data,'$.sources_refs'),
             json_array_length(r.data,'$.sources'),0) AS source_snapshots_count
"""


def report_score_preview(row):
    result = dict(row)
    for name in ('stats', 'web'):
        try:
            value = json.loads(result[name]) if isinstance(result.get(name), str) else result.get(name)
        except (ValueError, TypeError):
            value = None
        result[name] = value if isinstance(value, dict) else {}
    return result


class DataArchive:
    """An already finished ZIP; the HTTP handler owns and closes the file."""
    def __init__(self, stream):
        self.stream = stream


class DataManagement:
    def dictionary_words(self):
        with self.connect() as db:
            return [row[0] for row in db.execute('SELECT word FROM dictionary_words ORDER BY word')]

    def update_dictionary(self, word, action):
        if not isinstance(word, str) or not re.fullmatch(r'[А-Яа-яЁёA-Za-z]{2,40}', word):
            raise ValueError('В личный словарь можно добавить одно слово из 2–40 букв.')
        if action not in {'add', 'remove'}:
            raise ValueError('Неизвестное действие со словарём.')
        word = word.casefold()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if action == 'remove':
                db.execute('DELETE FROM dictionary_words WHERE word=?', (word,))
            else:
                if db.execute('SELECT count(*) FROM dictionary_words').fetchone()[0] >= 2000 and not db.execute('SELECT 1 FROM dictionary_words WHERE word=?', (word,)).fetchone():
                    raise ValueError('В личном словаре доступно до 2000 слов.')
                db.execute('INSERT OR IGNORE INTO dictionary_words VALUES (?,?)', (word, datetime.now(timezone.utc).isoformat()))
        return {'word': word, 'action': action}

    def is_trashed(self, kind, identifier):
        with self.connect() as db:
            if kind == 'correction':return db.execute('SELECT 1 FROM correction_trash WHERE id=?',(identifier,)).fetchone() is not None
            return db.execute('SELECT 1 FROM trash WHERE kind=? AND id=?', (kind, identifier)).fetchone() is not None

    def trash_item(self, kind, identifier):
        if kind not in {'source', 'report', 'correction'}:
            raise ValueError('Неизвестный тип записи.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT 1 FROM sources WHERE id=?', (identifier,)).fetchone() if kind == 'source' else db.execute(
                'SELECT 1 FROM records WHERE kind=? AND id=?', (kind, identifier)).fetchone()
            if not row:
                return False
            if kind == 'correction':
                return db.execute('INSERT OR IGNORE INTO correction_trash VALUES (?,?)',
                                  (identifier,datetime.now(timezone.utc).isoformat())).rowcount > 0
            return db.execute('INSERT OR IGNORE INTO trash VALUES (?,?,?)',
                              (kind, identifier, datetime.now(timezone.utc).isoformat())).rowcount > 0

    def restore_item(self, kind, identifier):
        if kind not in {'source', 'report', 'correction'}:
            raise ValueError('Неизвестный тип записи.')
        with self.connect() as db:
            if kind == 'correction':return db.execute('DELETE FROM correction_trash WHERE id=?',(identifier,)).rowcount > 0
            return db.execute('DELETE FROM trash WHERE kind=? AND id=?', (kind, identifier)).rowcount > 0

    def data_summary(self):
        """Counts and bounded previews only; large report snapshots stay on disk."""
        with self.connect() as db:
            reports = [report_score_preview(row) for row in db.execute(f"""SELECT r.id,r.created_at,
                substr(json_extract(r.data,'$.text'),1,180) AS preview,
                json_extract(r.data,'$.scope') AS scope,t.deleted_at,{REPORT_SCORE_COLUMNS}
                FROM records r LEFT JOIN trash t ON t.kind='report' AND t.id=r.id
                WHERE r.kind='report' ORDER BY r.created_at DESC,r.id LIMIT 500""")]
            sources = [dict(row) for row in db.execute("""SELECT s.id,s.title,s.url,s.created_at,
                length(s.text) AS characters,t.deleted_at FROM sources s
                LEFT JOIN trash t ON t.kind='source' AND t.id=s.id
                ORDER BY s.created_at DESC,s.id""")]
            totals = {'reports': db.execute("SELECT count(*) FROM records WHERE kind='report'").fetchone()[0],
                      'sources': len(sources), 'trashed': db.execute('SELECT count(*) FROM trash').fetchone()[0]+db.execute('SELECT count(*) FROM correction_trash').fetchone()[0]}
            corrections = [dict(row) for row in db.execute("""SELECT r.id,r.created_at,
                substr(json_extract(r.data,'$.text'),1,180) AS preview,t.deleted_at
                FROM records r LEFT JOIN correction_trash t ON t.id=r.id
                WHERE r.kind='correction' ORDER BY r.created_at DESC,r.id LIMIT 500""")]
            active_totals = {
                'reports': db.execute("SELECT count(*) FROM records r WHERE kind='report' AND NOT EXISTS (SELECT 1 FROM trash t WHERE t.kind='report' AND t.id=r.id)").fetchone()[0],
                'sources': sum(not source['deleted_at'] for source in sources),
                'corrections': db.execute("SELECT count(*) FROM records r WHERE kind='correction' AND NOT EXISTS (SELECT 1 FROM correction_trash t WHERE t.id=r.id)").fetchone()[0],
            }
        return {'reports': reports, 'sources': sources, 'corrections':corrections,'totals': totals,
                'active_totals': active_totals, 'listed_reports_limit': 500, 'database_bytes': self.path.stat().st_size}

    def export_data(self):
        """Consistent snapshot without credentials, jobs, quotas or cross-tenant data.

        JSONL records use stored source references resolved by source_snapshots.jsonl.
        Export includes the trash and never modifies operational/idempotency records.
        """
        stream = tempfile.TemporaryFile('w+b')
        try:
            with self.connect() as db, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
                db.execute('BEGIN')
                manifest = {'format': 'istok-personal-data', 'version': 1,
                            'created_at': datetime.now(timezone.utc).isoformat(),
                            'includes_trash': True,
                            'report_sources': 'sources_refs refer to hash in source_snapshots.jsonl',
                            'omitted': ['credentials', 'request reservations', 'search quotas', 'server logs']}
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
                queries = {
                    'sources': 'SELECT * FROM sources ORDER BY id',
                    'reports': "SELECT data FROM records WHERE kind='report' ORDER BY id",
                    'corrections': "SELECT data FROM records WHERE kind='correction' ORDER BY id",
                    'source_snapshots': 'SELECT hash,data FROM source_snapshots ORDER BY hash',
                    'trash': "SELECT kind,id,deleted_at FROM trash UNION ALL SELECT 'correction',id,deleted_at FROM correction_trash ORDER BY kind,id",
                    'match_reviews': 'SELECT * FROM match_reviews ORDER BY report_id,match_id',
                    'dictionary_words': 'SELECT * FROM dictionary_words ORDER BY word',
                }
                for name, query in queries.items():
                    with archive.open(name + '.jsonl', 'w') as entry:
                        for row in db.execute(query):
                            item = dict(row)
                            if name in {'reports','corrections'}:
                                item = json.loads(item['data'])
                            elif name == 'sources':
                                item['provenance'] = json.loads(item['provenance'])
                            elif name == 'source_snapshots':
                                item['data'] = json.loads(item['data'])
                            entry.write((json.dumps(item, ensure_ascii=False) + '\n').encode('utf-8'))
            stream.seek(0)
            return DataArchive(stream)
        except BaseException:
            stream.close()
            raise
