#!/usr/bin/env python3
"""Run the local web application with Python 3.11+ and no dependencies."""

import argparse
import json
import logging
import mimetypes
import os
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import urlsplit

from plagiarism import demo, evaluation, llm, retrieval
from plagiarism.jobs import Jobs
from plagiarism.matching import analyze, MAX_TEXT_CHARS
from plagiarism.storage import Store


ROOT = Path(__file__).resolve().parent
MODEL_LOCK = Lock()
MAX_BODY = 2_000_000
RELATIONS = {"exact", "paraphrase", "same_topic", "unrelated"}


def field(body, key, maximum=MAX_TEXT_CHARS, required=True):
    value = body.get(key, "")
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"Некорректное значение поля «{key}».")
    if required and not value.strip():
        raise ValueError(f"Заполните поле «{key}».")
    return value


def public_report(report, reviews=None):
    return {key: value for key, value in report.items() if key != "sources"} | {"reviews": reviews or {}}


def judge_candidate(store, record, candidate, model):
    try:
        with MODEL_LOCK:
            judgment = llm.judge(candidate["query_excerpt"], candidate["source_excerpt"], model)
    except (llm.OllamaError, ValueError) as error:
        store.save("candidate-error", {"retrieval_id": record["id"], "candidate_id": candidate["id"],
                                       "error": str(error), "model": model})
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
        self.end_headers()
        self.wfile.write(content)

    def trusted_request(self):
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
        try:
            if not self.trusted_request():
                self.send_json({"error": "Запрос разрешён только из локального приложения."}, 403)
                return
            path = urlsplit(self.path).path
            if not path.startswith("/api/"):
                if method != "GET":
                    self.send_json({"error": "Метод недоступен."}, 405)
                    return
                self.static(path)
                return
            body = self.read_body() if method == "POST" else {}
            result = self.route(method, path, body)
            self.send_json(result)
        except ValueError as error:
            self.send_json({"error": str(error)}, 400)
        except KeyError:
            self.send_json({"error": "Запись не найдена. Обновите страницу."}, 404)
        except llm.OllamaError as error:
            self.send_json({"error": str(error)}, 503)
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
        files = {"/": "index.html", "/index.html": "index.html", "/style.css": "style.css", "/app.js": "app.js"}
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
        self.send_header("Content-Type", (mimetypes.guess_type(file.name)[0] or "text/plain") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def route(self, method, path, body):
        store = self.server.store
        if method == "GET" and path == "/api/status":
            model_status = llm.status()
            embedding_model = os.environ.get("ISTOK_EMBED_MODEL", retrieval.EMBEDDING_MODEL)
            embedding_models = model_status.get("embedding_models", [])
            embedding = {"model": embedding_model, "models": embedding_models,
                         "available": embedding_model in embedding_models or embedding_model + ":latest" in embedding_models}
            return {"model": model_status, "embedding": embedding, "counts": store.counts(), "scope": "local_library"}
        if method == "GET" and path == "/api/sources":
            return {"sources": store.sources()}
        if method == "POST" and path == "/api/sources":
            title = field(body, "title", 300)
            text = field(body, "text")
            url = field(body, "url", 2000, required=False).strip()
            if url and (urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc):
                raise ValueError("Укажите полную ссылку, начинающуюся с http:// или https://.")
            return {"source": store.add_source(title, text, url)}
        if method == "DELETE" and path.startswith("/api/sources/"):
            if not store.delete_source(path.rsplit("/", 1)[1]):
                raise KeyError(path)
            return {"deleted": True}
        if method == "POST" and path == "/api/demo":
            sources = store.add_demo_sources(demo.SOURCES)
            return {"text": demo.TEXT, "sources": sources}
        if method == "POST" and path == "/api/check":
            text = field(body, "text")
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
            result = analyze(text, sources)
            report = store.save("report", {**result, "text": text, "sources": sources, "scope": "local_library"})
            return {"report": public_report(report, store.match_reviews(report["id"]))}
        if method == "POST" and path == "/api/compare":
            text = field(body, "text", llm.MAX_PASSAGE_LENGTH)
            source_id = field(body, "source_id", 100)
            source = next((item for item in store.sources() if item["id"] == source_id), None)
            if not source:
                raise KeyError("source")
            with MODEL_LOCK:
                judgment = llm.judge(text, source["text"], self.model(body))
            store.save("comparison", {"text": text, "source": source, "judgment": judgment})
            return {"judgment": judgment}
        if method == "GET" and path == "/api/reports":
            return {"reports": store.report_summaries()}
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

            def operation(progress):
                result = retrieval.retrieve(report["text"], report["sources"], mode=mode, top_k=top_k, progress=progress)
                return store.save("retrieval", {**result, "report_id": report_id})

            return {"job": self.server.jobs.submit((report_id, mode, top_k), operation)}
        if method == "GET" and path.startswith("/api/jobs/"):
            return {"job": self.server.jobs.get(path.rsplit("/", 1)[1])}
        if method == "POST" and path.startswith("/api/jobs/") and path.endswith("/cancel"):
            return {"job": self.server.jobs.cancel(path.split("/")[-2])}
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
                judgments.append(entry)
            return {"judgments": judgments}
        if method == "POST" and path == "/api/candidate-check-jobs":
            record = store.get("retrieval", field(body, "retrieval_id", 100))
            if not record:
                raise KeyError("retrieval")
            model = self.model(body)

            def operation(progress):
                started = time.monotonic()
                judgments = []
                for number, candidate in enumerate(record["candidates"]):
                    progress("Qwen сравнивает фрагменты", number, len(record["candidates"]))
                    try:
                        judgment = judge_candidate(store, record, candidate, model)
                        judgments.append({"candidate_id": candidate["id"], "judgment": judgment})
                    except (llm.OllamaError, ValueError) as error:
                        judgments.append({"candidate_id": candidate["id"], "error": str(error)})
                progress("Сохраняем результаты", len(judgments), len(judgments))
                result = {"retrieval_id": record["id"], "report_id": record["report_id"], "judgments": judgments,
                          "counts": {"total": len(judgments), "errors": sum("error" in item for item in judgments),
                                     "positive": sum(item.get("judgment", {}).get("relation") in {"exact", "paraphrase"} for item in judgments)},
                          "elapsed_seconds": round(time.monotonic() - started, 3)}
                return store.save("candidate-check", result)

            return {"job": self.server.jobs.submit((record["id"], "candidate-check", model), operation, kind="candidate-check")}
        if method == "POST" and path in {"/api/candidate-judge", "/api/candidate-rewrite"}:
            record = store.get("retrieval", field(body, "retrieval_id", 100))
            candidate_id = field(body, "candidate_id", 100)
            if not record:
                raise KeyError("retrieval")
            candidate = next((item for item in record["candidates"] if item["id"] == candidate_id), None)
            if not candidate:
                raise KeyError("candidate")
            if path == "/api/candidate-judge":
                return {"judgment": judge_candidate(store, record, candidate, self.model(body))}
            mode = body.get("mode", "paraphrase")
            if mode not in {"quote", "paraphrase"}:
                raise ValueError("Выберите цитату или пересказ.")
            saved_judgment = store.get("candidate-judgment", field(body, "judgment_id", 100))
            if (not saved_judgment or saved_judgment["retrieval_id"] != record["id"]
                    or saved_judgment["candidate_id"] != candidate_id):
                raise ValueError("Сначала сравните эту пару фрагментов с Qwen.")
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
            rewrite.update(original_text=original_text,
                           replacement_start=candidate["query_start"] + target_start,
                           replacement_end=candidate["query_start"] + target_end)
            return {"rewrite": rewrite}
        if method == "POST" and path in {"/api/judge", "/api/rewrite"}:
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
        if method == "GET" and path == "/api/examples":
            reviews = {item["example_id"]: item for item in store.reviews()}
            return {"examples": [{**item, "review": reviews.get(item["id"])} for item in evaluation.examples()]}
        if method == "GET" and path == "/api/evaluations":
            return {"runs": store.records("evaluation")}
        if method == "POST" and path == "/api/evaluate":
            split = body.get("split", "test")
            if split not in {"train", "test"}:
                raise ValueError("Неизвестная выборка.")
            with MODEL_LOCK:
                run = evaluation.run(self.model(body), split)
            return {"run": store.save("evaluation", run)}
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
    parser.add_argument("--database", type=Path, default=None)
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", options.port), Handler)
    server.daemon_threads = True
    server.store = Store(options.database) if options.database else Store()
    server.jobs = Jobs()
    print(f"Исток готов — http://127.0.0.1:{options.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.jobs.close()


if __name__ == "__main__":
    main()
