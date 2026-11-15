"""Bounded optional report receipts; never changes the native Jobs outcome."""
from collections import deque
from contextlib import contextmanager
from hashlib import sha256
import json
import logging
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
import time

from plagiarism import report_reuse as reuse


class ReportReuseRuntime:
    def __init__(self, root, secret, persist, *, clock=time.monotonic):
        self.root, self.secret, self.persist, self.clock = root, secret, persist, clock
        self.lock = Lock(); self.closed = False
        self.buckets, self.active, self.recent = {}, set(), deque()
        self.queue = Queue(maxsize=8); self.stop = Event()
        self.worker = Thread(target=self._publish, name='istok-reuse-publisher', daemon=True)
        self.worker.start()

    @contextmanager
    def admission(self, tenant):
        if not self.lock.acquire(blocking=False): raise reuse.unavailable()
        admitted = False
        try:
            now = self.clock()
            while self.recent and self.recent[0] <= now - 60: self.recent.popleft()
            for identifier, (_, _, seen) in list(self.buckets.items()):
                if seen <= now - 120 and identifier not in self.active: self.buckets.pop(identifier)
            if (self.closed or len(self.recent) >= 60 or len(self.active) >= 4 or tenant in self.active
                    or tenant not in self.buckets and len(self.buckets) >= 256):
                raise reuse.unavailable()
            tokens, last, _ = self.buckets.get(tenant, (3., now, now))
            tokens = min(3., tokens + max(0., now - last) / 10)
            if tokens < 1: raise reuse.unavailable()
            self.buckets[tenant] = (tokens - 1, now, now)
            self.recent.append(now); self.active.add(tenant); admitted = True
        finally: self.lock.release()
        try: yield
        finally:
            if admitted:
                with self.lock: self.active.discard(tenant)

    @staticmethod
    def capture(tenant, kind, record):
        """Capture exact Store.save serialization BEFORE original persist ACK1."""
        try:
            if (kind != 'model-job' or record.get('status') != 'complete'
                    or record.get('path') != '/api/web-check'): return None
            raw = json.dumps(record, ensure_ascii=False).encode('utf-8')
            if len(raw) > reuse.BYTE_LIMIT: return None
            capture = (tenant, record['id'], record['request_fingerprint'],
                       record['result']['report']['id'], sha256(raw).hexdigest())
            if not all(reuse._hex(value, size) for value, size in zip(capture, (64, 32, 64, 32, 64))):
                return None
            return capture
        except (KeyError, TypeError, ValueError, UnicodeError):
            return None

    def after_ack(self, capture, jobs, raw_save):
        """Invoked only after the original terminal persist returned successfully."""
        if capture is None or self.closed: return
        holder = {}
        try:
            job = jobs.items.get(capture[1])
            if job is None or job.get('request_fingerprint') != capture[2]: return
            future = job.get('_future')
            if future is None: return
            holder.update(job=job, jobs=jobs)

            def settled(completed):
                try:
                    if self.closed or completed.cancelled() or completed.exception(timeout=0) is not None: return
                    current, owner = holder['job'], holder['jobs']
                    if not owner.lock.acquire(blocking=False): return
                    try:
                        ready = (current.get('status') == 'complete' and not current.get('_persist_pending')
                                 and current.get('request_fingerprint') == capture[2])
                    finally: owner.lock.release()
                    if ready:
                        with self.lock:
                            if not self.closed:
                                try: self.queue.put_nowait((capture, raw_save))
                                except Full: pass
                except Exception:
                    logging.warning('Historical receipt callback unavailable')
                finally: holder.clear()

            future.add_done_callback(settled)
        except Exception:
            holder.clear()
            logging.warning('Historical receipt observation unavailable')

    def _publish(self):
        while not self.stop.is_set():
            try: capture, raw_save = self.queue.get(timeout=.1)
            except Empty: continue
            try:
                if self.closed: continue
                receipt = reuse.make_receipt(self.root, self.secret, capture)
                if receipt is not None:
                    raw_save(reuse.RECEIPT_KIND, receipt)
                    self.persist()  # ACK2 stores evidence of ACK1, never claims its own ACK.
            except Exception:
                logging.warning('Historical receipt publication unavailable')
            finally: self.queue.task_done()

    def wait_idle(self, timeout=3):
        until = time.monotonic() + timeout
        with self.queue.all_tasks_done:
            while self.queue.unfinished_tasks:
                remaining = until - time.monotonic()
                if remaining <= 0: return False
                self.queue.all_tasks_done.wait(remaining)
        return True

    def close(self, wait=True):
        with self.lock: self.closed = True
        self.stop.set()
        while True:
            try: self.queue.get_nowait(); self.queue.task_done()
            except Empty: break
        if wait: self.worker.join(timeout=1)
