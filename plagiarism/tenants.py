"""Separate account stores and caches, sharing only a bounded compute queue."""

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
from threading import Lock

from plagiarism import source_packs
from plagiarism.jobs import JobCoordinator, Jobs
from plagiarism.storage import Store


ROOT = Path(__file__).resolve().parent.parent
TENANT_DIRECTORY = ROOT / "data" / "tenants"
PUBLIC_PACK_IDS = ("biology-ru-v1", "economics-ru-v1")


@dataclass(frozen=True)
class TenantContext:
    store: Store
    jobs: Jobs
    embedding_cache_path: Path


@dataclass
class _Entry:
    context: TenantContext
    leases: int = 0


class TenantRegistry:
    """Lease a context per HTTP request; evict only unused, finished contexts."""

    def __init__(self, root=TENANT_DIRECTORY, max_contexts=32, max_active=6):
        if type(max_contexts) is not int or max_contexts < 1:
            raise ValueError("Некорректное число активных пользовательских контекстов.")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_contexts = max_contexts
        self.coordinator = JobCoordinator(max_active=max_active)
        self.lock = Lock()
        self.entries = OrderedDict()
        self.closed = False

    def _create(self, user_id):
        directory = self.root / user_id
        if directory.is_symlink():
            raise ValueError("Каталог пользователя не может быть символической ссылкой.")
        directory.mkdir(mode=0o700, exist_ok=True)
        database = directory / "istok.sqlite3"
        cache_path = directory / "embeddings.sqlite3"
        if database.is_symlink() or cache_path.is_symlink():
            raise ValueError("Файлы пользователя не могут быть символическими ссылками.")
        store = Store(database)
        if not store.get("tenant-bootstrap", "public-source-packs-v1"):
            # Only the two bundled, validated public collections are initial data.
            # The legacy application database and local uploads are never opened.
            for pack_id in PUBLIC_PACK_IDS:
                store.install_sources(source_packs.load_pack(pack_id)["sources"])
            store.save("tenant-bootstrap", {"id": "public-source-packs-v1", "packs": list(PUBLIC_PACK_IDS)})
        return TenantContext(store=store, jobs=Jobs(coordinator=self.coordinator),
                             embedding_cache_path=cache_path)

    @contextmanager
    def context(self, user_id):
        if not isinstance(user_id, str) or not re.fullmatch(r"[a-f0-9]{64}", user_id):
            raise ValueError("Некорректный идентификатор пользователя.")
        with self.lock:
            if self.closed:
                raise ValueError("Хранилище пользователей закрыто.")
            entry = self.entries.get(user_id)
            if entry is None:
                if len(self.entries) >= self.max_contexts:
                    idle = next((key for key, value in self.entries.items()
                                 if value.leases == 0 and value.context.jobs.is_idle()), None)
                    if idle is None:
                        raise ValueError("Все пользовательские контексты заняты. Дождитесь завершения проверки.")
                    self.entries.pop(idle).context.jobs.close()
                entry = _Entry(self._create(user_id))
                self.entries[user_id] = entry
            self.entries.move_to_end(user_id)
            entry.leases += 1
        try:
            yield entry.context
        finally:
            with self.lock:
                entry.leases -= 1

    def close(self):
        with self.lock:
            self.closed = True
            contexts = [entry.context for entry in self.entries.values()]
        try:
            # Stop the shared worker from starting another account's queued work.
            self.coordinator.close(wait=False)
            for context in contexts:
                context.jobs.close(wait=False)
        finally:
            self.coordinator.close()
