"""
Тесты клиента ИИ (modules/ai_client.py).

Worker поднимаем локально: обычный HTTP-сервер из стандартной библиотеки,
который отвечает так же, как настоящий Cloudflare Worker
(/health, /v1/chat/completions, 401, 429, 500).

Запуск: .venv/bin/pytest tests/ -v   (или  py -m pytest tests/ -v)
"""

import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

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
        elif self.path == "/v1/models":
            behaviour = getattr(self.server, "behaviour", "ok")
            if behaviour == "no_models":
                self._send({"error": {"message": "нет такого пути"}}, 404)
                return
            self._send({
                "object": "list",
                "data": [
                    {"id": "@cf/openai/gpt-oss-120b"},
                    {"id": "@cf/qwen/qwen3-30b-a3b-fp8"},
                    {"id": "@cf/openai/gpt-oss-20b"},
                    {"id": "gemini-2.5-flash"},
                    {"id": "gemini-2.5-pro"},
                    {"id": "gemini-2.0-flash"},
                ],
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
        if not hasattr(self.server, "requested_models"):
            self.server.requested_models = []
        self.server.requested_models.append(body.get("model"))

        if behaviour == "limit":
            self._send({"error": {"message": "дневной бесплатный лимит нейронов исчерпан", "type": "ai_limit_reached"}}, 429)
            return
        if behaviour == "limit_first":
            if not getattr(self.server, "limit_first_done", False):
                self.server.limit_first_done = True
                self._send({"error": {"message": "дневной бесплатный лимит нейронов исчерпан", "type": "ai_limit_reached"}}, 429)
                return
        if behaviour == "unknown_model":
            self._send({
                "error": {
                    "message": 'модель "gemini-3.6-flash" не поддерживается. '
                               "Доступные: @cf/openai/gpt-oss-120b, gemini-2.5-flash, gemini-2.5-pro",
                    "type": "unknown_model",
                }
            }, 400)
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
        assert server.last_request["stream"] is True

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

    def test_chain_uses_next_model_after_limit(self, client, worker_url):
        _, server = worker_url
        server.behaviour = "limit_first"
        server.limit_first_done = False

        text, model = client.chat_chain(
            [{"role": "user", "content": "привет"}],
            models=["gemini-3.6-flash", "@cf/qwen/qwen3-30b-a3b-fp8"],
        )

        assert "Тестовый гайд" in text
        assert model == "@cf/qwen/qwen3-30b-a3b-fp8"
        assert server.last_request["model"] == "@cf/qwen/qwen3-30b-a3b-fp8"

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
        monkeypatch.setenv("AI_MODEL_PRIORITY", "gemini-3.6-flash, @cf/qwen/qwen3-30b-a3b-fp8")
        assert ai_client.get_model_priority() == ["gemini-3.6-flash", "@cf/qwen/qwen3-30b-a3b-fp8"]

    def test_model_priority_default(self, monkeypatch):
        monkeypatch.delenv("AI_MODEL_PRIORITY", raising=False)
        models = ai_client.get_model_priority()
        assert models == ["gemini-3.6-flash"]
        assert not any(model.startswith("@cf/") for model in models)

    def test_default_models_are_gemini_only(self):
        """Рабочий сценарий — только Gemini через Worker, без @cf и без g4f."""
        assert ai_client.DEFAULT_MODELS == ["gemini-3.6-flash"]
        assert not any(model.startswith("@cf/") for model in ai_client.DEFAULT_MODELS)
        assert not hasattr(ai_client, "_ask_g4f")

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


# ═══════════════════════════════════════════════════════════════════════
# 5. Только Gemini через Worker
# ═══════════════════════════════════════════════════════════════════════

class TestGeminiOnly:

    def test_gemini_priority_calls_only_gemini(self, monkeypatch, worker_url):
        """AI_MODEL_PRIORITY=gemini-3.6-flash не должен звать @cf и g4f."""
        url, server = worker_url
        server.behaviour = "ok"
        server.requested_models = []
        monkeypatch.setenv("AI_MODEL_PRIORITY", "gemini-3.6-flash")
        monkeypatch.setattr(
            ai_client,
            "client",
            ai_client.AIClient(base_url=url, token="test-token", timeout=10),
        )

        text = ai_client.ai_ask([{"role": "user", "content": "привет"}])

        assert "Тестовый гайд" in text
        assert server.requested_models == ["gemini-3.6-flash"]

    def test_single_model_limit_gives_clear_error(self, monkeypatch, worker_url):
        """Единственная модель ответила лимитом — понятная ошибка, без подмены."""
        url, server = worker_url
        server.behaviour = "limit"
        server.requested_models = []
        monkeypatch.setattr(
            ai_client,
            "client",
            ai_client.AIClient(base_url=url, token="test-token", timeout=10),
        )

        with pytest.raises(ai_client.AILimitReached) as error:
            ai_client.ai_ask(
                [{"role": "user", "content": "привет"}],
                models=["gemini-3.6-flash"],
            )

        message = str(error.value)
        assert "gemini-3.6-flash" in message
        assert "лимит" in message.lower()
        assert server.requested_models == ["gemini-3.6-flash"]

    def test_missing_worker_does_not_fall_back_to_g4f(self, monkeypatch):
        monkeypatch.setattr(
            ai_client, "client", ai_client.AIClient(base_url="", token="")
        )
        with pytest.raises(ai_client.AIUnavailable) as error:
            ai_client.ai_ask([{"role": "user", "content": "привет"}])
        assert "AI_WORKER_URL" in str(error.value)


# ═══════════════════════════════════════════════════════════════════════
# 6. Worker должен знать модель из AI_MODEL_PRIORITY
# ═══════════════════════════════════════════════════════════════════════

class TestModelSupport:

    def test_supported_models_from_worker(self, client):
        models = client.supported_models()
        assert "gemini-2.5-flash" in models
        assert "@cf/qwen/qwen3-30b-a3b-fp8" in models

    def test_supported_models_falls_back_to_health(self, client, worker_url):
        """Старый Worker без /v1/models: список берём из /health."""
        url, server = worker_url
        server.behaviour = "no_models"
        models = client.supported_models()
        assert models == ["@cf/openai/gpt-oss-120b"]

    def test_supported_models_empty_without_url(self):
        assert ai_client.AIClient(base_url="").supported_models() == []

    def test_check_model_support_detects_outdated_worker(self, monkeypatch, worker_url):
        """Именно эта ситуация и сломала запуск: Worker v1.0 не знает gemini-3.6-flash."""
        url, server = worker_url
        server.behaviour = "ok"
        monkeypatch.setattr(
            ai_client, "client", ai_client.AIClient(base_url=url, token="test-token", timeout=10)
        )

        missing, available = ai_client.check_model_support(["gemini-3.6-flash"])

        assert missing == ["gemini-3.6-flash"]
        assert "gemini-2.5-flash" in available

    def test_check_model_support_accepts_known_model(self, monkeypatch, worker_url):
        url, server = worker_url
        server.behaviour = "ok"
        monkeypatch.setattr(
            ai_client, "client", ai_client.AIClient(base_url=url, token="test-token", timeout=10)
        )

        missing, available = ai_client.check_model_support(["gemini-2.5-flash"])

        assert missing == []
        assert available

    def test_check_model_support_silent_when_worker_hides_list(self, monkeypatch, worker_url):
        """Worker не сообщает список — не пугаем пользователя зря."""
        url, server = worker_url
        server.behaviour = "no_models"
        monkeypatch.setattr(
            ai_client,
            "client",
            ai_client.AIClient(base_url=url, token="test-token", timeout=10),
        )

        class _NoHealthModels:
            def supported_models(self):
                return []

        monkeypatch.setattr(ai_client, "client", _NoHealthModels())
        missing, available = ai_client.check_model_support(["gemini-3.6-flash"])
        assert missing == []
        assert available == []

    def test_unknown_model_raises_model_not_supported(self, client, worker_url):
        url, server = worker_url
        server.behaviour = "unknown_model"
        with pytest.raises(ai_client.AIModelNotSupported) as error:
            client.chat([{"role": "user", "content": "привет"}], model="gemini-3.6-flash", attempts=1)

        assert "не поддерживается" in str(error.value)
        assert "gemini-2.5-flash" in error.value.available

    def test_unknown_model_is_subclass_of_error(self):
        assert issubclass(ai_client.AIModelNotSupported, ai_client.AIError)


class TestTimeoutIsFatal:
    """Таймаут — это недоступность Worker'а, а не повод ждать ещё 180 секунд."""

    def test_timeout_raises_unavailable_without_retry(self):
        class _TimeoutSession:
            def __init__(self):
                self.calls = 0

            def post(self, *args, **kwargs):
                self.calls += 1
                raise requests.exceptions.ReadTimeout("timed out")

        session = _TimeoutSession()
        client = ai_client.AIClient(
            base_url="http://worker.test",
            token="test-token",
            session=session,
            timeout=1,
            models=["gemini-3.6-flash"],
        )

        with pytest.raises(ai_client.AIUnavailable):
            client.chat(
                [{"role": "user", "content": "тест"}],
                model="gemini-3.6-flash",
            )

        assert session.calls == 1, "после таймаута не нужно повторять тот же запрос"


class TestAIProxy:
    """Провайдер может блокировать *.workers.dev — тогда спасает прокси."""

    def _client(self):
        return ai_client.AIClient(base_url="http://worker.test", token="test-token")

    def test_ai_proxy_url_is_applied(self, monkeypatch):
        monkeypatch.delenv("PROXY_URL", raising=False)
        monkeypatch.setenv("AI_PROXY_URL", "http://user:pass@proxy.local:8080")

        proxies = self._client().session.proxies

        assert proxies["https"] == "http://user:pass@proxy.local:8080"
        assert proxies["http"] == "http://user:pass@proxy.local:8080"

    def test_proxy_url_is_fallback(self, monkeypatch):
        monkeypatch.delenv("AI_PROXY_URL", raising=False)
        monkeypatch.setenv("PROXY_URL", "http://proxy.local:3128")

        assert self._client().session.proxies["https"] == "http://proxy.local:3128"

    def test_ai_proxy_url_wins_over_common_proxy(self, monkeypatch):
        monkeypatch.setenv("PROXY_URL", "http://funpay-proxy:3128")
        monkeypatch.setenv("AI_PROXY_URL", "http://ai-proxy:1080")

        assert self._client().session.proxies["https"] == "http://ai-proxy:1080"

    def test_without_proxy_session_goes_direct(self, monkeypatch):
        monkeypatch.delenv("AI_PROXY_URL", raising=False)
        monkeypatch.delenv("PROXY_URL", raising=False)

        assert dict(self._client().session.proxies) == {}

    def test_proxy_password_is_masked_in_logs(self):
        assert ai_client._mask_proxy("http://user:secret@proxy.local:8080") == (
            "http://***@proxy.local:8080"
        )
        assert ai_client._mask_proxy("proxy.local:8080") == "proxy.local:8080"


class _FakeResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.text = text or json.dumps(self._data)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._data


class _StreamResponse:
    """Ответ Worker'а в формате SSE."""

    def __init__(self, lines, error_after=None):
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream; charset=utf-8"}
        self.text = ""
        self._lines = lines
        self._error_after = error_after

    def json(self):
        raise ValueError("это поток, а не JSON")

    def iter_lines(self, decode_unicode=True):
        for index, line in enumerate(self._lines):
            if self._error_after is not None and index >= self._error_after:
                raise requests.exceptions.ChunkedEncodingError("поток оборвался")
            yield line
        if self._error_after is not None:
            raise requests.exceptions.ChunkedEncodingError("поток оборвался")


class _WorkerDownGeminiSession:
    """Worker рвёт соединение, Gemini API отвечает как настоящий."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/v1/chat/completions"):
            raise requests.exceptions.ConnectionError("Connection aborted")
        if url.endswith(":generateContent"):
            return self.response
        raise AssertionError(f"неожиданный адрес: {url}")


class TestDirectGeminiFallback:
    """Провайдер блокирует *.workers.dev — бот спрашивает Gemini напрямую."""

    def _client(self, session):
        return ai_client.AIClient(
            base_url="http://worker.test",
            token="test-token",
            session=session,
            timeout=5,
            models=["gemini-3.6-flash"],
        )

    def test_worker_blocked_uses_direct_gemini(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        session = _WorkerDownGeminiSession(
            _FakeResponse(
                data={
                    "candidates": [{"content": {"parts": [{"text": "Ответ напрямую"}]}}],
                    "usageMetadata": {"totalTokenCount": 42, "candidatesTokenCount": 7},
                }
            )
        )

        text = self._client(session).chat([{"role": "user", "content": "привет"}])

        assert text == "Ответ напрямую"
        urls = [url for url, _ in session.calls]
        assert urls[0].endswith("/v1/chat/completions")
        assert urls[1].endswith("/models/gemini-3.6-flash:generateContent")
        headers = session.calls[1][1]["headers"]
        assert headers["x-goog-api-key"] == "test-gemini-key"

    def test_worker_blocked_without_key_reports_unavailable(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("PROXY_URL", "http://proxy.local:3128")
        session = _WorkerDownGeminiSession(_FakeResponse())

        with pytest.raises(ai_client.AIUnavailable):
            self._client(session).chat([{"role": "user", "content": "тест"}])

        assert all(url.endswith("/v1/chat/completions") for url, _ in session.calls)

    def test_direct_works_even_without_worker_url(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        session = _WorkerDownGeminiSession(
            _FakeResponse(
                data={"candidates": [{"content": {"parts": [{"text": "Без Worker'а"}]}}]}
            )
        )
        client = ai_client.AIClient(
            base_url="",
            token="",
            session=session,
            timeout=5,
            models=["gemini-3.6-flash"],
        )

        assert client.chat([{"role": "user", "content": "тест"}]) == "Без Worker'а"
        assert len(session.calls) == 1

    def test_direct_limit_is_reported_as_limit(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        session = _WorkerDownGeminiSession(
            _FakeResponse(
                status_code=429,
                data={"error": {"code": 429, "message": "quota exceeded"}},
            )
        )

        with pytest.raises(ai_client.AILimitReached):
            self._client(session).chat([{"role": "user", "content": "тест"}])

    def test_direct_bad_key_is_unavailable(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "wrong-key")
        session = _WorkerDownGeminiSession(
            _FakeResponse(
                status_code=403,
                data={"error": {"code": 403, "message": "API key not valid"}},
            )
        )

        with pytest.raises(ai_client.AIUnavailable) as error:
            self._client(session).chat([{"role": "user", "content": "тест"}])

        assert "GEMINI_API_KEY" in str(error.value)

    def test_payload_maps_system_and_assistant_roles(self):
        payload = ai_client.AIClient._direct_payload(
            [
                {"role": "system", "content": "будь краток"},
                {"role": "user", "content": "вопрос"},
                {"role": "assistant", "content": "ответ"},
            ],
            max_tokens=500,
            temperature=0.3,
        )

        assert payload["systemInstruction"]["parts"][0]["text"] == "будь краток"
        assert [item["role"] for item in payload["contents"]] == ["user", "model"]
        assert payload["generationConfig"] == {"maxOutputTokens": 500, "temperature": 0.3}


class TestWorkingCloudflareAddress:
    """Провайдер режет часть anycast-адресов Cloudflare — бот ищет рабочий."""

    def test_pin_applies_only_to_worker_host(self, monkeypatch):
        monkeypatch.setitem(ai_client._PINNED_IPS, "worker.test", "127.0.0.1")

        pinned = socket.getaddrinfo("worker.test", 443, type=socket.SOCK_STREAM)

        assert {item[4][0] for item in pinned} == {"127.0.0.1"}
        # чужой хост резолвится как обычно
        other = socket.getaddrinfo("localhost", 80, type=socket.SOCK_STREAM)
        assert other

    def test_ensure_worker_ip_picks_first_working(self, monkeypatch):
        monkeypatch.setenv("AI_WORKER_URL", "https://worker.test")
        monkeypatch.setattr(ai_client, "_resolved_ips", lambda host: ["10.0.0.1", "10.0.0.2"])
        monkeypatch.setattr(ai_client, "_doh_ips", lambda host: ["10.0.0.3"])
        monkeypatch.setattr(ai_client, "_ip_works", lambda host, ip, timeout=4.0: ip == "10.0.0.2")
        monkeypatch.setitem(ai_client._PINNED_IPS, "worker.test", None)
        monkeypatch.delitem(ai_client._PINNED_IPS, "worker.test")

        assert ai_client.ensure_worker_ip() == "10.0.0.2"
        assert ai_client.worker_ip() == "10.0.0.2"

    def test_ensure_worker_ip_returns_none_when_all_dead(self, monkeypatch):
        monkeypatch.setenv("AI_WORKER_URL", "https://worker.test")
        monkeypatch.setattr(ai_client, "_resolved_ips", lambda host: ["10.0.0.1"])
        monkeypatch.setattr(ai_client, "_doh_ips", lambda host: [])
        monkeypatch.setattr(ai_client, "_ip_works", lambda host, ip, timeout=4.0: False)

        assert ai_client.ensure_worker_ip() is None
        assert ai_client.worker_ip() is None

    def test_switch_reports_new_address(self, monkeypatch):
        monkeypatch.setenv("AI_WORKER_URL", "https://worker.test")
        monkeypatch.setitem(ai_client._PINNED_IPS, "worker.test", "10.0.0.1")
        monkeypatch.setattr(ai_client, "ensure_worker_ip", lambda timeout=4.0: "10.0.0.9")

        assert ai_client.switch_worker_ip() is True

    def test_switch_is_false_when_address_is_the_same(self, monkeypatch):
        monkeypatch.setenv("AI_WORKER_URL", "https://worker.test")
        monkeypatch.setitem(ai_client._PINNED_IPS, "worker.test", "10.0.0.1")
        monkeypatch.setattr(ai_client, "ensure_worker_ip", lambda timeout=4.0: "10.0.0.1")

        assert ai_client.switch_worker_ip() is False

    def test_chat_retries_after_switching_address(self, monkeypatch):
        calls = []

        class _Session:
            def post(self, url, **kwargs):
                calls.append(url)
                if len(calls) == 1:
                    raise requests.exceptions.ConnectionError("сброс соединения")
                return _FakeResponse(
                    data={"choices": [{"message": {"content": "Ответ после смены IP"}}]}
                )

        monkeypatch.setenv("AI_WORKER_URL", "https://worker.test")
        monkeypatch.setattr(ai_client, "switch_worker_ip", lambda: True)
        client = ai_client.AIClient(
            base_url="https://worker.test",
            token="t",
            session=_Session(),
            timeout=5,
            models=["gemini-3.6-flash"],
        )

        assert client.chat([{"role": "user", "content": "тест"}]) == "Ответ после смены IP"
        assert len(calls) == 2


class TestStreaming:
    """Провайдер рвёт «молчащие» соединения через ~19 с — отвечаем потоком."""

    def _client(self, session):
        return ai_client.AIClient(
            base_url="http://worker.test",
            token="t",
            session=session,
            timeout=10,
            models=["gemini-3.6-flash"],
        )

    def test_streaming_answer_is_assembled(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"Привет"}}]}',
            "",
            'data: {"choices":[{"delta":{"content":", мир"}}]}',
            "",
            'data: {"choices":[],"usage":{"total_tokens":12}}',
            "data: [DONE]",
        ]
        seen = {}

        class _Session:
            def post(self, url, **kwargs):
                seen["stream"] = kwargs.get("stream")
                return _StreamResponse(lines)

        text = self._client(_Session()).chat([{"role": "user", "content": "тест"}])

        assert text == "Привет, мир"
        assert seen["stream"] is True

    def test_broken_stream_keeps_partial_text(self):
        long_part = "Длинный гайд " * 40
        lines = [f'data: {{"choices":[{{"delta":{{"content":"{long_part}"}}}}]}}']
        error_text = []

        class _Session:
            def post(self, url, **kwargs):
                return _StreamResponse(lines, error_after=1)

        import modules.ai_client as client_module

        original = client_module.logger.log_error
        client_module.logger.log_error = lambda message: error_text.append(message)
        try:
            text = self._client(_Session()).chat([{"role": "user", "content": "тест"}])
        finally:
            client_module.logger.log_error = original

        assert text.startswith("Длинный гайд")
        assert any("Поток оборвался" in message for message in error_text)

    def test_broken_stream_without_text_is_unavailable(self):
        class _Session:
            def post(self, url, **kwargs):
                return _StreamResponse([], error_after=0)

        with pytest.raises(ai_client.AIUnavailable):
            self._client(_Session()).chat([{"role": "user", "content": "тест"}])

    def test_old_worker_falls_back_to_plain_request(self):
        responses = [
            _FakeResponse(
                status_code=400,
                data={"error": {"message": "стриминг не поддерживается — убери stream или поставь false"}},
            ),
            _FakeResponse(data={"choices": [{"message": {"content": "обычный ответ"}}]}),
        ]
        calls = []

        class _Session:
            def post(self, url, **kwargs):
                calls.append(kwargs.get("stream"))
                return responses[len(calls) - 1]

        client = self._client(_Session())
        text = client.chat([{"role": "user", "content": "тест"}])

        assert text == "обычный ответ"
        assert calls == [True, False]
        assert client.use_stream is False
    def test_old_worker_falls_back_with_single_attempt(self):
        responses = [
            _FakeResponse(
                status_code=400,
                data={"error": {"message": "стриминг не поддерживается — убери stream или поставь false"}},
            ),
            _FakeResponse(data={"choices": [{"message": {"content": "ответ с одной попытки"}}]}),
        ]
        calls = []

        class _Session:
            def post(self, url, **kwargs):
                calls.append(kwargs.get("stream"))
                return responses[len(calls) - 1]

        text = self._client(_Session()).chat([{"role": "user", "content": "тест"}], attempts=1)

        assert text == "ответ с одной попытки"
        assert calls == [True, False]
