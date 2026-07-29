import requests
from bs4 import BeautifulSoup
import json
import re
import string
from config import LIMITS
from modules.ai_generator import ai_translate_to_en

def enforce_limits(text, min_len, max_len, is_en=False, single_line=False):
    if not text: text = "Premium Gaming Service"
    if is_en:
        allowed = string.ascii_letters + string.digits + string.punctuation + " \n\r\t"
        text = "".join(c for c in text if c in allowed)
    if single_line:
        text = text.replace("\n", " ").replace("\r", " ").strip()
        text = re.sub(r'\s+', ' ', text)
    if len(text) < min_len:
        f_ru = " Данный товар содержит проверенную информацию и экспертные тактики. Мы гарантируем 24/7 моментальную выдачу."
        f_en = " This professional gaming service provides elite strategies and verified tips. We ensure instant automated delivery 24/7."
        while len(text) < min_len: text += (f_en if is_en else f_ru)
    return text[:max_len]

def get_category_info(session, node_id):
    try:
        url = f"https://funpay.com/lots/offerEdit?node={node_id}"
        resp = session.get(url, timeout=20)
        soup = BeautifulSoup(resp.text, 'html.parser')
        h1 = soup.find('h1')
        return h1.get_text().replace("Продать ", "").strip() if h1 else f"Node {node_id}"
    except: return f"Node {node_id}"

def _fill_select(payload, name, select_tag):
    """
    Умное заполнение выпадающего списка <select>.
    Пропускает пустые плейсхолдеры (value="") и выбирает первый реальный option.
    Для server_id — предпочитает вариант с текстом «PC» / «Все серверы» / «Все».
    """
    options = select_tag.find_all('option')
    # Собираем все непустые варианты: (value, text)
    non_empty = []
    for opt in options:
        val = opt.get('value', '')
        text = opt.get_text(strip=True)
        if val:  # value непустой — это реальный вариант, а не плейсхолдер
            non_empty.append((val, text))

    if not non_empty:
        # Все option пустые — берём первый, какой есть
        for opt in options:
            val = opt.get('value', '')
            if val:
                payload[name] = val
                return
        # Вообще ничего нет — ставим пустую строку
        payload[name] = ''
        return

    # Для server_id — ищем лучший вариант
    low = name.lower()
    if 'server' in low:
        preferred_keywords = ['pc', 'все серверы', 'все', 'all', 'all servers', 'любой']
        for val, text in non_empty:
            if text.lower() in preferred_keywords:
                payload[name] = val
                return
        # Если нет предпочтительного — берём первый непустой
        payload[name] = non_empty[0][0]
        return

    # Для всех остальных select — первый непустой option
    payload[name] = non_empty[0][0]


def publish_lot(session, game_data, short_description, full_description, payment_message, price):
    try:
        node_id = str(game_data['game_id'])
        edit_url = f"https://funpay.com/lots/offerEdit?node={node_id}"
        save_url = "https://funpay.com/lots/offerSave"
        
        # Переводы и лимиты
        s_en = enforce_limits(ai_translate_to_en(short_description), LIMITS['summary_min'], LIMITS['summary_max'], is_en=True, single_line=True)
        f_en = enforce_limits(ai_translate_to_en(full_description), LIMITS['description_min_en'], 3000, is_en=True)
        p_en = enforce_limits(ai_translate_to_en(payment_message), 1, 1000, is_en=True)
        
        resp = session.get(edit_url, timeout=25)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        body = soup.find('body')
        csrf = None
        if body and body.has_attr('data-app-data'):
            csrf = json.loads(body.get('data-app-data')).get('csrf-token')
        if not csrf:
            inp = soup.find('input', {'name': 'csrf_token'})
            if inp: csrf = inp.get('value')
        
        if not csrf: return None

        payload = {
            "node_id": node_id, "offer_id": "0", "location": "shop",
            "price": str(price), "amount": "999", "active": "1", "csrf_token": csrf
        }

        for item in soup.find_all(['input', 'textarea', 'select']):
            name = item.get('name')
            if not name or name in payload: continue

            # <select> — всегда обрабатываем, даже если нет "fields" в имени
            if item.name == 'select':
                _fill_select(payload, name, item)
                continue

            # <input type="hidden"> — всегда включаем значение
            if item.get('type') == 'hidden':
                payload[name] = item.get('value', '')
                continue

            # Текстовые поля — только с "fields" в имени
            if 'fields' not in name: continue
            low = name.lower()
            if 'summary' in low: payload[name] = s_en if '[en]' in low else short_description[:100]
            elif 'desc' in low: payload[name] = f_en if '[en]' in low else full_description
            elif 'payment_msg' in low: payload[name] = p_en if '[en]' in low else payment_message
            elif 'quantity' in low or 'amount' in low: payload[name] = "999"

        response = session.post(save_url, data=payload, headers={"X-Requested-With": "XMLHttpRequest", "Referer": edit_url}, timeout=30)
        result = response.json()
        if result.get('done'):
            print(f"      ✅ УСПЕШНО ВЫСТАВЛЕНО!")
            return "success"
        else:
            errors = result.get('errors')
            print(f"      ❌ ОТКАЗ: {errors}")
            # Проверяем ошибку «Много предложений» — значит лимит категории исчерпан
            if errors:
                for err in errors:
                    err_text = str(err)
                    if 'много предложений' in err_text.lower() or 'много лотов' in err_text.lower() or 'too many' in err_text.lower() or 'удалите ненужные' in err_text.lower():
                        print(f"      ⚠️ Лимит лотов в категории исчерпан — пропускаем оставшиеся товары.")
                        return "limit_reached"
            return None
    except: return None
