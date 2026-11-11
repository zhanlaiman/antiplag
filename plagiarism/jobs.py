"""Bounded background work for long local retrieval runs."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import logging
import sqlite3
from threading import Lock
from uuid import uuid4


class JobCancelled(RuntimeError):
    pass


class Jobs:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="istok-retrieval")
        self.lock = Lock()
        self.items = {}

    def _trim(self):
        while len(self.items) > 100:
            finished = next((identifier for identifier, item in self.items.items()
                             if item["status"] in {"complete", "failed", "cancelled"}
                             and not item.get("_persist_pending")), None)
            if not finished:
                break
            del self.items[finished]

    def submit(self, key, operation, kind="retrieval"):
        with self.lock:
            active = [item for item in self.items.values() if item["status"] in {"queued", "running", "cancelling"}]
            for item in active:
                if item["key"] == key:
                    return self.public(item)
            if len(active) >= 3:
                raise ValueError("Уже запущены три проверки. Дождитесь завершения одной из них.")
            job = {"id": uuid4().hex, "key": key, "kind": kind, "status": "queued", "cancel_requested": False,
                   "phase": "queued", "stage": "Ожидаем свободную очередь", "progress": {"current": 0, "total": 0}}
            self.items[job["id"]] = job
            self._trim()
            self.executor.submit(self.run, job["id"], operation)
            return self.public(job)

    def submit_model(self, request_id, path, body, operation, store, candidate_attempt=None):
        fingerprint = sha256(json.dumps({"path": path, "body": body}, ensure_ascii=False,
                                       sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        with self.lock:
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
                return self.public(saved) if saved else self._recover(request_id, reservation, store)
            job = {"id": request_id, "key": ("model", request_id), "kind": "model", "path": path,
                   "request_fingerprint": fingerprint, "status": "queued", "phase": "queued",
                   "cancel_requested": False, "stage": "Ожидаем свободную очередь",
                   "progress": {"current": 0, "total": 1}, "_store": store, "_persist_pending": True}
            if candidate_attempt:
                job["candidate_attempt"] = dict(candidate_attempt)
            self.items[request_id] = job
            self._trim()
            self.executor.submit(self.run, request_id, operation)
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
        except Exception as error:
            logging.exception("Background job failed")
            with self.lock:
                self.items[job_id].update(status="failed", phase="complete", stage="Проверка остановлена", error=str(error),
                                          code=getattr(error, "code", "invalid_request" if isinstance(error, ValueError)
                                                       else "record_not_found" if isinstance(error, KeyError) else "model_error"),
                                          details=getattr(error, "details", None))
        finally:
            with self.lock:
                if "_store" in job:
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
            return self.public(job)

    def close(self):
        with self.lock:
            for job in self.items.values():
                job["cancel_requested"] = True
        self.executor.shutdown(wait=True, cancel_futures=True)
