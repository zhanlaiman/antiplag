"""Bounded background work for long local retrieval runs."""

from concurrent.futures import ThreadPoolExecutor, wait as wait_futures
from hashlib import sha256
import json
import logging
import sqlite3
from threading import Lock, RLock
from uuid import uuid4


class JobCancelled(RuntimeError):
    pass


class _Permit:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.lock = Lock()
        self.released = False

    def release(self):
        with self.lock:
            if not self.released:
                with self.coordinator.lock:
                    self.coordinator.active -= 1
                self.released = True


class JobCoordinator:
    """One shared worker with a bound on running and queued work together."""

    def __init__(self, max_active=6, executor=None):
        if type(max_active) is not int or max_active < 1:
            raise ValueError("Некорректный размер общей очереди.")
        self.executor = executor if executor is not None else ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="istok-retrieval")
        self.owns_executor = executor is None
        self.max_active = max_active
        self.active = 0
        self.closed = False
        self.lock = Lock()

    def reserve(self):
        with self.lock:
            if self.closed:
                raise ValueError("Очередь проверок закрыта.")
            if self.active >= self.max_active:
                raise ValueError("Общая очередь проверок заполнена. Попробуйте после завершения текущих заданий.")
            self.active += 1
        return _Permit(self)

    def close(self, wait=True):
        with self.lock:
            self.closed = True
        if self.owns_executor:
            self.executor.shutdown(wait=wait, cancel_futures=True)


