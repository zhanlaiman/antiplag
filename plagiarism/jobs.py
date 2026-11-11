"""Bounded background work for long local retrieval runs."""

from concurrent.futures import ThreadPoolExecutor
import logging
from threading import Lock
from uuid import uuid4


class JobCancelled(RuntimeError):
    pass


class Jobs:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="istok-retrieval")
        self.lock = Lock()
        self.items = {}

    def submit(self, key, operation, kind="retrieval"):
        with self.lock:
            active = [item for item in self.items.values() if item["status"] in {"queued", "running", "cancelling"}]
            for item in active:
                if item["key"] == key:
                    return self.public(item)
            if len(active) >= 3:
                raise ValueError("Уже запущены три проверки. Дождитесь завершения одной из них.")
            job = {"id": uuid4().hex, "key": key, "kind": kind, "status": "queued", "cancel_requested": False,
                   "stage": "Ожидаем свободную очередь", "progress": {"current": 0, "total": 0}}
            self.items[job["id"]] = job
            if len(self.items) > 100:
                finished = next((identifier for identifier, item in self.items.items()
                                 if item["status"] in {"complete", "failed", "cancelled"}), None)
                if finished:
                    del self.items[finished]
            self.executor.submit(self.run, job["id"], operation)
            return self.public(job)

    def run(self, job_id, operation):
        def progress(stage, current, total):
            with self.lock:
                if self.items[job_id]["cancel_requested"]:
                    raise JobCancelled("Проверка отменена.")
                self.items[job_id].update(stage=stage, progress={"current": current, "total": total})

        with self.lock:
            self.items[job_id].update(status="running", stage="Подготавливаем библиотеку")
        try:
            progress("Подготавливаем проверку", 0, 0)
            result = operation(progress)
            with self.lock:
                if self.items[job_id]["cancel_requested"]:
                    self.items[job_id].update(status="cancelled", stage="Проверка отменена")
                else:
                    self.items[job_id].update(status="complete", stage="Проверка завершена", result=result)
        except JobCancelled:
            with self.lock:
                self.items[job_id].update(status="cancelled", stage="Проверка отменена")
        except Exception as error:
            logging.exception("Retrieval job failed")
            with self.lock:
                self.items[job_id].update(status="failed", stage="Проверка остановлена", error=str(error))

    @staticmethod
    def public(job):
        return {key: value for key, value in job.items() if key not in {"key", "cancel_requested"}}

    def get(self, job_id):
        with self.lock:
            if job_id not in self.items:
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
