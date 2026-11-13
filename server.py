#!/usr/bin/env python3
"""Run the local web application with Python 3.11+ and no dependencies."""

import argparse
from contextlib import ExitStack
from hashlib import sha256
import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import urlsplit

from plagiarism import access, auth_provider, demo, evaluation, learning, llm, matching, report_export, retrieval, saas, search_credentials, source_packs, web_search
from plagiarism.jobs import Jobs
from plagiarism.matching import analyze, MAX_TEXT_CHARS
from plagiarism.storage import Store


ROOT = Path(__file__).resolve().parent
MODEL_LOCK = Lock()
MAX_BODY = 2_000_000
RELATIONS = {"exact", "paraphrase", "same_topic", "unrelated"}
MODEL_ROUTES = {"/api/compare", "/api/judge", "/api/rewrite", "/api/evaluate",
                "/api/candidate-judge", "/api/candidate-rewrite"}


def field(body, key, maximum=MAX_TEXT_CHARS, required=True):
    value = body.get(key, "")
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"Некорректное значение поля «{key}».")
    if required and not value.strip():
        raise ValueError(f"Заполните поле «{key}».")
    return value


def public_report(report, reviews=None):
    result = {key: value for key, value in report.items() if key != "sources"} | {"reviews": reviews or {}}
    if report.get("scope") == "web_search":
        result["source_snapshots_count"] = sum(
            1 for source in report.get("sources", [])
            if isinstance(source, dict) and not source.get("representation_of")
            and isinstance(source.get("text"), str) and source["text"].strip())
    return result


