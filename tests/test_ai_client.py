"""
Тесты клиента ИИ (modules/ai_client.py).

Worker поднимаем локально: обычный HTTP-сервер из стандартной библиотеки,
который отвечает так же, как настоящий Cloudflare Worker
(/health, /v1/chat/completions, 401, 429, 500).

Запуск: .venv/bin/pytest tests/ -v   (или  py -m pytest tests/ -v)
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from modules import ai_client  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════
# Локальный «Worker»
# ═══════════════════════════════════════════════════════════════════════

class _Handler(BaseHTTPRequestHandler):
    """Отвечает как настоящий Worker. Поведение задаётся через server.behaviour."""

    def log_message(self, *args):
        pass  # не засоряем вывод тестов

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send({
                "ok": True,
                "version": "1.0",
                "colo": "DME",
                "providers": {"cloudflare": True, "gemini": False, "groq": False, "openrouter": False},
                "models": ["@cf/openai/gpt-oss-120b"],
            })
        else:
            self._send({"error": {"message": "нет такого пути"}}, 404)

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        behaviour = getattr(self.server, "behaviour", "ok")

        if behaviour == "unauthorized":
            self._send({"error": {"message": "неверный токен", "type": "unauthorized"}}, 401)
            return
        if auth != "Bearer test-token":
            self._send({"error": {"message": "неверный токен", "type": "unauthorized"}}, 401)
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.last_request = body

        if behaviour == "limit":
            self._send({"error": {"message": "дневной бесплатный лимит нейронов исчерпан", "type": "ai_limit_reached"}}, 429)
            return
        if behaviour == "error500":
            self._send({"error": {"message": "ошибка модели", "type": "ai_error"}}, 502)
            return
        if behaviour == "empty":
            self._send({"choices": [{"message": {"content": ""}}]}, 200)
            return

        self._send({
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": body.get("model", "@cf/openai/gpt-oss-120b"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Тестовый гайд по игре."}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })


@pytest.fixture(scope="module")
def worker_url():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.behaviour = "ok"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", server
    server.shutdown()


@pytest.fixture
def client(worker_url):
    url, server = worker_url
    server.behaviour = "ok"
    return ai_client.AIClient(base_url=url, token="test-token", timeout=10)


# ═══════════════════════════════════════════════════════════════════════
# 1. Конфигурация и /health
# ═══════════════════════════════════════════════════════════════════════

class TestConfiguration:

    def test_not_configured_without_url(self):
        c = ai_client.AIClient(base_url="", token="")
        assert c.configured is False

    def test_configured_with_url(self, client):
        assert client.configured is True

    def test_health_ok(self, client):
        ok, info = client.health()
        assert ok is True
        assert info["version"] == "1.0"
        assert info["colo"] == "DME"
        assert info["providers"]["cloudflare"] is True

    def test_health_without_url_says_how_to_fix(self):
        ok, message = ai_client.AIClient(base_url="").health()
        assert ok is False
        assert "AI_WORKER_URL" in message

    def test_health_is_public_so_you_can_check_in_browser(self, worker_url):
        """/health специально без пароля: чтобы открыть в браузере и увидеть, жив ли Worker."""
        url, _ = worker_url
        c = ai_client.AIClient(base_url=url, token="wrong")
        ok, info = c.health()
        assert ok is True
        assert info["ok"] is True

    def test_chat_requires_correct_token(self, worker_url):
        url, _ = worker_url
        c = ai_client.AIClient(base_url=url, token="wrong", timeout=10)
        with pytest.raises(ai_client.AIError):
            c.chat([{"role": "user", "content": "привет"}], model="@cf/a", attempts=1)


# ═══════════════════════════════════════════════════════════════════════
# 2. Запросы к модели
# ═══════════════════════════════════════════════════════════════════════

class TestChat:

    def test_returns_text(self, client):
        text = client.chat([{"role": "user", "content": "привет"}], model="@cf/openai/gpt-oss-120b")
        assert "Тестовый гайд" in text

    def test_model_and_stream_sent(self, client, worker_url):
        _, server = worker_url
        client.chat([{"role": "user", "content": "привет"}], model="@cf/openai/gpt-oss-120b")
        assert server.last_request["model"] == "@cf/openai/gpt-oss-120b"
        assert server.last_request["stream"] is False

    def test_max_tokens_passed(self, client, worker_url):
        _, server = worker_url
        client.chat([{"role": "user", "content": "привет"}], model="m", max_tokens=123)
        assert server.last_request["max_tokens"] == 123

    def test_no_url_raises_unavailable(self):
        c = ai_client.AIClient(base_url="", token="")
        with pytest.raises(ai_client.AIUnavailable):
            c.chat([{"role": "user", "content": "привет"}])

    def test_unavailable_is_subclass_of_error(self):
        assert issubclass(ai_client.AIUnavailable, ai_client.AIError)
        assert issubclass(ai_client.AILimitReached, ai_client.AIError)

    def test_limit_raises_limit_reached(self, client, worker_url):
        _, server = worker_url
        server.behaviour = "limit"
        with pytest.raises(ai_client.AILimitReached):
            client.chat([{"role": "user", "content": "привет"}], model="m", attempts=1)

    def test_server_error_raises(self, client, worker_url):
        _, server = worker_url
        server.behaviour = "error500"
        with pytest.raises(ai_client.AIError):
            client.chat([{"role": "user", "content": "привет"}], model="m", attempts=1)

    def test_empty_response_raises(self, client, worker_url):
        _, server = worker_url
        server.behaviour = "empty"
        with pytest.raises(ai_client.AIError):
            client.chat([{"role": "user", "content": "привет"}], model="m", attempts=1)

    def test_usage_written_to_log(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(ai_client, "USAGE_LOG", str(tmp_path / "usage.jsonl"))
        client.chat([{"role": "user", "content": "привет"}], model="@cf/openai/gpt-oss-120b")
        content = (tmp_path / "usage.jsonl").read_text(encoding="utf-8")
        assert "@cf/openai/gpt-oss-120b" in content
        assert "15" in content


# ═══════════════════════════════════════════════════════════════════════
# 3. Перебор моделей по очереди
# ═══════════════════════════════════════════════════════════════════════

class TestChain:

    def test_chain_uses_first_working_model(self, client):
        text, model = client.chat_chain([{"role": "user", "content": "привет"}], models=["@cf/a", "@cf/b"])
        assert "Тестовый гайд" in text
        assert model == "@cf/a"

    def test_chain_reports_limit_immediately(self, client, worker_url):
        _, server = worker_url
        server.behaviour = "limit"
        with pytest.raises(ai_client.AILimitReached):
            client.chat_chain([{"role": "user", "content": "привет"}], models=["@cf/a", "@cf/b"])

    def test_chain_raises_when_nothing_works(self, client, worker_url):
        _, server = worker_url
        server.behaviour = "error500"
        with pytest.raises(ai_client.AIError):
            client.chat_chain([{"role": "user", "content": "привет"}], models=["@cf/a"])


# ═══════════════════════════════════════════════════════════════════════
# 4. Настройки из .env и разбор ответов
# ═══════════════════════════════════════════════════════════════════════

class TestSettings:

    def test_model_priority_from_env(self, monkeypatch):
        monkeypatch.setenv("AI_MODEL_PRIORITY", "gemini-2.5-flash, @cf/openai/gpt-oss-120b")
        assert ai_client.get_model_priority() == ["gemini-2.5-flash", "@cf/openai/gpt-oss-120b"]

    def test_model_priority_default(self, monkeypatch):
        monkeypatch.delenv("AI_MODEL_PRIORITY", raising=False)
        models = ai_client.get_model_priority()
        assert models and models[0].startswith("@cf/")

    def test_worker_url_strips_slash(self, monkeypatch):
        monkeypatch.setenv("AI_WORKER_URL", "https://example.workers.dev/")
        assert ai_client.get_worker_url() == "https://example.workers.dev"

    def test_extract_text_openai_format(self):
        data = {"choices": [{"message": {"content": "привет"}}]}
        assert ai_client._extract_text(data) == "привет"

    def test_extract_text_cf_native_format(self):
        assert ai_client._extract_text({"response": "привет"}) == "привет"

    def test_extract_text_content_blocks(self):
        data = {"choices": [{"message": {"content": [{"text": "а"}, {"text": "б"}]}}]}
        assert ai_client._extract_text(data) == "аб"

    def test_extract_text_empty(self):
        assert ai_client._extract_text({}) == ""

    def test_used_today_counts_requests(self, tmp_path, monkeypatch):
        log = tmp_path / "usage.jsonl"
        monkeypatch.setattr(ai_client, "USAGE_LOG", str(log))
        ai_client._log_usage("@cf/x", {"total_tokens": 42}, 1.0, 10)
        count, tokens = ai_client.used_today()
        assert count == 1
        assert tokens == 42
