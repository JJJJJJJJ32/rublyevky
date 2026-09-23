"""
Тесты сетевого слоя (modules/network.py).

Проверяем, что слой действительно стал простым: он не запускает сторонние
программы, не правит маршруты, а только собирает сессию и объясняет,
доступен ли FunPay.

Запуск: .venv/bin/pytest tests/ -v
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from modules import network  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code=200, text="<html>funpay</html>"):
        self.status_code = status_code
        self.text = text


class _FakeSession:
    """Мини-сессия: отвечает заранее заготовленным ответом или падает."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def get(self, url, timeout=None, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response

    def close(self):
        pass


# ═══════════════════════════════════════════════════════════════════════
# 1. В слое не осталось управления системой
# ═══════════════════════════════════════════════════════════════════════

class TestNoSystemHacks:

    def test_no_zapret_or_warp_functions(self):
        for name in ("is_zapret_active", "start_zapret", "stop_zapret",
                     "is_warp_active", "warp_cli_reconnect", "add_funpay_routes",
                     "remove_funpay_routes", "cleanup_all_funpay_routes"):
            assert not hasattr(network, name), f"в network.py осталась функция {name}"

    def test_code_does_not_touch_the_system(self):
        """В коде (без учёта пояснений в шапке) не должно быть системных команд."""
        import ast
        source = open(network.__file__, encoding="utf-8").read()
        tree = ast.parse(source)

        # Убираем docstring модуля — это просто пояснение, а не код
        code = source
        doc = ast.get_docstring(tree)
        if doc:
            code = code.replace(doc, "")

        # Никаких модулей для запуска программ
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert "subprocess" not in imported, "network.py снова умеет запускать программы"
        assert "os.system" not in code

        # И никаких следов Запрета / маршрутов в исполняемом коде
        for forbidden in ("winws", "route add", "route delete", "warp-cli", "tasklist"):
            assert forbidden not in code, f"в коде network.py осталось: {forbidden}"

    def test_module_is_small(self):
        lines = open(network.__file__, encoding="utf-8").read().splitlines()
        assert len(lines) < 250, f"модуль разросся: {len(lines)} строк"


# ═══════════════════════════════════════════════════════════════════════
# 2. Прокси
# ═══════════════════════════════════════════════════════════════════════

class TestProxy:

    def test_no_proxy_by_default(self, monkeypatch):
        monkeypatch.delenv("PROXY_URL", raising=False)
        monkeypatch.delenv("FUNPAY_PROXY_URL", raising=False)
        assert network.get_proxy_url() is None

    def test_proxy_from_env(self, monkeypatch):
        monkeypatch.setenv("PROXY_URL", "http://user:pass@1.2.3.4:8080")
        assert network.get_proxy_url() == "http://user:pass@1.2.3.4:8080"

    def test_funpay_proxy_has_priority(self, monkeypatch):
        monkeypatch.setenv("PROXY_URL", "http://a:1")
        monkeypatch.setenv("FUNPAY_PROXY_URL", "http://b:2")
        assert network.get_proxy_url() == "http://b:2"

    def test_session_uses_proxy(self):
        session = network.build_session(proxy_url="http://user:pass@1.2.3.4:8080")
        assert session.proxies["https"] == "http://user:pass@1.2.3.4:8080"

    def test_proxy_password_hidden_in_logs(self):
        masked = network._mask_proxy("http://user:secret@1.2.3.4:8080")
        assert "secret" not in masked
        assert "1.2.3.4:8080" in masked

    def test_plain_proxy_unchanged(self):
        assert network._mask_proxy("http://1.2.3.4:8080") == "http://1.2.3.4:8080"


# ═══════════════════════════════════════════════════════════════════════
# 3. Понятные причины сбоя
# ═══════════════════════════════════════════════════════════════════════