def judge_candidate(store, record, candidate, model):
    try:
        with MODEL_LOCK:
            judgment = llm.judge(candidate["query_excerpt"], candidate["source_excerpt"], model)
    except (llm.OllamaError, ValueError) as error:
        store.save("candidate-error", {"retrieval_id": record["id"], "candidate_id": candidate["id"],
                                       "error": str(error), "model": model, "details": getattr(error, "details", None),
                                       "code": getattr(error, "code", "model_error")})
        raise
    saved = store.save("candidate-judgment", {"retrieval_id": record["id"], "candidate_id": candidate["id"], "judgment": judgment})
    return {**judgment, "id": saved["id"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "Istok/0.1"

    def log_message(self, message, *arguments):
        logging.info(message, *arguments)

    def send_json(self, payload, status=200):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in getattr(self, "response_headers", []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(content)

    def trusted_request(self):
        runtime = getattr(self.server, "saas", None)
        if runtime:
            return runtime.trusted(self.headers, self.command)
        # Protect local data against cross-site requests and DNS rebinding.
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in allowed:
            return False
        origin = self.headers.get("Origin")
        return not origin or origin in {f"http://{item}" for item in allowed}

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        self.response_headers = []
        self.tenant = None
        self.identity = None
        with ExitStack() as leases:
            self._dispatch(method, leases)

    def _dispatch(self, method, leases):
        try:
            if not self.trusted_request():
                self.send_json({"error": "Запрос разрешён только из локального приложения."}, 403)
                return
            path = urlsplit(self.path).path
            runtime = getattr(self.server, "saas", None)
            if runtime and (path.startswith("/api/") or path.startswith("/reports/")):
                self.identity, new_device = runtime.identity(self.headers, create_device=method == "GET" and path == "/api/session")
                if new_device:
                    self.response_headers.append(("Set-Cookie", runtime.cookie(saas.DEVICE_COOKIE, self.identity.device)))
                if method not in {"GET", "HEAD"}:
                    runtime.csrf_guard(self.identity, self.headers)
                if method == "GET" and path == "/api/session":
                    self.send_json(runtime.session_payload(self.identity))
                    return
                if method == "POST" and path in {"/api/auth/request", "/api/auth/verify", "/api/auth/logout"}:
                    payload, cookie = runtime.auth(path, self.read_body(), self.identity, self.client_address[0])
                    if cookie:
                        self.response_headers.append(("Set-Cookie", cookie))
                    self.send_json(payload)
                    return
                user = runtime.require_user(self.identity)
                self.tenant = leases.enter_context(runtime.tenants.context(user["id"]))
            if method == "GET" and re.fullmatch(r"/reports/[a-f0-9]{32}\.html", path):
                report_id = path.rsplit("/", 1)[1][:-5]
                document = self.route("GET", "/api/report-exports/" + report_id, {})
                content = document["html"].encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(content)
                return
            if not path.startswith("/api/"):
                if method != "GET":
                    self.send_json({"error": "Метод недоступен."}, 405)
                    return
                self.static(path)
                return
            body = self.read_body() if method == "POST" else {}
            result = self.route(method, path, body)
            self.send_json(result)
        except (access.AccessError, auth_provider.AccessError) as error:
            payload = {"error": str(error), "code": error.code}
            if getattr(error, "details", None) is not None:
                payload["details"] = error.details
            if getattr(error, "retry_after", None) is not None:
                payload["retry_after"] = error.retry_after
                self.response_headers.append(("Retry-After", str(error.retry_after)))
            self.send_json(payload, error.status)
        except learning.LearningError as error:
            self.send_json({"error": str(error), "code": error.code}, 429 if error.code == "feedback_daily_limit" else 400)
        except ValueError as error:
            self.send_json({"error": str(error)}, 400)
        except KeyError:
            self.send_json({"error": "Запись не найдена. Обновите страницу."}, 404)
        except llm.OllamaError as error:
            self.send_json({"error": str(error), "code": getattr(error, "code", "model_error"),
                            "details": getattr(error, "details", None)}, 503)
        except (BrokenPipeError, ConnectionResetError):
            logging.info("Client disconnected")
        except Exception:
            logging.exception("Request failed")
            self.send_json({"error": "Не удалось выполнить запрос. Подробности сохранены в журнале сервера."}, 500)

    def read_body(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Ожидается запрос JSON.")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_BODY:
            raise ValueError("Запрос пустой или превышает 2 МБ.")
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Не удалось прочитать JSON.") from error
        if not isinstance(body, dict):
            raise ValueError("Ожидается объект JSON.")
        return body

    def static(self, path):
        if path in {"/setup-search.html", "/setup-search.js"} and getattr(self.server, "saas", None):
            raise access.AccessError("Настройка ключа доступна только в локальном режиме владельца.", "operation_forbidden", 403)
        files = {"/": "index.html", "/index.html": "index.html", "/style.css": "style.css", "/app.js": "app.js",
                 "/motion.js": "motion.js", "/motion.css": "motion.css",
                 "/setup-search.html": "setup-search.html", "/setup-search.js": "setup-search.js"}
        files["/privacy.html"] = "privacy.html"
        files["/owner.html"] = "owner.html"
        files["/owner.js"] = "owner.js"
        files["/fonts/InterVariable.woff2"] = "fonts/InterVariable.woff2"
        files["/fonts/Inter-LICENSE.txt"] = "fonts/Inter-LICENSE.txt"
        files["/fonts/PTSerif-Regular.ttf"] = "fonts/PTSerif-Regular.ttf"
        files["/fonts/PTSerif-OFL.txt"] = "fonts/PTSerif-OFL.txt"
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path not in files:
            raise KeyError(path)
        file = ROOT / "web" / files[path]
        if not file.is_file():
            raise KeyError(path)
        content = file.read_bytes()
        self.send_response(200)
        content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def route(self, method, path, body):
        if method == "GET" and path == "/api/session":
            return {"mode": "local", "web": web_search.status()}
        runtime = getattr(self.server, "saas", None)
        if path == "/api/local-search-config":
            if runtime:
                raise access.AccessError("Настройка ключа доступна только на Mac владельца.", "operation_forbidden", 403)
            if method == "POST":
                return search_credentials.save_local(body.get("provider"), body.get("api_key"))
            if method == "GET":
                return {"web": web_search.status()}
            raise KeyError(path)
        store = self.tenant.store if getattr(self, "tenant", None) else self.server.store
        jobs = self.tenant.jobs if getattr(self, "tenant", None) else getattr(self.server, "jobs", None)
        if runtime:
            if (path in {"/api/examples", "/api/evaluations", "/api/reviews", "/api/export", "/api/evaluate", "/api/demo", "/api/source-packs/install"}
                    or method == "POST" and path in MODEL_ROUTES):
                raise access.AccessError("Эта служебная операция недоступна в публичном приложении.", "operation_forbidden", 403)
        if path == "/api/learning-feedback" and method == "POST" or path.startswith("/api/learning-feedback/") and method == "DELETE":
            if not runtime or not runtime.learning:
                raise access.AccessError("Обратная связь доступна только в настроенном аккаунтном режиме.", "learning_not_configured", 403)
            account_id = self.identity.user["id"]
            if method == "DELETE":
                return {"feedback": runtime.learning.revoke(account_id, path.rsplit("/", 1)[1])}
            if body.get("consent") is not True or body.get("consent_version") != "2026-11-11-v1":
                raise learning.LearningError("Подтвердите отдельное согласие текущей версии на передачу этой пары.", "consent_required")
            report = store.get("report", field(body, "report_id", 100))
            if not report:
                raise KeyError("report")
            pair_id = field(body, "pair_id", 100)
            judgment = None
            if body.get("pair_type") == "match":
                pair = next((item for item in report["matches"] if item["id"] == pair_id), None)
                if pair is None:
                    raise KeyError("match")
                text, source_text = pair["excerpt"], pair["source_excerpt"]
                outcomes = [item for item in store.records("judgment", limit=1000)
                            if item.get("report_id") == report["id"] and item.get("match_id") == pair_id]
                judgment = outcomes[0]["judgment"] if outcomes else None
            elif body.get("pair_type") == "candidate":
                record, pair = self.candidate_pair(store, {**body, "candidate_id": pair_id})
                if record["report_id"] != report["id"]:
                    raise KeyError("retrieval")
                text, source_text = pair["query_excerpt"], pair["source_excerpt"]
                latest = next((item for item in store.candidate_judgments(record["id"])
                               if item["candidate_id"] == pair_id), None)
                judgment = latest.get("judgment") if latest else None
            else:
                raise ValueError("Выберите совпадение или найденную пару.")
            source = next((item for item in report["sources"] if item["id"] == pair["source_id"]), None)
            if source is None:
                raise KeyError("source")
            version = ({"model": judgment.get("model", "unknown"), "judge_version": judgment.get("judge_version", "legacy-unversioned")}
                       if judgment else {"model": "lexical", "judge_version": "lexical-" + sha256(Path(matching.__file__).read_bytes()).hexdigest()})
            payload = {"text": text, "source_text": source_text, "source_url": source.get("url", ""),
                       "source_provenance": source.get("provenance", {}), "user_label": body.get("user_label"),
                       "user_comment": body.get("user_comment", ""), "model_version": version}
            return {"feedback": runtime.learning.submit(account_id, report["id"], pair_id, payload,
                        body["consent_version"], consent=body["consent"])}
        if method == "GET" and path == "/api/status":
            model_status = llm.status()
            embedding_model = model_status.get("embedding_model", os.environ.get("ISTOK_EMBED_MODEL", retrieval.EMBEDDING_MODEL))
            embedding_models = model_status.get("embedding_models", [])
            embedding = {"model": embedding_model, "models": embedding_models,
                         "available": embedding_model in embedding_models or embedding_model + ":latest" in embedding_models}
            if model_status.get("provider") == "modal":
                embedding.update(provider="modal", configured=model_status.get("configured", False),
                                 can_submit=model_status.get("can_submit", False),
                                 last_success_at=model_status.get("last_success_at"),
                                 synthetic_only=model_status.get("synthetic_only", True),
                                 local_inference_disabled=model_status.get("local_inference_disabled", False))
            return {"model": model_status, "embedding": embedding, "counts": store.counts(), "scope": "local_library",
                    "web": web_search.status()}
        if method == "GET" and path == "/api/sources":
            return {"sources": store.sources()}
        if method == "GET" and path == "/api/source-packs":
            existing_ids = {source["id"] for source in store.sources()}
            packs = source_packs.list_packs()
            for pack in packs:
                sources = source_packs.load_pack(pack["id"])["sources"]
                pack["installed_count"] = sum(source["id"] in existing_ids for source in sources)
            return {"packs": packs}
        if method == "POST" and path == "/api/source-packs/install":
            pack = source_packs.load_pack(field(body, "pack_id", 80))
            return store.install_sources(pack["sources"])
        if method == "POST" and path == "/api/sources":
            title = field(body, "title", 300)
            text = field(body, "text")
            url = field(body, "url", 2000, required=False).strip()
            if url and (urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc):
                raise ValueError("Укажите полную ссылку, начинающуюся с http:// или https://.")
            return {"source": runtime.add_source(store, title, text, url) if runtime else store.add_source(title, text, url)}
        if method == "DELETE" and path.startswith("/api/sources/"):
            if not store.delete_source(path.rsplit("/", 1)[1]):
                raise KeyError(path)
            return {"deleted": True}
        if method == "POST" and path == "/api/demo":
            sources = store.add_demo_sources(demo.SOURCES)
            return {"text": demo.TEXT, "sources": sources}
        if method == "POST" and path == "/api/web-check-jobs":
            if body.get("internet_consent") is not True:
                raise access.AccessError("Для поиска в интернете нужно отдельное согласие на отправку поисковых фраз.", "internet_consent_required", 400)
            web_status = web_search.status()
            if not web_status["configured"]:
                raise access.AccessError(web_status["error"], "web_not_configured", 503)
            text = field(body, "text")
            web_search.plan_queries(text)
            request_id = body.get("request_id")
            saas.ensure_job_identifier(store, request_id)
            operation_body = {"request_id": request_id, "text": text, "internet_consent": True}
            report_id = saas.report_identifier(request_id)
            account_id = self.identity.user["id"] if runtime else None
            if runtime:
                reservation = runtime.ledger.reserve_check(account_id, self.identity.device, request_id, text,
                                                           saas.fingerprint({"path": "/api/web-check", "body": operation_body}))
                if reservation["reused"]:
                    report = store.get("report", report_id)
                    if report:
                        runtime.ledger.bind_report(account_id, request_id, report_id)
                        runtime.ledger.commit(account_id, request_id)
                        return {"job": {"id": request_id, "kind": "model", "status": "complete", "phase": "complete",
                                        "stage": "Проверка завершена", "progress": {"current": 1, "total": 1},
                                        "result": {"report": public_report(report, store.match_reviews(report_id))}}}
                    reused = runtime.reused_job(jobs, store, account_id, reservation)
                    return {"job": reused}

            def operation(progress):
                if runtime:
                    runtime.ledger.mark_started(account_id, request_id)
                remote_id = saas.fingerprint({"account_id": account_id, "request_id": request_id})[:32] if runtime else request_id
                payload = web_search.run(text, remote_id, progress=progress)
                report = store.save("report", {**payload, "id": report_id})
                if runtime:
                    runtime.ledger.bind_report(account_id, request_id, report_id)
                    runtime.ledger.commit(account_id, request_id)
                return {"report": public_report(report, store.match_reviews(report_id))}

            try:
                return {"job": jobs.submit_model(request_id, "/api/web-check", operation_body, operation, store)}
            except BaseException:
                if runtime:
                    try:
                        runtime.ledger.release(account_id, request_id)
                    except access.AccessError:
                        pass
                raise
        if method == "POST" and path == "/api/check":
            text = field(body, "text")
            reservation = None
            if runtime:
                reservation = runtime.ledger.reserve_check(self.identity.user["id"], self.identity.device,
                    body.get("request_id"), text, saas.fingerprint({"text": text, "source_ids": body.get("source_ids")}))
                if reservation["reused"]:
                    report = store.get("report", saas.report_identifier(reservation["request_id"]))
                    if report is None:
                        raise access.AccessError("Исход предыдущей проверки не подтверждён. Повтор автоматически не запускается.", "check_uncertain", 409)
                    runtime.ledger.bind_report(self.identity.user["id"], reservation["request_id"], report["id"])
                    runtime.ledger.commit(self.identity.user["id"], reservation["request_id"])
                    return {"report": public_report(report, store.match_reviews(report["id"]))}
            try:
                sources = self.selected_sources(store, body)
            except Exception:
                if runtime:
                    runtime.ledger.release(self.identity.user["id"], reservation["request_id"])
                raise
            if runtime:
                runtime.ledger.mark_started(self.identity.user["id"], reservation["request_id"])
            result = analyze(text, sources)
            report = store.save("report", {**result, "text": text, "sources": sources, "scope": "local_library",
                                          **({"id": saas.report_identifier(reservation["request_id"])} if runtime else {})})
            if runtime:
                runtime.ledger.bind_report(self.identity.user["id"], reservation["request_id"], report["id"])
                runtime.ledger.commit(self.identity.user["id"], reservation["request_id"])
            return {"report": public_report(report, store.match_reviews(report["id"]))}
        if method == "POST" and path in MODEL_ROUTES:
            return self.model_operation(store, path, body)
        if method == "POST" and path == "/api/model-jobs":
            request_id = body.get("request_id")
            operation_path = body.get("path")
            operation_body = body.get("body")
            if (not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id)
                    or not isinstance(operation_path, str) or operation_path not in MODEL_ROUTES
                    or not isinstance(operation_body, dict)):
                raise ValueError("Некорректный запрос фоновой проверки.")
            # Detach the queued input from the HTTP handler's mutable request object.
            operation_body = json.loads(json.dumps(operation_body, ensure_ascii=False, allow_nan=False))
            candidate_attempt = None
            if operation_path == "/api/candidate-judge":
                record, candidate = self.candidate_pair(store, operation_body)
                candidate_attempt = {"retrieval_id": record["id"], "candidate_id": candidate["id"],
                                     "model": self.model(operation_body)}

            def operation(progress):
                if operation_path != "/api/evaluate":
                    progress("Подготавливаем серверную модель и выполняем проверку", 0, 1, phase="starting")
                result = self.model_operation(store, operation_path, operation_body, progress=progress)
                if operation_path != "/api/evaluate":
                    progress("Сохраняем результат", 1, 1, phase="saving")
                return result

            if runtime:
                if operation_path == "/api/evaluate":
                    raise access.AccessError("Диагностическая оценка недоступна в публичном приложении.", "operation_forbidden", 403)
                if operation_body.get("operation_id") != request_id:
                    raise access.AccessError("Идентификаторы операции и задания должны совпадать.", "request_conflict", 409)
                report_id = self.operation_report(store, operation_path, operation_body)
                kind = "rewrite" if operation_path in {"/api/rewrite", "/api/candidate-rewrite"} else "compare" if operation_path == "/api/compare" else "judge"
                job = runtime.queued_operation(jobs, store, self.identity.user["id"], request_id, operation_path,
                                               operation_body, report_id, kind, operation, candidate_attempt,
                                               device_id=self.identity.device)
                return {"job": job}
            return {"job": jobs.submit_model(request_id, operation_path, operation_body, operation, store,
                                               candidate_attempt=candidate_attempt)}
        if method == "GET" and path == "/api/reports":
            return {"reports": store.report_summaries()}
        if method == "GET" and path.startswith("/api/report-exports/"):
            report = store.get("report", path.rsplit("/", 1)[1])
            if not report:
                raise KeyError(path)
            saved_retrieval = store.latest_retrieval(report["id"])
            outcomes = store.candidate_judgments(saved_retrieval["id"]) if saved_retrieval else []
            return {"filename": f"istok-report-{report['id']}.html",
                    "html": report_export.render(report, store.match_reviews(report["id"]), saved_retrieval, outcomes)}
        if method == "GET" and path.startswith("/api/reports/"):
            report = store.get("report", path.rsplit("/", 1)[1])
            if not report:
                raise KeyError(path)
            return {"report": public_report(report, store.match_reviews(report["id"]))}
        if method == "POST" and path == "/api/report-reviews":
            report_id = field(body, "report_id", 100)
            match_id = field(body, "match_id", 100)
            report = store.get("report", report_id)
            if not report or match_id not in {item["id"] for item in report["matches"]}:
                raise KeyError("match")
            decision = body.get("decision")
            if decision not in {"pending", "confirmed", "dismissed"}:
                raise ValueError("Неизвестное решение по находке.")
            review = store.review_match(report_id, match_id, decision, field(body, "comment", 4000, False))
            return {"review": review}
        if method == "POST" and path == "/api/retrieval-jobs":
            report_id = field(body, "report_id", 100)
            report = store.get("report", report_id)
            if not report:
                raise KeyError("report")
            mode = body.get("mode", "hybrid")
            top_k = body.get("top_k", 5)
            if mode not in {"lexical", "hybrid"} or type(top_k) is not int or not 1 <= top_k <= 10:
                raise ValueError("Некорректные параметры поиска.")

            cache_path = self.tenant.embedding_cache_path if runtime else None

            def operation(progress):
                options = {"cache": retrieval.EmbeddingCache(cache_path)} if cache_path else {}
                result = retrieval.retrieve(report["text"], report["sources"], mode=mode, top_k=top_k, progress=progress, **options)
                return store.save("retrieval", {**result, "report_id": report_id})

            if runtime:
                model_ops_cost = retrieval.estimate_embedding_calls(report["text"], report["sources"]) if mode == "hybrid" else 1
                return {"job": runtime.queued_operation(jobs, store, self.identity.user["id"], body.get("operation_id"), path,
                    body, report_id, "semantic_retrieval" if mode == "hybrid" else "lexical_retrieval", operation,
                    device_id=self.identity.device, model_ops_cost=model_ops_cost)}
            return {"job": jobs.submit((report_id, mode, top_k), operation)}
        if method == "GET" and path.startswith("/api/jobs/"):
            job = jobs.get(path.rsplit("/", 1)[1], store)
            if runtime:
                runtime.reconcile_job(self.identity.user["id"], job)
            return {"job": job}
        if method == "POST" and path.startswith("/api/jobs/") and path.endswith("/cancel"):
            job = jobs.cancel(path.split("/")[-2])
            if runtime:
                runtime.reconcile_job(self.identity.user["id"], job)
            return {"job": job}
        if method == "GET" and path.startswith("/api/retrievals/"):
            report_id = path.rsplit("/", 1)[1]
            if not store.get("report", report_id):
                raise KeyError("report")
            return {"retrieval": store.latest_retrieval(report_id)}
        if method == "GET" and path.startswith("/api/candidate-judgments/"):
            retrieval_id = path.rsplit("/", 1)[1]
            if not store.get("retrieval", retrieval_id):
                raise KeyError("retrieval")
            judgments = []
            for item in store.candidate_judgments(retrieval_id):
                entry = {"candidate_id": item["candidate_id"]}
                if "judgment" in item:
                    entry["judgment"] = {**item["judgment"], "id": item["id"]}
                else:
                    entry["error"] = item["error"]
                    entry["code"] = item.get("code", "model_error")
                    entry["details"] = item.get("details")
                judgments.append(entry)
            return {"judgments": judgments}
        if method == "POST" and path == "/api/candidate-check-jobs":
            record = store.get("retrieval", field(body, "retrieval_id", 100))
            if not record:
                raise KeyError("retrieval")
            model = self.model(body)
            candidates = record["candidates"]
            if runtime:
                existing = {item["candidate_id"] for item in store.candidate_judgments(record["id"])}
                candidates = [candidate for candidate in candidates if candidate["id"] not in existing][:20]
                operation_id = body.get("operation_id")
                if not candidates:
                    try:
                        jobs.get(operation_id, store)
                    except KeyError:
                        raise access.AccessError("Все найденные пары уже проверены. Новая пакетная проверка не требуется.", "candidates_already_checked", 409) from None
                account_id = self.identity.user["id"]
                device_id = self.identity.device

            def operation(progress):
                started = time.monotonic()
                judgments = []
                for number, candidate in enumerate(candidates):
                    progress("Qwen сравнивает фрагменты", number, len(candidates))
                    try:
                        child_id = None
                        if runtime:
                            child_id = saas.fingerprint({"operation_id": operation_id, "candidate_id": candidate["id"]})[:32]
                            admission = runtime.ledger.reserve_operation(account_id, record["report_id"], child_id, "judge",
                                saas.fingerprint({"candidate": candidate, "model": model}), device_id=device_id)
                            if admission["reused"]:
                                raise access.AccessError("Исход этой пары уже зарезервирован. Автоматический повтор отключён.", "operation_uncertain", 409)
                            runtime.ledger.mark_started(account_id, child_id)
                        try:
                            judgment = judge_candidate(store, record, candidate, model)
                        finally:
                            if child_id:
                                runtime.ledger.commit(account_id, child_id)
                        judgments.append({"candidate_id": candidate["id"], "judgment": judgment})
                    except (llm.OllamaError, ValueError) as error:
                        judgments.append({"candidate_id": candidate["id"], "error": str(error),
                                          "code": getattr(error, "code", "model_error"),
                                          "details": getattr(error, "details", None)})
                progress("Сохраняем результаты", len(judgments), len(judgments))
                result = {"retrieval_id": record["id"], "report_id": record["report_id"], "judgments": judgments,
                          "counts": {"total": len(judgments),
                                     "errors": sum("error" in item and item.get("code") != "unconfirmed_exact" for item in judgments),
                                     "uncertain": sum(item.get("code") == "unconfirmed_exact" for item in judgments),
                                     "positive": sum(item.get("judgment", {}).get("relation") in {"exact", "paraphrase"} for item in judgments)},
                          "elapsed_seconds": round(time.monotonic() - started, 3)}
                return store.save("candidate-check", result)

            if runtime:
                return {"job": runtime.queued_operation(jobs, store, account_id, operation_id, path,
                    body, record["report_id"], "judge", operation, device_id=device_id)}
            return {"job": jobs.submit((record["id"], "candidate-check", model), operation, kind="candidate-check")}
        if method == "GET" and path == "/api/examples":
            reviews = {item["example_id"]: item for item in store.reviews()}
            return {"examples": [{**item, "review": reviews.get(item["id"])} for item in evaluation.examples()]}
        if method == "GET" and path == "/api/evaluations":
            return {"runs": store.records("evaluation")}
        if method == "POST" and path == "/api/reviews":
            example_id = field(body, "example_id", 100)
            if example_id not in {item["id"] for item in evaluation.examples()}:
                raise KeyError("example")
            relation = body.get("relation")
            if relation not in RELATIONS:
                raise ValueError("Неизвестная оценка.")
            return {"review": store.review(example_id, relation, field(body, "comment", 4000, False))}
        if method == "POST" and path == "/api/export":
            if body.get("split", "train") != "train":
                raise ValueError("Для обучения экспортируется только обучающая выборка.")
            reviews = {item["example_id"]: item for item in store.reviews()}
            records = [{**item, "expected": reviews[item["id"]]["relation"], "review": reviews[item["id"]]}
                       for item in evaluation.examples() if item["split"] == "train" and item["id"] in reviews]
            return {"records": records}
        raise KeyError(path)

    def selected_sources(self, store, body):
        sources = store.sources()
        selected = body.get("source_ids")
        if selected is not None:
            if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
                raise ValueError("Некорректный список источников.")
            selected_ids = set(selected)
            if selected_ids - {source["id"] for source in sources}:
                raise ValueError("Один из выбранных источников удалён. Обновите библиотеку.")
            sources = [source for source in sources if source["id"] in selected_ids]
        if not sources:
            raise ValueError("Добавьте хотя бы один источник или загрузите учебный пример.")
        return sources

    def operation_report(self, store, path, body):
        if path in {"/api/candidate-judge", "/api/candidate-rewrite"}:
            record, candidate = self.candidate_pair(store, body)
            report_id = record["report_id"]
        else:
            report_id = field(body, "report_id", 100)
        report = store.get("report", report_id)
        if not report:
            raise KeyError("report")
        if path == "/api/compare":
            if field(body, "text", llm.MAX_PASSAGE_LENGTH) != report["text"]:
                raise access.AccessError("Сравнение доступно только для текста сохранённой проверки.", "report_text_mismatch", 403)
            source = next((item for item in store.sources() if item["id"] == body.get("source_id")), None)
            if source is None:
                raise KeyError("source")
            llm._pair(report["text"], source["text"])
        elif path in {"/api/judge", "/api/rewrite"}:
            if body.get("match_id") not in {item["id"] for item in report["matches"]}:
                raise KeyError("match")
        return report_id

    def model_operation(self, store, path, body, progress=None):
        """Shared guards and evidence validation for immediate and queued requests."""
        if path == "/api/compare":
            text = field(body, "text", llm.MAX_PASSAGE_LENGTH)
            source_id = field(body, "source_id", 100)
            source = next((item for item in store.sources() if item["id"] == source_id), None)
            if not source:
                raise KeyError("source")
            with MODEL_LOCK:
                judgment = llm.judge(text, source["text"], self.model(body))
            store.save("comparison", {"text": text, "source": source, "judgment": judgment})
            return {"judgment": judgment}
        if path in {"/api/candidate-judge", "/api/candidate-rewrite"}:
            record, candidate = self.candidate_pair(store, body)
            candidate_id = candidate["id"]
            if path == "/api/candidate-judge":
                return {"judgment": judge_candidate(store, record, candidate, self.model(body))}
            mode = body.get("mode", "paraphrase")
            if mode not in {"quote", "paraphrase"}:
                raise ValueError("Выберите цитату или пересказ.")
            saved_judgment = store.get("candidate-judgment", field(body, "judgment_id", 100))
            if (not saved_judgment or saved_judgment["retrieval_id"] != record["id"]
                    or saved_judgment["candidate_id"] != candidate_id):
                raise ValueError("Сначала сравните эту пару фрагментов с Qwen.")
            self.validate_latest_judgment(store, record["id"], candidate_id, saved_judgment["id"])
            judgment = saved_judgment["judgment"]
            if judgment["relation"] not in {"exact", "paraphrase"}:
                raise ValueError("Для этой пары модель не выделила совпадение или пересказ. Выберите другой фрагмент.")
            target_start, target_end = judgment.get("evidence_text_start"), judgment.get("evidence_text_end")
            source_start, source_end = judgment.get("evidence_source_start"), judgment.get("evidence_source_end")
            self.validate_span(candidate["query_excerpt"], target_start, target_end, judgment.get("evidence_text"))
            self.validate_span(candidate["source_excerpt"], source_start, source_end, judgment.get("evidence_source"))
            original_text = candidate["query_excerpt"][target_start:target_end]
            source_text = candidate["source_excerpt"][source_start:source_end]
            with MODEL_LOCK:
                rewrite = llm.rewrite(original_text, source_text,
                                      candidate["source_title"], candidate["source_url"], mode, self.model(body))
            self.validate_latest_judgment(store, record["id"], candidate_id, saved_judgment["id"])
            rewrite.update(original_text=original_text,
                           judgment_id=saved_judgment["id"],
                           replacement_start=candidate["query_start"] + target_start,
                           replacement_end=candidate["query_start"] + target_end)
            return {"rewrite": rewrite}
        if path in {"/api/judge", "/api/rewrite"}:
            report = store.get("report", field(body, "report_id", 100))
            if not report:
                raise KeyError("report")
            match_id = field(body, "match_id", 100)
            match = next((match for match in report["matches"] if match["id"] == match_id), None)
            if not match:
                raise KeyError("match")
            model = self.model(body)
            # Evidence comes from saved reports, never from client-supplied source text.
            if path == "/api/judge":
                with MODEL_LOCK:
                    judgment = llm.judge(match["excerpt"], match["source_excerpt"], model)
                store.save("judgment", {"report_id": report["id"], "match_id": match_id, "judgment": judgment})
                return {"judgment": judgment}
            mode = body.get("mode", "paraphrase")
            if mode not in {"quote", "paraphrase"}:
                raise ValueError("Выберите цитату или пересказ.")
            with MODEL_LOCK:
                rewrite = llm.rewrite(match["excerpt"], match["source_excerpt"], match["source_title"], match["source_url"], mode, model)
            return {"rewrite": rewrite}
        if path == "/api/evaluate":
            split = body.get("split", "test")
            if split not in {"train", "test"}:
                raise ValueError("Неизвестная выборка.")
            if progress:
                total = sum(item["split"] == split for item in evaluation.examples())
                progress("Подготавливаем серверную модель и учебные пары", 0, total, phase="starting")

                def on_result(item, done, total):
                    progress("Сравниваем учебные пары", done, total, phase="comparing")

            with MODEL_LOCK:
                if progress:
                    run = evaluation.run(self.model(body), split, on_result=on_result)
                else:
                    run = evaluation.run(self.model(body), split)
            if progress:
                total = run.get("metrics", {}).get("total", total)
                progress("Сохраняем результаты учебных пар", len(run.get("results", [])), total, phase="saving")
            return {"run": store.save("evaluation", run)}
        raise KeyError(path)

    def candidate_pair(self, store, body):
        record = store.get("retrieval", field(body, "retrieval_id", 100))
        candidate_id = field(body, "candidate_id", 100)
        if not record:
            raise KeyError("retrieval")
        candidate = next((item for item in record["candidates"] if item["id"] == candidate_id), None)
        if not candidate:
            raise KeyError("candidate")
        return record, candidate

    def validate_latest_judgment(self, store, retrieval_id, candidate_id, judgment_id):
        latest = next((item for item in store.candidate_judgments(retrieval_id)
                       if item["candidate_id"] == candidate_id), None)
        if not latest or latest["id"] != judgment_id:
            raise ValueError("Эта пара уже проверена повторно. Обновите оценку перед созданием правки.")

    def validate_span(self, text, start, end, evidence):
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                or not isinstance(evidence, str) or text[start:end] != evidence):
            raise ValueError("Сохранённая оценка не содержит точных границ правки. Повторите сравнение пары.")

    def model(self, body):
        value = body.get("model")
        if value is not None and (not isinstance(value, str) or len(value) > 200 or not value.strip()):
            raise ValueError("Некорректное имя модели.")
        return value


def main():
    parser = argparse.ArgumentParser(description="Исток · локальная проверка заимствований")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--database", type=Path, default=None)
    options = parser.parse_args()
    mode = os.environ.get("ISTOK_APP_MODE", "local")
    if mode not in {"local", "saas"}:
        parser.error("Выберите ISTOK_APP_MODE=local либо saas.")
    if mode == "local" and options.host not in {"127.0.0.1", "localhost"}:
        parser.error("Публичный адрес требует настроенного режима saas.")
    if mode == "saas" and options.database:
        parser.error("Публичный режим использует отдельные базы аккаунтов; --database недоступен.")
    runtime = saas.SaaSRuntime.from_env() if mode == "saas" else None
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = ThreadingHTTPServer((options.host, options.port), Handler)
    server.daemon_threads = True
    server.saas = runtime
    if runtime is None:
        server.store = Store(options.database) if options.database else Store()
        if not options.database and not server.store.counts()["sources"]:
            for pack in source_packs.list_packs():
                server.store.install_sources(source_packs.load_pack(pack["id"])["sources"])
        server.jobs = Jobs()
    print(f"Исток готов — {runtime.config.origin if runtime else f'http://{options.host}:{options.port}'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if runtime:
            runtime.close()
        else:
            server.jobs.close()


if __name__ == "__main__":
    main()
