"""Rank source passages using BM25 and optional local multilingual embeddings.

Candidates are suggestions for inspection, never plagiarism verdicts. All offsets
refer to the supplied original strings, and every source is a report snapshot.
"""

from collections import Counter
from contextlib import closing
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

from plagiarism import llm
from plagiarism.matching import normalize


EMBEDDING_MODEL = "bge-m3"
MAX_QUERY_CHUNKS = 160
MAX_SOURCE_CHUNKS = 4000
CHUNK_WORDS = 120
OVERLAP_WORDS = 25
MAX_CHUNK_CHARACTERS = 6000
EMBEDDING_BATCH_SIZE = 12
CACHE_PATH = Path(__file__).resolve().parent.parent / ".runtime" / "embeddings.sqlite3"
STOPWORDS = frozenset("и в во на с со к у а но или для из по от до при о об что это как не ни the a an and or in on of to for by with is are was were this that".split())


def chunks(text):
    """Return overlapping word windows with exact original character offsets."""
    words = list(re.finditer(r"\S+", text))
    output = []
    position = 0
    while position < len(words):
        stop = min(position + CHUNK_WORDS, len(words))
        # Prefer a sentence end in the last quarter of a full window.
        if stop < len(words):
            for index in range(stop - 1, position + CHUNK_WORDS * 3 // 4, -1):
                if re.search(r"[.!?][\"»)]*$", words[index].group()):
                    stop = index + 1
                    break
        start_offset = words[position].start()
        end_offset = words[stop - 1].end()
        piece_start = start_offset
        while piece_start < end_offset:
            piece_end = min(piece_start + MAX_CHUNK_CHARACTERS, end_offset)
            output.append({"start": piece_start, "end": piece_end, "text": text[piece_start:piece_end]})
            if piece_end == end_offset:
                break
            piece_start = piece_end - 200
        if stop == len(words):
            break
        position = stop - OVERLAP_WORDS
    return output


def estimate_embedding_calls(text, sources):
    """Conservatively price a cold hybrid index without contacting a model.

    The embedding cache batches twelve distinct passages per call. Existing
    vectors can reduce actual calls, but are deliberately not an admission
    discount: a free request must fit the budget even when its cache is cold.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Добавьте текст для поиска источников.")
    queries = chunks(text)
    source_chunks = [chunk for source in sources for chunk in chunks(source["text"])]
    if len(queries) > MAX_QUERY_CHUNKS or len(source_chunks) > MAX_SOURCE_CHUNKS:
        raise ValueError("Для этого локального прогона выберите более короткий текст или меньшую библиотеку. Лимит — 160 фрагментов текста и 4000 фрагментов источников.")
    if not source_chunks:
        raise ValueError("Добавьте непустые источники для поиска.")
    unique = {chunk["text"] for chunk in source_chunks + queries}
    return (len(unique) + EMBEDDING_BATCH_SIZE - 1) // EMBEDDING_BATCH_SIZE


def terms(text):
    return [word for word in normalize(text).split() if len(word) > 1 and word not in STOPWORDS]


class BM25:
    def __init__(self, passages):
        self.documents = [Counter(terms(passage)) for passage in passages]
        self.lengths = [sum(document.values()) for document in self.documents]
        self.average_length = sum(self.lengths) / max(1, len(self.lengths))
        self.frequency = Counter(term for document in self.documents for term in document)

    def scores(self, query):
        query_terms = set(terms(query))
        count = len(self.documents)
        scores = []
        for document, length in zip(self.documents, self.lengths):
            score = 0.0
            for term in query_terms & document.keys():
                frequency = document[term]
                inverse_frequency = math.log(1 + (count - self.frequency[term] + 0.5) / (self.frequency[term] + 0.5))
                denominator = frequency + 1.2 * (0.25 + 0.75 * length / max(1, self.average_length))
                score += inverse_frequency * frequency * 2.2 / denominator
            scores.append(score)
        return scores


class EmbeddingCache:
    """Content-addressed vectors, invalidated when the installed model changes."""

    def __init__(self, path=CACHE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def vectors(self, texts, model, identity, progress=None):
        keys = [sha256((identity + "\0" + text).encode("utf-8")).hexdigest() for text in texts]
        cached = {}
        with closing(self.connect()) as connection:
            for key in set(keys):
                row = connection.execute("SELECT vector FROM vectors WHERE key = ?", (key,)).fetchone()
                if row:
                    cached[key] = json.loads(row[0])
        missing = list(dict.fromkeys(key for key in keys if key not in cached))
        by_key = dict(zip(keys, texts))
        for start in range(0, len(missing), EMBEDDING_BATCH_SIZE):
            batch = missing[start:start + EMBEDDING_BATCH_SIZE]
            if progress:
                progress("Вычисляем смысловые представления", start, len(missing))
            vectors = llm.embed([by_key[key] for key in batch], model)
            with closing(self.connect()) as connection, connection:
                for key, vector in zip(batch, vectors):
                    magnitude = math.sqrt(sum(value * value for value in vector))
                    if not magnitude:
                        raise llm.OllamaError("Модель вернула пустое смысловое представление.")
                    normalized = [value / magnitude for value in vector]
                    connection.execute("INSERT OR REPLACE INTO vectors VALUES (?, ?)", (key, json.dumps(normalized)))
                    cached[key] = normalized
        vectors = [cached[key] for key in keys]
        if len({len(vector) for vector in vectors}) > 1:
            raise llm.OllamaError("Размер смысловых представлений изменился. Перезапустите проверку.")
        return vectors, len(missing)


def embedding_identity(model):
    response = llm._request("/api/tags", timeout=3)
    for entry in response.get("models", []):
        if entry.get("name") in {model, model + ":latest"}:
            return model + ":" + entry["digest"]
    raise llm.OllamaError(f"Модель поиска по смыслу {model} ещё не загружена.")


def _dot(left, right):
    if hasattr(math, "sumprod"):
        return math.sumprod(left, right)
    return sum(first * second for first, second in zip(left, right))


def retrieve(text, sources, mode="lexical", top_k=5, cache=None, progress=None):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Добавьте текст для поиска источников.")
    if mode not in {"lexical", "hybrid"}:
        raise ValueError("Неизвестный режим поиска.")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 10:
        raise ValueError("Можно запросить от 1 до 10 кандидатов на фрагмент.")
    started = time.monotonic()
    queries = chunks(text)
    source_chunks = [{**chunk, "source": source} for source in sources for chunk in chunks(source["text"])]
    if len(queries) > MAX_QUERY_CHUNKS or len(source_chunks) > MAX_SOURCE_CHUNKS:
        raise ValueError("Для этого локального прогона выберите более короткий текст или меньшую библиотеку. Лимит — 160 фрагментов текста и 4000 фрагментов источников.")
    if not source_chunks:
        raise ValueError("Добавьте непустые источники для поиска.")
    index = BM25([chunk["text"] for chunk in source_chunks])
    source_vectors = query_vectors = None
    cache_misses = 0
    model = None
    if mode == "hybrid":
        model = os.environ.get("ISTOK_EMBED_MODEL", EMBEDDING_MODEL)
        identity = embedding_identity(model)
        cache = cache or EmbeddingCache()
        all_vectors, cache_misses = cache.vectors(
            [chunk["text"] for chunk in source_chunks] + [query["text"] for query in queries],
            model, identity, progress,
        )
        source_vectors = all_vectors[:len(source_chunks)]
        query_vectors = all_vectors[len(source_chunks):]
    candidates = []
    for query_index, query in enumerate(queries):
        if progress:
            progress("Ранжируем фрагменты источников", query_index, len(queries))
        lexical = index.scores(query["text"])
        lexical_order = sorted((i for i, score in enumerate(lexical) if score > 0), key=lambda i: (-lexical[i], i))
        lexical_ranks = {index: rank for rank, index in enumerate(lexical_order, 1)}
        semantic = [_dot(query_vectors[query_index], vector) for vector in source_vectors] if mode == "hybrid" else []
        semantic_order = sorted(range(len(semantic)), key=lambda i: (-semantic[i], i))
        semantic_ranks = {index: rank for rank, index in enumerate(semantic_order, 1)}
        if mode == "hybrid":
            # Reciprocal-rank fusion combines incomparable score scales.
            scores = {i: (1 / (60 + lexical_ranks[i]) if i in lexical_ranks else 0)
                      + 1 / (60 + semantic_ranks[i]) for i in semantic_ranks}
            order = sorted(scores, key=lambda i: (-scores[i], -semantic[i], i))
            # A translated passage can have zero lexical overlap. Preserve the
            # best dense candidate within the requested shortlist so weak word
            # matches cannot exclude the semantic search's strongest result.
            strongest_semantic = semantic_order[0]
            if top_k > 1 and strongest_semantic not in order[:top_k]:
                order.remove(strongest_semantic)
                order.insert(top_k - 1, strongest_semantic)
        else:
            order = lexical_order
            scores = {i: lexical[i] for i in order}
        selected = []
        for position in order:
            chunk = source_chunks[position]
            # Suppress substantially overlapping windows from the same source.
            duplicate = any(
                previous["source"]["id"] == chunk["source"]["id"]
                and min(previous["end"], chunk["end"]) - max(previous["start"], chunk["start"])
                > 0.5 * min(previous["end"] - previous["start"], chunk["end"] - chunk["start"])
                for previous in selected
            )
            if duplicate:
                continue
            selected.append(chunk)
            source = chunk["source"]
            candidates.append({
                "id": f"candidate-{query_index + 1}-{len(selected)}",
                "query_index": query_index, "rank": len(selected),
                "query_start": query["start"], "query_end": query["end"], "query_excerpt": query["text"],
                "source_id": source["id"], "source_title": source.get("title", "Источник"),
                "source_url": source.get("url", ""), "source_start": chunk["start"],
                "source_end": chunk["end"], "source_excerpt": chunk["text"],
                "bm25_score": round(lexical[position], 5),
                "semantic_score": round(semantic[position], 5) if semantic else None,
                "rank_score": round(scores[position], 7),
            })
            if len(selected) == top_k:
                break
    if progress:
        progress("Поиск источников завершён", len(queries), len(queries))
    return {
        "candidates": candidates, "mode": mode, "embedding_model": model,
        "stats": {"query_chunks": len(queries), "source_chunks": len(source_chunks),
                  "candidate_count": len(candidates), "cache_misses": cache_misses,
                  "ranking_method": "rrf-dense-preservation-v2" if mode == "hybrid" else "bm25-v1",
                  "elapsed_seconds": round(time.monotonic() - started, 3)},
        "warnings": ["Кандидаты показывают сходство с источником и требуют проверки. Ранг и смысловое сходство не являются вероятностью плагиата."],
    }