class Jobs:
    def __init__(self, coordinator=None, executor=None):
        if coordinator is not None and executor is not None:
            raise ValueError("Передайте общую очередь либо отдельный исполнитель.")
        self.coordinator = coordinator if coordinator is not None else JobCoordinator(executor=executor)
        self.owns_coordinator = coordinator is None
        self.executor = self.coordinator.executor
        self.lock = RLock()
        self.items = {}
        self.closed = False

    def is_idle(self):
        with self.lock:
            return not any(item["status"] in {"queued", "running", "cancelling"}
                           or item.get("_persist_pending")
                           or item.get("_permit") is not None and not item["_permit"].released
                           or item.get("_future") is not None and not item["_future"].done()
                           for item in self.items.values())

    def _schedule(self, job, operation, permit):
        job["_permit"] = permit
        try:
            future = self.executor.submit(self.run, job["id"], operation)
        except BaseException:
            job.update(status="failed", phase="complete", stage="Не удалось поставить проверку в очередь",
                       error="Очередь проверок недоступна.", code="job_submission_failed", details=None)
            self._persist(job)
            raise
        job["_future"] = future

        def finished(completed):
            try:
                if completed.cancelled():
                    with self.lock:
                        job.update(status="cancelled", phase="complete", stage="Проверка отменена")
                        self._persist(job)
            finally:
                permit.release()

        future.add_done_callback(finished)

    def _persist(self, job):
        if not job.get("_persist_pending"):
            return
        try:
            self._candidate_failure(job, job["_store"])
            job["_store"].save("model-job", {**self.public(job),
                                            "request_fingerprint": job["request_fingerprint"]})
        except Exception:
            logging.exception("Could not persist model job outcome")
            job.update(status="failed", error="Результат проверки не удалось сохранить. Повтор автоматически не запускается.",
                       code="job_storage_error", details=None)
        finally:
            job["_persist_pending"] = False
            self._trim()

    def _trim(self):
        while len(self.items) > 100:
            finished = next((identifier for identifier, item in self.items.items()
                             if item["status"] in {"complete", "failed", "cancelled"}
                             and not item.get("_persist_pending")
                             and (item.get("_permit") is None or item["_permit"].released)), None)
            if not finished:
                break
            del self.items[finished]

    def submit(self, key, operation, kind="retrieval"):
        with self.lock:
            if self.closed:
                raise ValueError("Очередь проверок закрыта.")
            active = [item for item in self.items.values() if item["status"] in {"queued", "running", "cancelling"}]
            for item in active:
                if item["key"] == key:
                    return self.public(item)
            if len(active) >= 3:
                raise ValueError("Уже запущены три проверки. Дождитесь завершения одной из них.")
            permit = self.coordinator.reserve()
            try:
                job = {"id": uuid4().hex, "key": key, "kind": kind, "status": "queued", "cancel_requested": False,
                       "phase": "queued", "stage": "Ожидаем свободную очередь", "progress": {"current": 0, "total": 0}}
                self.items[job["id"]] = job
                self._trim()
                self._schedule(job, operation, permit)
            except BaseException:
                permit.release()
                raise
            return self.public(job)

    def submit_model(self, request_id, path, body, operation, store, candidate_attempt=None):
        fingerprint = sha256(json.dumps({"path": path, "body": body}, ensure_ascii=False,
                                       sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        with self.lock:
            if self.closed:
                raise ValueError("Очередь проверок закрыта.")
            saved = self.items.get(request_id) or store.get("model-job", request_id)
            reservation = store.get("model-request", "model-request:" + request_id)
            identity = saved or reservation
            if identity:
                if identity.get("request_fingerprint") != fingerprint:
                    raise ValueError("Этот идентификатор уже использован для другого запроса.")
                return self.public(saved) if saved else self._recover(request_id, reservation, store)
            active = sum(item["status"] in {"queued", "running", "cancelling"} for item in self.items.values())
            if active >= 3:
                raise ValueError("Уже запущены три проверки. Дождитесь завершения одной из них.")
            permit = self.coordinator.reserve()
            try:
                reservation = {"id": "model-request:" + request_id, "path": path, "request_fingerprint": fingerprint}
                if candidate_attempt:
                    reservation["candidate_attempt"] = dict(candidate_attempt)
                try:
                    store.save("model-request", reservation)
                except sqlite3.IntegrityError:
                    reservation = store.get("model-request", reservation["id"])
                    if not reservation or reservation.get("request_fingerprint") != fingerprint:
                        raise ValueError("Этот идентификатор уже использован для другого запроса.") from None
                    saved = store.get("model-job", request_id)
                    permit.release()
                    return self.public(saved) if saved else self._recover(request_id, reservation, store)
                job = {"id": request_id, "key": ("model", request_id), "kind": "model", "path": path,
                       "request_fingerprint": fingerprint, "status": "queued", "phase": "queued",
                       "cancel_requested": False, "stage": "Ожидаем свободную очередь",
                       "progress": {"current": 0, "total": 1}, "_store": store, "_persist_pending": True}
                if candidate_attempt:
                    job["candidate_attempt"] = dict(candidate_attempt)
                self.items[request_id] = job
                self._trim()
                self._schedule(job, operation, permit)
            except BaseException:
                permit.release()
                raise
            return self.public(job)

    def _recover(self, request_id, reservation, store):
        # Never repeat an uncertain, potentially charged remote call after a restart.
        recovered = {"id": request_id, "kind": "model", "path": reservation["path"],
                     "request_fingerprint": reservation["request_fingerprint"],
                     "status": "failed", "phase": "complete", "stage": "Проверка прервана при перезапуске",
                     "error": "Приложение перезапустилось до сохранения результата. Автоматический повтор отключён.",
                     "code": "job_interrupted", "details": None, "progress": {"current": 0, "total": 1}}
        if reservation.get("candidate_attempt"):
            recovered["candidate_attempt"] = reservation["candidate_attempt"]
        try:
            self._candidate_failure(recovered, store)
            recovered = store.save("model-job", recovered)
        except sqlite3.IntegrityError:
            recovered = store.get("model-job", request_id)
        return self.public(recovered)

    @staticmethod
    def _candidate_failure(job, store):
        attempt = job.get("candidate_attempt")
        if attempt and job["status"] in {"failed", "cancelled"}:
            store.save("candidate-error", {**attempt, "job_id": job["id"],
                       "error": job.get("error", "Повторная проверка отменена."),
                       "code": job.get("code", "job_cancelled"), "details": job.get("details")})

    def run(self, job_id, operation):
        def progress(stage, current, total, phase=None):
            with self.lock:
                if self.items[job_id]["cancel_requested"]:
                    raise JobCancelled("Проверка отменена.")
                self.items[job_id].update(stage=stage, progress={"current": current, "total": total})
                if phase:
                    self.items[job_id]["phase"] = phase

        with self.lock:
            job = self.items[job_id]
            job.update(status="running", phase="starting", stage="Подготавливаем библиотеку")
        try:
            progress("Подготавливаем проверку", 0, 0)
            result = operation(progress)
            with self.lock:
                if self.items[job_id]["cancel_requested"]:
                    self.items[job_id].update(status="cancelled", phase="complete", stage="Проверка отменена")
                else:
                    self.items[job_id].update(status="complete", phase="complete", stage="Проверка завершена", result=result)
        except JobCancelled:
            with self.lock:
                self.items[job_id].update(status="cancelled", phase="complete", stage="Проверка отменена")
        except BaseException as error:
            logging.exception("Background job failed")
            with self.lock:
                self.items[job_id].update(status="failed", phase="complete", stage="Проверка остановлена", error=str(error),
                                          code=getattr(error, "code", "invalid_request" if isinstance(error, ValueError)
                                                       else "record_not_found" if isinstance(error, KeyError) else "model_error"),
                                          details=getattr(error, "details", None))
        finally:
            with self.lock:
                self._persist(job)

    @staticmethod
    def public(job):
        return {key: value for key, value in job.items()
                if key not in {"key", "cancel_requested", "request_fingerprint"} and not key.startswith("_")}

    def get(self, job_id, store=None):
        with self.lock:
            if job_id not in self.items:
                if store:
                    saved = store.get("model-job", job_id)
                    if saved:
                        return self.public(saved)
                    reservation = store.get("model-request", "model-request:" + job_id)
                    if reservation:
                        return self._recover(job_id, reservation, store)
                raise KeyError(job_id)
            return self.public(self.items[job_id])

    def cancel(self, job_id):
        with self.lock:
            if job_id not in self.items:
                raise KeyError(job_id)
            job = self.items[job_id]
            if job["status"] in {"queued", "running", "cancelling"}:
                job.update(cancel_requested=True, status="cancelling", stage="Останавливаем после текущего шага")
                if "_future" in job:
                    job["_future"].cancel()
            return self.public(job)

    def close(self, wait=True):
        with self.lock:
            self.closed = True
            for job in self.items.values():
                job["cancel_requested"] = True
            futures = [item["_future"] for item in self.items.values() if "_future" in item]
        for future in futures:
            future.cancel()
        if self.owns_coordinator:
            self.coordinator.close(wait=wait)
        if wait:
            wait_futures([future for future in futures if not future.cancelled()])
