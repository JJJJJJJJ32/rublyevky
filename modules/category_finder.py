"""Поиск раздела игры на FunPay.

Важно различать два случая:
  * игры действительно нет на FunPay — возвращаем None;
  * FunPay не ответил (сеть, Cloudflare, неожиданный статус) — бросаем
    CategoryLookupError.

Разница принципиальна: во втором случае игру нельзя удалять из
data/games_list.txt, иначе один сетевой сбой стирает список игр.
"""

import re

import requests
from bs4 import BeautifulSoup

CATALOG_URL = "https://funpay.com/"
TIMEOUT = 20


class CategoryLookupError(RuntimeError):
    """FunPay не отдал каталог: сеть, Cloudflare или неожиданный статус."""


def _load(session, url):
    """Загружает страницу FunPay или бросает CategoryLookupError."""
    try:
        resp = session.get(url, timeout=TIMEOUT)
    except requests.exceptions.RequestException as error:
        raise CategoryLookupError(f"сетевая ошибка: {error}") from error

    body_low = resp.text.lower()
    if (
        resp.status_code == 403
        or "just a moment" in body_low
        or "cf-browser-verification" in body_low
    ):
        raise CategoryLookupError("Cloudflare требует проверку браузера")
    if resp.status_code != 200:
        raise CategoryLookupError(f"FunPay ответил статусом {resp.status_code}")
    return resp.text


def _find_game_url(html, game_name):
    """Ссылка на страницу игры из каталога или None, если игры в нём нет."""
    soup = BeautifulSoup(html, "html.parser")
    game_name_lower = game_name.lower()
    game_url = None

    for link in soup.find_all("a", href=re.compile(r"/lots/\d+/")):
        if game_name_lower not in link.get_text().lower():
            continue
        game_url = link["href"]
        if not game_url.startswith("http"):
            game_url = "https://funpay.com" + game_url
        if "game-title" in link.get("class", []):
            break

    return game_url


def find_funpay_category(session, game_name):
    """Возвращает раздел игры или None, если игры действительно нет на FunPay."""
    print(f"   [Поиск] Шаг 1: Ищем игру '{game_name}'...")
    catalog = _load(session, CATALOG_URL)
    if "/lots/" not in catalog:
        # 200 без ссылок на разделы — это заглушка или новая вёрстка,
        # а не «игры нет». Игру в таком случае удалять нельзя.
        raise CategoryLookupError("каталог FunPay не распознан")
    game_url = _find_game_url(catalog, game_name)
    if not game_url:
        return None

    print("   [Поиск] Шаг 2: Заходим на страницу игры...")
    soup = BeautifulSoup(_load(session, game_url), "html.parser")
    sub_links = soup.find_all("a", href=re.compile(r"/lots/\d+/"))

    for sl in sub_links:
        if "услуги" in sl.get_text().lower():
            return {"id": sl["href"].split("/")[-2], "type": "Услуги"}
    for sl in sub_links:
        if "прочее" in sl.get_text().lower() or "other" in sl.get_text().lower():
            return {"id": sl["href"].split("/")[-2], "type": "Прочее"}

    node_id = game_url.split("/")[-2]
    return {"id": node_id, "type": "Общий раздел"} if node_id.isdigit() else None
