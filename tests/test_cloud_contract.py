import copy
import unittest

from plagiarism import cloud_contract as contract


def manifest():
    return {"provider": "modal", "app_name": "istok-inference", "class_name": "OllamaService",
            "environment": "main", "deployment_id": "a" * 64, "default_model": "qwen3:4b",
            "embedding_model": "bge-m3", "synthetic_only": True, "verified_at": "2026-11-11T12:00:00Z",
            "models": [{"name": "qwen3:4b", "digest": "b" * 64, "capabilities": ["completion"]},
                       {"name": "bge-m3:latest", "digest": "c" * 64, "capabilities": ["embedding"]}]}


def chat():
    return {"model": "qwen3:4b", "messages": [{"role": "system", "content": "Return JSON"},
            {"role": "user", "content": "Авторский учебный пример."}], "format": {"type": "object"},
            "stream": False, "think": False, "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 1000},
            "keep_alive": "5m"}


class CloudContractTests(unittest.TestCase):
    def test_manifest_accepts_real_tag_alias_without_class_id_and_copies_input(self):
        original = manifest()
        checked = contract.validate_manifest(original)
        checked["models"][0]["name"] = "changed"
        self.assertEqual(original["models"][0]["name"], "qwen3:4b")
        self.assertEqual(contract.model_entry(original, "bge-m3")["name"], "bge-m3:latest")

    def test_manifest_rejects_missing_digest_duplicate_wrong_capability_and_timestamp(self):
        for mutation in (lambda v: v["models"][0].pop("digest"),
                         lambda v: v["models"].append(copy.deepcopy(v["models"][0])),
                         lambda v: v.update(default_model="bge-m3"),
                         lambda v: v.update(verified_at="2026-11-11"),
                         lambda v: v.update(deployment_id="name-only-version")):
            value = manifest()
            mutation(value)
            with self.assertRaises(contract.CloudContractError):
                contract.validate_manifest(value)

    def test_existing_chat_and_embedding_payloads_are_accepted_with_canonical_model(self):
        self.assertEqual(contract.validate_request("/api/chat", chat(), manifest()), chat())
        value = {"model": "bge-m3", "input": ["Клетка содержит генетический материал."],
                 "truncate": False, "keep_alive": "2m"}
        result = contract.validate_request("/api/embed", value, manifest())
        self.assertEqual(result["model"], "bge-m3:latest")
        self.assertEqual(value["model"], "bge-m3")

    def test_arbitrary_operations_models_messages_and_limits_are_rejected(self):
        for path in ("/api/pull", "/api/delete", "/api/generate", "http://localhost/api/chat", "/api/tags"):
            with self.assertRaises(contract.CloudContractError):
                contract.validate_request(path, chat(), manifest())
        for update in ({"model": "qwen3:32b"}, {"stream": True}, {"think": True}, {"tools": []},
                       {"options": {"temperature": 0, "num_ctx": 999999, "num_predict": 1000}},
                       {"messages": [{"role": "user", "content": "text"}]}, {"keep_alive": "24h"}):
            with self.assertRaises(contract.CloudContractError):
                contract.validate_request("/api/chat", chat() | update, manifest())

    def test_embedding_resource_bounds_and_nan_are_rejected(self):
        for texts in (["x"] * 33, ["x" * 12001], ["x" * 12000] * 11, [""]):
            with self.assertRaises(contract.CloudContractError):
                contract.validate_request("/api/embed", {"model": "bge-m3", "input": texts,
                                          "truncate": False, "keep_alive": "2m"}, manifest())
        value = chat()
        value["format"]["bad"] = float("nan")
        with self.assertRaises(contract.CloudContractError):
            contract.validate_request("/api/chat", value, manifest())

    def test_response_requires_matching_deployment_and_actual_model_digests(self):
        config = manifest()
        envelope = {"deployment_id": config["deployment_id"], "model_digests": contract.model_digests(config),
                    "response": {"embeddings": [[0.1, 0.2]]}}
        self.assertEqual(contract.validate_envelope(envelope, config), envelope["response"])
        for update in ({"deployment_id": "d" * 64}, {"model_digests": {}},
                       {"response": {"error": "SECRET PRIVATE PROVIDER ERROR"}},
                       {"response": {"embeddings": [[float("inf")]]}}):
            with self.assertRaises(contract.CloudContractError) as context:
                contract.validate_envelope(envelope | update, config)
            self.assertNotIn("SECRET", str(context.exception))


if __name__ == "__main__":
    unittest.main()
