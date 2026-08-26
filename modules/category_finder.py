from urllib.parse import urljoin

from bs4 import BeautifulSoup
import re


_BASE_URL = "https://funpay.com"
_LOTS_LINK_RE = re.compile(r"/lots/(\d+)/")


def _normalise(value):
    return " ".join(str(value).casefold().split())


def _lot_id(href):
    match = _LOTS_LINK_RE.search(href or "")
    return match.group(1) if match else None


def find_funpay_category(session, game_name, known_id=None):
    """Ищет на FunPay раздел «Услуги» или «Прочее» для игры.

    ``known_id`` позволяет использовать ID из строки ``название|id`` и не
    зависеть от поисковой выдачи FunPay.
    """
    if known_id and str(known_id).strip().isdigit():
        return {"id": str(known_id).strip(), "type": "Указанный раздел"}

    print(f"   [Поиск] Шаг 1: Ищем игру '{game_name}'...")
    try:
        # params корректно кодирует пробелы, &, :, кириллицу и другие символы.
        resp = session.get(_BASE_URL + "/", params={"q": game_name}, timeout=20)
        if resp.status_code >= 400:
            print(f"   [Поиск] FunPay вернул HTTP {resp.status_code}.")
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')
        candidates = []
        wanted = _normalise(game_name)
        for link in soup.find_all('a', href=_LOTS_LINK_RE):
            node_id = _lot_id(link.get('href'))
            if not node_id:
                continue
            text = _normalise(link.get_text(" ", strip=True))
            classes = set(link.get('class', []))
            score = 0
            if text == wanted:
                score = 3
            elif 'game-title' in classes:
                score = 2
            elif wanted in text:
                score = 1
            if score:
                candidates.append((score, urljoin(_BASE_URL, link['href']), node_id))

        if not candidates:
            return None

        # Сначала предпочитаем точное название, а не похожую игру
        _, game_url, fallback_id = max(candidates, key=lambda item: item[0])

        print("   [Поиск] Шаг 2: Заходим на страницу игры...")
        resp = session.get(game_url, timeout=20)
        if resp.status_code >= 400:
            print(f"   [Поиск] Страница игры вернула HTTP {resp.status_code}.")
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')
        sub_links = soup.find_all('a', href=_LOTS_LINK_RE)

        for sl in sub_links:
            node_id = _lot_id(sl.get('href'))
            text = _normalise(sl.get_text(" ", strip=True))
            if node_id and 'услуги' in text:
                return {"id": node_id, "type": "Услуги"}
        for sl in sub_links:
            node_id = _lot_id(sl.get('href'))
            text = _normalise(sl.get_text(" ", strip=True))
            if node_id and ('прочее' in text or 'other' in text):
                return {"id": node_id, "type": "Прочее"}

        return {"id": fallback_id, "type": "Общий раздел"}
    except Exception as e:
        print(f"   [Поиск] Ошибка поиска категории: {e}")
        return None
