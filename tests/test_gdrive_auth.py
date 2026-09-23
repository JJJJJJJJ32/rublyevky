"""
Тесты доступа к Google Drive (modules/gdrive_manager.py).

Главная проверка: бот должен принимать ОБА типа ключа Google —
сервисный аккаунт (как написано в README) и OAuth-клиент «Desktop app».
Раньше принимался только второй, и по инструкции бот падал с ValueError.

Запуск: .venv/bin/pytest tests/ -v
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from modules import gdrive_manager  # noqa: E402


SERVICE_ACCOUNT_JSON = {
    "type": "service_account",
    "project_id": "bot-test",
    "private_key_id": "abc123",
    "private_key": "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n",
    "client_email": "funpay-bot@bot-test.iam.gserviceaccount.com",
    "client_id": "123456789",
    "token_uri": "https://oauth2.googleapis.com/token",
}

OAUTH_DESKTOP_JSON = {
    "installed": {
        "client_id": "xxx.apps.googleusercontent.com",
        "project_id": "bot-test",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": "secret",
        "redirect_uris": ["http://localhost"],
    }
}


def _write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return str(path)


# ═══════════════════════════════════════════════════════════════════════
# 1. Определение типа ключа
# ═══════════════════════════════════════════════════════════════════════

class TestDetectAuthMode:

    def test_service_account(self, tmp_path):
        path = _write(tmp_path / "sa.json", SERVICE_ACCOUNT_JSON)
        assert gdrive_manager.detect_auth_mode(path) == "service_account"

    def test_oauth_desktop(self, tmp_path):
        path = _write(tmp_path / "oauth.json", OAUTH_DESKTOP_JSON)
        assert gdrive_manager.detect_auth_mode(path) == "oauth"

    def test_missing_file(self, tmp_path):
        assert gdrive_manager.detect_auth_mode(str(tmp_path / "нет.json")) == "missing"

    def test_broken_json(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{это не json", encoding="utf-8")
        assert gdrive_manager.detect_auth_mode(str(path)) == "unknown"

    def test_json_without_needed_fields(self, tmp_path):
        path = _write(tmp_path / "other.json", {"hello": "world"})
        assert gdrive_manager.detect_auth_mode(path) == "unknown"

    def test_default_path_is_client_secrets(self):
        assert gdrive_manager.CLIENT_SECRETS_FILE == "client_secrets.json"


# ═══════════════════════════════════════════════════════════════════════
# 2. Авторизация сервисным аккаунтом — без браузера
# ═══════════════════════════════════════════════════════════════════════

class TestServiceAccountAuth:

    def test_uses_service_account_and_never_opens_browser(self, tmp_path, monkeypatch):
        path = _write(tmp_path / "sa.json", SERVICE_ACCOUNT_JSON)
        monkeypatch.setattr(gdrive_manager, "CLIENT_SECRETS_FILE", path)

        used = {}

        class _FakeCreds:
            @staticmethod
            def from_service_account_file(filename, scopes=None):
                used["file"] = filename
                used["scopes"] = scopes
                return "creds-object"

        def _fake_build(name, version, credentials=None):
            used["build"] = (name, version, credentials)
            return "service-object"

        def _browser_flow(*args, **kwargs):
            raise AssertionError("браузер открываться не должен")

        monkeypatch.setattr(gdrive_manager, "service_account", type("m", (), {"Credentials": _FakeCreds}))
        monkeypatch.setattr(gdrive_manager, "build", _fake_build)
        monkeypatch.setattr(gdrive_manager, "InstalledAppFlow", _browser_flow)

        manager = gdrive_manager.GoogleDriveManager()
        service = manager._authenticate()

        assert service == "service-object"
        assert manager.auth_mode == "service_account"
        assert used["file"] == path
        assert "drive" in used["scopes"][0]

    def test_clear_error_when_service_account_broken(self, tmp_path, monkeypatch, capsys):
        path = _write(tmp_path / "sa.json", SERVICE_ACCOUNT_JSON)
        monkeypatch.setattr(gdrive_manager, "CLIENT_SECRETS_FILE", path)

        class _BrokenCreds:
            @staticmethod
            def from_service_account_file(filename, scopes=None):
                raise ValueError("private_key is invalid")

        monkeypatch.setattr(gdrive_manager, "service_account", type("m", (), {"Credentials": _BrokenCreds}))

        manager = gdrive_manager.GoogleDriveManager()
        assert manager._authenticate() is None
        assert "private_key is invalid" in capsys.readouterr().out


# ═══════════════════════════════════════════════════════════════════════
# 3. Понятные сообщения вместо падений
# ═══════════════════════════════════════════════════════════════════════

class TestClearMessages:

    def test_missing_file_explains_what_to_do(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(gdrive_manager, "CLIENT_SECRETS_FILE", str(tmp_path / "нет.json"))
        manager = gdrive_manager.GoogleDriveManager()
        assert manager._authenticate() is None
        out = capsys.readouterr().out
        assert "не найден" in out          # говорим, что файла нет
        assert "Google Cloud" in out       # и куда идти за ключом
        assert "main.py" in out            # и куда его положить

    def test_unknown_file_explains_both_options(self, tmp_path, monkeypatch, capsys):
        path = _write(tmp_path / "other.json", {"hello": "world"})
        monkeypatch.setattr(gdrive_manager, "CLIENT_SECRETS_FILE", path)
        manager = gdrive_manager.GoogleDriveManager()
        assert manager._authenticate() is None
        out = capsys.readouterr().out
        assert "Service Account" in out and "Desktop app" in out

    def test_self_test_without_folder_id(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_DRIVE_FOLDER_ID", raising=False)
        ok, message = gdrive_manager.GoogleDriveManager().self_test()
        assert ok is False
        assert "GOOGLE_DRIVE_FOLDER_ID" in message

    def test_self_test_without_key_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "1abc")
        monkeypatch.setattr(gdrive_manager, "CLIENT_SECRETS_FILE", str(tmp_path / "нет.json"))
        ok, message = gdrive_manager.GoogleDriveManager().self_test()
        assert ok is False
        assert "нет файла" in message
        assert "ключ Google Cloud" in message

    def test_self_test_reports_missing_folder_with_hint(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "1abc")
        monkeypatch.setattr(gdrive_manager, "CLIENT_SECRETS_FILE", "client_secrets.json")
        monkeypatch.setattr(gdrive_manager, "detect_auth_mode", lambda path=None: "service_account")

        class _Files:
            def create(self, **kwargs):
                raise RuntimeError("File not found: 1abc")

        class _Service:
            def files(self):
                return _Files()

        manager = gdrive_manager.GoogleDriveManager()
        monkeypatch.setattr(manager, "_authenticate", lambda: _Service())
        ok, message = manager.self_test()
        assert ok is False
        assert "поделись" in message or "client_email" in message
