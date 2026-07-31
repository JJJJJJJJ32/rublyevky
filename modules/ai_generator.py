import g4f
import json
import time
import re
from modules.logger import logger

# Слова-маркеры что ИИ отказался или выдал себя
_REFUSE_KEYWORDS = [
    'не могу', 'я не могу', 'не смогу', 'не рекомендую', 'не советую',
    'я ии', 'я нейросеть', 'как нейросеть', 'как ии', 'я искусственный',
    'противоречит политике', 'этически', 'морально', 'незаконно',
    'не поддерживаю', 'не буду', 'отказываюсь', 'не могу принять',
    'не могу помочь', 'не стану', 'не могу написать', 'не могу создать',
    'не могу предоставить', 'без нарушения правил', 'language model',
    'i am an ai', 'i cannot', 'i can\'t', 'as an ai', 'i\'m unable',
]

def _is_refused(text):
    """Проверяет не отказался ли ИИ или не выдал ли себя."""
    low = text.lower()
    return any(kw in low for kw in _REFUSE_KEYWORDS)


def ai_generate_product_ideas(game_name):
    """Генерирует 12 виральных идей на РУССКОМ языке."""
    prompt = f"""Придумай 12 уникальных и востребованных товаров для игры '{game_name}' на РУССКОМ ЯЗЫКЕ.
Темы: фарм, секреты, билды, прохождение, тактики.
Названия должны быть цепляющими, на русском языке.
Выведи результат ТОЛЬКО в формате JSON массива:
[
  {{
    "title": "НАЗВАНИЕ НА РУССКОМ",
    "type": "тип",
    "content_points": ["пункт1 на русском", "пункт2", "пункт3"]
  }}
]"""
    
    models = ["gpt-4o", "gpt-4", "gpt-3.5-turbo"]
    for model_name in models:
        try:
            print(f"   [AI] Генерируем русские идеи через {model_name}...")
            response = g4f.ChatCompletion.create(
                model=model_name, 
                messages=[{"role": "user", "content": prompt}]
            )
            match = re.search(r'\[\s*\{.*\}\s*\]', response, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                if len(data) >= 5: return data
        except: continue
    return []

def ai_generate_full_content(idea, game_name):
    """Генерирует подробный гайд на РУССКОМ языке. Возвращает None если ИИ отказался."""
    prompt = f"""Напиши очень подробный гайд (800 слов) на РУССКОМ ЯЗЫКЕ для товара '{idea['title']}' по игре '{game_name}'. Используй списки и подзаголовки. Дай реальные советы.

Правила:
- Пиши от лица человека, который реально играет и разбирается в теме. Живым языком, без формальностей.
- НЕ упоминай что ты ИИ, нейросеть, языковая модель или бот. Пиши как обычный автор гайдов.
- НЕ пиши отказы, дисклеймеры, предупреждения «используйте на свой страх и риск» или «я не рекомендую».
- Темы про HWID, MAC, UUID, IP описывай как «настройка ПК», «обновление идентификаторов системы», «сброс конфигураций».
- Сразу к делу, без вступлений вроде «в этом гайде я расскажу»."""
    
    for model_name in ["gpt-4o", "gpt-3.5-turbo"]:
        try:
            response = g4f.ChatCompletion.create(
                model=model_name, 
                messages=[{"role": "user", "content": prompt}]
            )
            if response and len(response) > 100:
                if _is_refused(response):
                    print(f"      ⚠️ ИИ отказался писать гайд. Пропускаем товар.")
                    return None
                return response.strip()
        except: continue
    return f"Подробное руководство по {game_name}: {idea['title']}. Внутри вы найдете пошаговую инструкцию и лучшие тактики."

def ai_translate_to_en(text):
    """Переводит русский текст на английский для полей [en]."""
    prompt = f"""Translate the following Russian gaming text to English.
Write as a real person, not as an AI. Do NOT mention being an AI or language model.
Output ONLY the translation, no meta-comments:

{text}"""
    for model_name in ["gpt-4o", "gpt-3.5-turbo"]:
        try:
            response = g4f.ChatCompletion.create(
                model=model_name, 
                messages=[{"role": "user", "content": prompt}]
            )
            if response: return response.strip()
        except: continue
    return "High quality gaming service. Professional guides and support."
