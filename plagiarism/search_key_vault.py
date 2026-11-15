"""Private immutable search credentials and fail-closed volume persistence.

This module never reads environment credentials. A pending journal always wins
over the candidate state, including after restart. ``persist`` is a synchronous
durability barrier, not an activation callback or distributed transaction.
"""

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
from threading import RLock

from plagiarism.access import AccessError


MAX_VAULT_BYTES = 2_000_000
MAX_CREDENTIALS = 128
MAX_AUDIT_EVENTS = 10_000


def vault_error(code="pool_storage_unavailable"):
    message = ("Сохранение пула не завершено. Требуется восстановление владельцем сервера."
               if code == "pool_recovery_required" else
               "Хранилище ключей недоступно. Изменение не подтверждено.")
    return AccessError(message, code, 503)


class KeyVault:
    def __init__(self, root, persist):
        self.root = Path(root) / "search-pool-vault"
        self.persist = persist
        self.lock = RLock()
        try:
            if Path(root).is_symlink():
                raise OSError("Unsafe root")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = self.root.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
                    or info.st_uid != os.geteuid()):
                raise OSError("Unsafe directory")
        except OSError:
            raise vault_error() from None

    @staticmethod
    def empty():
        return {"version": 1, "revision": 0, "salt": secrets.token_hex(32),
                "records": {}, "active": {}, "audit": []}

    @staticmethod
    def _schema(value):
        if (not isinstance(value, dict) or value.get("version") != 1
                or type(value.get("revision")) is not int or value["revision"] < 0
                or not isinstance(value.get("salt"), str) or len(value["salt"]) != 64
                or not isinstance(value.get("records"), dict)
                or len(value["records"]) > MAX_CREDENTIALS
                or not isinstance(value.get("active"), dict)
                or not isinstance(value.get("audit"), list)
                or len(value["audit"]) > MAX_AUDIT_EVENTS):
            raise vault_error()
        try:
            bytes.fromhex(value["salt"])
        except ValueError:
            raise vault_error() from None
        return value

    def _read(self, name):
        descriptor = None
        try:
            descriptor = os.open(self.root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or info.st_size > MAX_VAULT_BYTES):
                raise OSError("Unsafe file")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                raw = stream.read(MAX_VAULT_BYTES + 1)
            if len(raw) > MAX_VAULT_BYTES:
                raise OSError("Oversize file")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("Invalid object")
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeError):
            raise vault_error() from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _sync_directory(self):
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write(self, name, value):
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
        if len(raw) > MAX_VAULT_BYTES:
            raise vault_error("pool_storage_capacity")
        temporary = self.root / (".pending-" + secrets.token_hex(16))
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.root / name)
            self._sync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def locked(self):
        with self.lock:
            descriptor = None
            try:
                descriptor = os.open(self.root / "vault.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                        or info.st_uid != os.geteuid() or info.st_nlink != 1):
                    raise OSError("Unsafe lock")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                intent = self._read("pending.json")
                if intent is not None:
                    if not isinstance(intent, dict) or set(intent) != {"previous"}:
                        raise vault_error()
                    value = self._schema(intent["previous"])
                else:
                    saved = self._read("vault.json")
                    value = self._schema(self.empty() if saved is None else saved)
                yield value, intent is not None
            except OSError:
                raise vault_error() from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def commit(self, previous, value):
        """Caller holds locked(). A failed barrier retains the rollback intent."""
        if len(value["audit"]) > MAX_AUDIT_EVENTS:
            raise vault_error("pool_storage_capacity")
        intent = {"previous": previous}
        try:
            self._write("pending.json", intent)
            self.persist()
            self._write("vault.json", value)
            self.persist()
            (self.root / "pending.json").unlink()
            self._sync_directory()
            self.persist()
        except Exception:
            # Restore the journal even when the final barrier fails after its
            # removal. Never publish an uncertain active candidate in-process.
            # If the disk itself refuses this write, subsequent reads fail too.
            try:
                self._write("pending.json", intent)
            except Exception:
                self.failed_closed = True
            raise vault_error("pool_recovery_required") from None

    def read(self):
        if getattr(self, "failed_closed", False):
            raise vault_error()
        with self.locked() as (value, pending):
            return deepcopy(value), pending

    def mutate(self, operation):
        if getattr(self, "failed_closed", False):
            raise vault_error()
        with self.locked() as (before, pending):
            if pending:
                raise vault_error("pool_recovery_required")
            after = deepcopy(before)
            result = operation(after)
            after["revision"] += 1
            self.commit(before, after)
            return result

    def recover(self):
        """Internal operator recovery; there is deliberately no HTTP endpoint."""
        with self.locked() as (previous, pending):
            if not pending:
                return
            try:
                self._write("vault.json", previous)
                self.persist()
                (self.root / "pending.json").unlink()
                self._sync_directory()
                self.persist()
            except Exception:
                try:
                    self._write("pending.json", {"previous": previous})
                except Exception:
                    self.failed_closed = True
                raise vault_error("pool_recovery_required") from None
            self.failed_closed = False
