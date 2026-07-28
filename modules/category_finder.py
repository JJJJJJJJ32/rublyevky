import requests
from bs4 import BeautifulSoup
import re

def find_funpay_category(session, game_name):
    """Глубокий поиск раздела услуг или прочего."""
    print(f"   [Поиск] Шаг 1: Ищем игру '{game_name}'...")
    try:
        search_url = f"https://funpay.com/?q={game_name}"
        resp = session.get(search_url, timeout=20)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        game_url = None
        links = soup.find_all('a', href=re.compile(r'/lots/\d+/'))
        for link in links:
            t = link.get_text().lower()
            if game_name.lower() in t:
                game_url = link['href']
                if not game_url.startswith('http'): game_url = 'https://funpay.com' + game_url
                if 'game-title' in link.get('class', []): break

        if not game_url: return None

        print(f"   [Поиск] Шаг 2: Заходим на страницу игры...")
        resp = session.get(game_url, timeout=20)
        soup = BeautifulSoup(resp.text, 'html.parser')
        sub_links = soup.find_all('a', href=re.compile(r'/lots/\d+/'))
        
        for sl in sub_links:
            st = sl.get_text().lower()
            if 'услуги' in st:
                return {"id": sl['href'].split('/')[-2], "type": "Услуги"}
        for sl in sub_links:
            if 'прочее' in sl.get_text().lower() or 'other' in sl.get_text().lower():
                return {"id": sl['href'].split('/')[-2], "type": "Прочее"}

        node_id = game_url.split('/')[-2]
        return {"id": node_id, "type": "Общий раздел"} if node_id.isdigit() else None
    except: return None
