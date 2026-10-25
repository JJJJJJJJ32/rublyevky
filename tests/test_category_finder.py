"""Поиск раздела FunPay: «игры нет» и «FunPay не ответил» — разные вещи."""

import sys
import os

import requests
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.category_finder import (  # noqa: E402
    CATALOG_URL,
    CategoryLookupError,
    find_funpay_category,
)


class _Response:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


class _Session:
    """Отдаёт заранее заданные страницы или бросает заданное исключение."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return page


def _game_page(title, game_url="/lots/1805/"):
    return _Response(f'<a class="game-title" href="{game_url}">{title}</a>')


class TestFindCategory:
    def test_returns_services_section(self):
        session = _Session({
            CATALOG_URL: _game_page("Baldur's Gate 3"),
            "https://funpay.com/lots/1805/": _Response(
                '<a href="/lots/1805/">Baldur\'s Gate 3</a>'
                '<a href="/lots/1806/">Услуги</a>'
            ),
        })

        assert find_funpay_category(session, "Baldur's Gate 3") == {
            "id": "1806",
            "type": "Услуги",
        }

    def test_returns_other_section(self):
        session = _Session({
            CATALOG_URL: _game_page("Baldur's Gate 3"),
            "https://funpay.com/lots/1805/": _Response(
                '<a href="/lots/1807/">Прочее</a>'
            ),
        })

        assert find_funpay_category(session, "Baldur's Gate 3") == {
            "id": "1807",
            "type": "Прочее",
        }

    def test_falls_back_to_game_section(self):
        session = _Session({
            CATALOG_URL: _game_page("Baldur's Gate 3"),
            "https://funpay.com/lots/1805/": _Response("<p>без подразделов</p>"),
        })

        assert find_funpay_category(session, "Baldur's Gate 3") == {
            "id": "1805",
            "type": "Общий раздел",
        }

    def test_absent_game_returns_none(self):
        session = _Session({CATALOG_URL: _game_page("Baldur's Gate 3")})

        assert find_funpay_category(session, "Apex Legends") is None
        # Страницу игры даже не запрашиваем — игры нет в каталоге.
        assert session.requested == [CATALOG_URL]


class TestNetworkFailureIsNotAbsence:
    def test_timeout_raises_lookup_error(self):
        session = _Session({
            CATALOG_URL: requests.exceptions.ConnectTimeout("timed out"),
        })

        with pytest.raises(CategoryLookupError):
            find_funpay_category(session, "Apex Legends")

    def test_connection_error_raises_lookup_error(self):
        session = _Session({
            CATALOG_URL: requests.exceptions.ConnectionError("reset"),
        })

        with pytest.raises(CategoryLookupError):
            find_funpay_category(session, "Apex Legends")

    def test_http_500_raises_lookup_error(self):
        session = _Session({CATALOG_URL: _Response("server error", 500)})

        with pytest.raises(CategoryLookupError):
            find_funpay_category(session, "Apex Legends")

    def test_cloudflare_challenge_raises_lookup_error(self):
        session = _Session({
            CATALOG_URL: _Response(
                "<html><title>Just a moment...</title>"
                '<div id="cf-browser-verification"></div></html>'
            ),
        })

        with pytest.raises(CategoryLookupError):
            find_funpay_category(session, "Apex Legends")

    def test_unrecognized_catalog_raises_lookup_error(self):
        """200 без ссылок на разделы — заглушка или новая вёрстка, не «игры нет»."""
        session = _Session({
            CATALOG_URL: _Response("<html><body>ничего нет</body></html>"),
        })

        with pytest.raises(CategoryLookupError):
            find_funpay_category(session, "Apex Legends")

    def test_game_page_failure_raises_lookup_error(self):
        session = _Session({
            CATALOG_URL: _game_page("Baldur's Gate 3"),
            "https://funpay.com/lots/1805/": requests.exceptions.ReadTimeout("timed out"),
        })

        with pytest.raises(CategoryLookupError):
            find_funpay_category(session, "Baldur's Gate 3")
