import requests
from bs4 import BeautifulSoup
import json
import re
import string
from config import LIMITS
from modules.ai_generator import ai_translate_to_en

def truncate_to_bytes(text, max_bytes):
    """Обрезает текст до max_bytes в UTF-8, не разрывая многобайтовые символы
    и не отрывая variation selector (U+FE0F) от базового символа."""
    encoded = text.encode('utf-8')
    if len(encoded) <= max_bytes:
        return text
    # Собираем "кластеры" символов: базовый + variation selector вместе
    clusters = []
    i = 0
    while i < len(text):
        char = text[i]
        cluster = char
        # Если следующий символ — variation selector (U+FE0F), объединяем
        while i + 1 < len(text) and text[i + 1] == '\uFE0F':
            i += 1
            cluster += text[i]
        clusters.append(cluster)
        i += 1
    # Теперь собираем результат по кластерам
    result = ""
    for cluster in clusters:
        if len((result + cluster).encode('utf-8')) > max_bytes:
            break
        result += cluster
    return result

def enforce_limits(text, min_len, max_len, is_en=False, single_line=False, max_bytes=0):
    if not text: text = "Premium Gaming Service"
    if is_en:
        allowed = string.ascii_letters + string.digits + string.punctuation + " \n\r\t"
        text = "".join(c for c in text if c in allowed)
    if single_line:
        text = text.replace("\n", " ").replace("\r", " ").strip()
        text = re.sub(r'\s+', ' ', text)
    if len(text) < min_len:
        f_ru = " Данный товар содержит проверенную информацию и экспертные тактики. Мы гарантируем 24/7 моментальную выдачу."
        f_en = " This professional gaming service provides elite strategies and verified tips. We ensure instant automated delivery 24/7 with full support. Buy with confidence - thousands of satisfied customers trust our guides. Fast response guaranteed."
        while len(text) < min_len: text += (f_en if is_en else f_ru)
    text = text[:max_len]
    # Байтовый лимит (FunPay считает байты для некоторых полей)
    if max_bytes > 0:
        text = truncate_to_bytes(text, max_bytes)
    return text

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
        # Нет реальных вариантов — отправляем пустое значение, не выдумывая ID.
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
        # Английский summary: одна строка, байтовый лимит
        s_en = enforce_limits(ai_translate_to_en(short_description), LIMITS['summary_min'], LIMITS['summary_max'], is_en=True, single_line=True, max_bytes=LIMITS['summary_max_bytes'])
        # Русский summary: тоже обрезаем по байтам
        s_ru = enforce_limits(short_description, LIMITS['summary_min'], LIMITS['summary_max'], is_en=False, single_line=True, max_bytes=LIMITS['summary_max_bytes'])
        # Английский description: минимум 500 символов
        f_en = enforce_limits(ai_translate_to_en(full_description), LIMITS['description_min_en'], 3000, is_en=True)
        # Русский description: тоже соблюдаем лимиты FunPay.
        f_ru = enforce_limits(
            full_description,
            LIMITS['description_min_ru'],
            LIMITS.get('description_max_ru', 3000),
        )
        # Английский payment_msg: минимум 300 символов
        p_en = enforce_limits(ai_translate_to_en(payment_message), LIMITS.get('payment_msg_min_en', 300), LIMITS['payment_msg_max'], is_en=True)
        
        resp = session.get(edit_url, timeout=25)

        # Проверка Cloudflare бана
        response_text = resp.text or ""
        response_lower = response_text.lower()
        if (
            resp.status_code == 403
            or "cf-browser-verification" in response_lower
            or "just a moment" in response_lower
            or ("cloudflare" in response_lower and len(response_text) < 5000)
        ):
            print("      🚫 Cloudflare заблокировал IP. Требуется пауза.")
            return "cloudflare_banned"
        if resp.status_code >= 500:
            print(f"      ❌ FunPay временно недоступен (HTTP {resp.status_code}).")
            return "network_error"
        if resp.status_code >= 400:
            print(f"      ❌ FunPay вернул HTTP {resp.status_code}.")
            return "no_csrf"

        soup = BeautifulSoup(response_text, 'html.parser')

        body = soup.find('body')
        csrf = None
        if body and body.has_attr('data-app-data'):
            try:
                app_data = json.loads(body.get('data-app-data') or '{}')
                csrf = app_data.get('csrf-token')
            except (TypeError, ValueError):
                print("      ⚠️ Не удалось разобрать данные страницы FunPay.")
        if not csrf:
            inp = soup.find('input', {'name': 'csrf_token'})
            if inp:
                csrf = inp.get('value')
        
        if not csrf:
            print("      ❌ CSRF не найден — страница формы недоступна")
            return "no_csrf"

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
            if 'summary' in low: payload[name] = s_en if '[en]' in low else s_ru
            elif 'desc' in low: payload[name] = f_en if '[en]' in low else f_ru
            elif 'payment_msg' in low: payload[name] = p_en if '[en]' in low else payment_message
            elif 'quantity' in low or 'amount' in low: payload[name] = "999"

        response = session.post(
            save_url,
            data=payload,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": edit_url},
            timeout=30,
        )
        post_text = response.text or ""
        post_lower = post_text.lower()
        if (
            response.status_code == 403
            or "cf-browser-verification" in post_lower
            or "just a moment" in post_lower
            or ("cloudflare" in post_lower and len(post_text) < 5000)
        ):
            print("      🚫 Cloudflare заблокировал IP при сохранении.")
            return "cloudflare_banned"
        if response.status_code >= 500:
            return "network_error"
        try:
            result = response.json()
        except ValueError:
            print(f"      ❌ FunPay вернул не JSON (HTTP {response.status_code}).")
            return "no_csrf"
        if result.get('done'):
            print("      ✅ УСПЕШНО ВЫСТАВЛЕНО!")
            return "success"
        else:
            errors = result.get('errors')
            print(f"      ❌ ОТКАЗ: {errors}")
            # Проверяем ошибку «Много предложений» — значит лимит категории исчерпан
            if errors:
                err_str = str(errors).lower()
                if any(kw in err_str for kw in ['много предложений', 'много лотов', 'too many', 'удалите ненужные', 'limit']):
                    print("      ⚠️ Лимит лотов в категории исчерпан — переходим к следующей игре.")
                    return "limit_reached"
                # Короткий английский текст — можно попробовать перезаполнить
                if 'короткого английского' in err_str or 'too short' in err_str:
                    return "too_short_en"
                # Длинный русский текст — можно попробовать обрезать
                if 'слишком длинный' in err_str or 'too long' in err_str:
                    return "too_long_ru"
            return None
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        print(f"      ❌ ОШИБКА СЕТИ: {e}")
        return "network_error"
    except requests.exceptions.RequestException as e:
        print(f"      ❌ ОШИБКА HTTP: {e}")
        return "network_error"
    except Exception as e:
        print(f"      ❌ ОШИБКА publish_lot: {e}")
        return None