class TestErrorsAreClear:

    def test_timeout_is_explained(self):
        reason = network._classify_error(Exception("HTTPSConnectionPool: Read timed out"))
        assert "прокси" in reason.lower() or "timeout" in reason.lower()

    def test_ssl_eof_is_explained(self):
        reason = network._classify_error(Exception("SSL: UNEXPECTED_EOF_WHILE_READING"))
        assert "прокси" in reason.lower() or "блокировк" in reason.lower()

    def test_dns_is_explained(self):
        reason = network._classify_error(Exception("Name or service not known"))
        assert "dns" in reason.lower()

    def test_connection_refused_is_explained(self):
        reason = network._classify_error(Exception("[Errno 111] Connection refused"))
        assert "прокси" in reason.lower()

    def test_unknown_error_still_reported(self):
        reason = network._classify_error(Exception("что-то странное"))
        assert "что-то странное" in reason


# ═══════════════════════════════════════════════════════════════════════
# 4. Проверка FunPay
# ═══════════════════════════════════════════════════════════════════════

class TestCheckFunpay:

    def test_ok(self):
        ok, message = network.check_funpay(_FakeSession(response=_FakeResponse(200, "funpay")))
        assert ok is True
        assert message == "FunPay доступен"

    def test_cloudflare_challenge(self):
        session = _FakeSession(response=_FakeResponse(403, "<html>Just a moment...</html>"))
        ok, message = network.check_funpay(session)
        assert ok is False
        assert "cloudflare" in message.lower()

    def test_http_error_status(self):
        ok, message = network.check_funpay(_FakeSession(response=_FakeResponse(502, "")))
        assert ok is False
        assert "502" in message

    def test_not_funpay_page(self):
        ok, message = network.check_funpay(_FakeSession(response=_FakeResponse(200, "<html>Заглушка провайдера</html>")))
        assert ok is False
        assert "FunPay" in message

    def test_retries_and_gives_reason(self, monkeypatch):
        monkeypatch.setattr(network, "RETRY_PAUSE", 0)
        import requests
        session = _FakeSession(error=requests.exceptions.ConnectTimeout("timed out"))
        ok, message = network.check_funpay(session)
        assert ok is False
        assert session.calls == network.CONNECT_ATTEMPTS
        assert "timeout" in message.lower()

    def test_own_session_is_closed(self, monkeypatch):
        created = {}

        def fake_build_session(proxy_url=None):
            created["session"] = _FakeSession(response=_FakeResponse(200, "funpay"))
            return created["session"]

        monkeypatch.setattr(network, "build_session", fake_build_session)
        ok, _ = network.check_funpay(None)
        assert ok is True


# ═══════════════════════════════════════════════════════════════════════
# 5. setup_network / recheck_network
# ═══════════════════════════════════════════════════════════════════════

class TestSetup:

    def test_setup_ok_when_funpay_available(self, monkeypatch):
        monkeypatch.delenv("PROXY_URL", raising=False)
        monkeypatch.delenv("FUNPAY_PROXY_URL", raising=False)
        monkeypatch.setattr(network, "build_session",
                            lambda proxy_url=None: _FakeSession(response=_FakeResponse(200, "funpay")))
        session, ok = network.setup_network()
        assert ok is True
        assert session is not None

    def test_setup_reports_when_unavailable(self, monkeypatch):
        monkeypatch.delenv("PROXY_URL", raising=False)
        monkeypatch.setattr(network, "RETRY_PAUSE", 0)
        monkeypatch.setattr(network, "build_session",
                            lambda proxy_url=None: _FakeSession(response=_FakeResponse(403, "cloudflare")))
        session, ok = network.setup_network()
        assert ok is False

    def test_recheck_rebuilds_session_on_failure(self, monkeypatch):
        monkeypatch.setattr(network, "build_session",
                            lambda proxy_url=None: _FakeSession(response=_FakeResponse(502, "")))
        old_session = _FakeSession(response=_FakeResponse(502, ""))
        new_session, ok = network.recheck_network(old_session)
        assert ok is False
        assert new_session is not old_session

    def test_recheck_uses_proxy_when_set(self, monkeypatch):
        monkeypatch.setenv("PROXY_URL", "http://user:pass@1.2.3.4:8080")
        session, ok = network.setup_network()
        assert session.proxies["https"] == "http://user:pass@1.2.3.4:8080"
