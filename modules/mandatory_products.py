"""Выбор обязательных товаров с учётом конкретной игры.

Сначала применяются очевидные правила по названию игры и разделу FunPay.
Если ситуация неочевидна, один запрос ИИ оценивает все три товара сразу.
На выходе всегда ровно три готовых словаря товара.
"""

import json
import os
import re
from datetime import datetime

import config
from modules import ai_client
from modules.logger import logger


_PRODUCT_IDS = tuple(product["id"] for product in config.MANDATORY_PRODUCTS)

_PC_HINTS = (
    "pc", "пк", "windows", "win", "steam", "epic games", "epic",
    "компьютер", "desktop", "gog",
    # Известные в основном PC-игры. Явная пометка mobile/console имеет приоритет.
    "counter-strike", "cs2", "valorant", "dota", "league of legends",
    "apex legends", "fortnite", "pubg", "rust", "overwatch", "rainbow six",
    "escape from tarkov", "world of warcraft", "path of exile", "warframe",
)
_NON_PC_HINTS = (
    "mobile", "мобильн", "android", "ios", "iphone", "ipad", "телефон",
    "планшет", "ps4", "ps5", "playstation", "xbox", "nintendo", "switch",
    "console", "консоль",
)
_ONLINE_HINTS = (
    "online", "онлайн", "multiplayer", "мультиплеер", "mmorpg", "mmo",
    "pvp", "pve", "co-op", "coop", "кооп", "сетевая", "сетевой",
    "матч", "battle royale", "арена", "рейд",
)
_OFFLINE_HINTS = (
    "offline", "офлайн", "single-player", "single player", "singleplayer",
    "одиночная", "одиночный", "только сюжет", "сюжетная", "без онлайна",
    "без сети", "без сетевого режима", "visual novel",
)
_ANTICHEAT_HINTS = (
    "anti-cheat", "anticheat", "античит", "easy anti-cheat", "eac", "battleye",
    "vanguard", "ricochet", "vac", "faceit", "valorant", "counter-strike",
    "cs2", "apex legends", "fortnite", "pubg", "rainbow six", "destiny",
    "dead by daylight", "call of duty", "warzone", "escape from tarkov",
)
_NO_ANTICHEAT_HINTS = (
    "без античита", "no anti-cheat", "no anticheat", "offline", "офлайн",
    "single-player", "single player", "одиночная", "одиночный",
)


def _normalise(text):
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _has_hint(text, hints):
    return any(hint in text for hint in hints)


def quick_game_check(game_name, section_name=""):
    """Дешёвая проверка. Значение None означает «нужно спросить ИИ»."""
    text = _normalise(f"{game_name} {section_name}")
    explicit_pc = _has_hint(text, _PC_HINTS)
    explicit_non_pc = _has_hint(text, _NON_PC_HINTS)
    explicit_online = _has_hint(text, _ONLINE_HINTS)
    explicit_offline = _has_hint(text, _OFFLINE_HINTS)
    explicit_anticheat = _has_hint(text, _ANTICHEAT_HINTS)
    explicit_no_anticheat = _has_hint(text, _NO_ANTICHEAT_HINTS)

    if explicit_pc:
        is_pc = True
    elif explicit_non_pc:
        is_pc = False
    else:
        is_pc = None

    if explicit_online:
        is_online = True
    elif explicit_offline:
        is_online = False
    else:
        is_online = None

    if explicit_anticheat and not explicit_no_anticheat:
        has_anticheat = True
    elif explicit_no_anticheat or is_online is False:
        has_anticheat = False
    else:
        has_anticheat = None

    forced_replace = set()
    if is_pc is False:
        forced_replace.update(("pc_optimization", "hwid"))
    if is_online is False:
        forced_replace.update(("ping", "hwid"))
    if has_anticheat is False:
        forced_replace.add("hwid")

    return {
        "pc": is_pc,
        "online": is_online,
        "anti_cheat": has_anticheat,
        "forced_replace": sorted(forced_replace),
        "text": text,
    }


# Более короткое имя удобно для тестов и для будущих интеграций.
cheap_game_check = quick_game_check


def _public_product(product):
    points = product.get("content_points", [])
    if isinstance(points, str):
        points = [points]
    if not isinstance(points, (list, tuple)):
        points = []
    return {
        "title": str(product.get("title", "Игровая инструкция")),
        "type": str(product.get("type", "инструкция")),
        "content_points": [str(point) for point in points][:6],
    }


def _default_replacement(product_id, game_name, signals):
    """Безопасная локальная замена, если ИИ не ответил или дал плохой JSON."""
    if product_id == "pc_optimization":
        if signals["pc"] is False:
            title = f"НАСТРОЙКА ИГРЫ {game_name.upper()} НА ЭТОЙ ПЛАТФОРМЕ"
            points = ["Графика и управление", "Комфортный FPS", "Проверка системных настроек"]
        else:
            title = f"БЫСТРАЯ НАСТРОЙКА {game_name.upper()} ДЛЯ СТАБИЛЬНОЙ ИГРЫ"
            points = ["Базовые параметры", "Устранение лишних задержек", "Проверка результата"]
        return {"title": title, "type": "мануал", "content_points": points}

    if product_id == "ping":
        title = f"НАСТРОЙКИ СТАБИЛЬНОЙ ИГРЫ В {game_name.upper()}"
        points = ["Проверка соединения", "Настройки клиента", "Как найти причину задержек"]
        if signals["online"] is False:
            title = f"КОМФОРТНЫЕ НАСТРОЙКИ И УПРАВЛЕНИЕ В {game_name.upper()}"
            points = ["Управление", "Изображение", "Удобный игровой профиль"]
        return {"title": title, "type": "инструкция", "content_points": points}

    title = f"РАЗБОР ПРОГРЕССА И РЕСУРСОВ В {game_name.upper()}"
    points = ["Порядок действий", "Полезные предметы", "Как избежать потери прогресса"]
    if signals["online"] is True and signals["anti_cheat"] is False:
        title = f"ЧЕСТНЫЙ ФАРМ И РАЗВИТИЕ В {game_name.upper()}"
        points = ["Маршруты фарма", "Приоритет улучшений", "Экономия времени"]
    return {"title": title, "type": "гайд", "content_points": points}


def _product_id(value):
    if isinstance(value, dict):
        value = value.get("product_id") or value.get("id") or value.get("title")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if 0 <= value < len(_PRODUCT_IDS):
            return _PRODUCT_IDS[value]
        if 1 <= value <= len(_PRODUCT_IDS):
            return _PRODUCT_IDS[value - 1]
        return None

    text = _normalise(value)
    for product in config.MANDATORY_PRODUCTS:
        if text == product["id"] or text == _normalise(product["title"]):
            return product["id"]
    if "пинг" in text or "ping" in text or "сеть" in text:
        return "ping"
    if "hwid" in text or "желез" in text or "идентификатор" in text:
        return "hwid"
    if "оптимизац" in text or "nvidia" in text or "fps" in text or "пк" in text:
        return "pc_optimization"
    return None


def _extract_json(response):
    if isinstance(response, dict):
        return response
    match = re.search(r"\{.*\}", str(response or ""), re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _ai_prompt(game_name, section_name, signals):
    products = [
        {"id": product["id"], "title": product["title"], "type": product["type"]}
        for product in config.MANDATORY_PRODUCTS
    ]
    return f"""Оцени пригодность трёх обязательных товаров для игры «{game_name}».
Раздел FunPay: «{section_name or 'не указан'}».
Быстрая проверка по названию дала сигналы: {json.dumps(signals, ensure_ascii=False)}.

Товары:
{json.dumps(products, ensure_ascii=False)}

Проверь отдельно:
- есть ли у игры версия для ПК/Windows;
- есть ли сетевой или соревновательный режим;
- используется ли античит или есть ли смысл в теме системных идентификаторов.
Не притягивай товар к игре, если он явно бессмысленен. Для офлайн-, мобильной или
консольной игры предложи замену с реальными темами именно этой игры.

Ответь строго одним JSON-объектом без Markdown:
{{
  "keep": ["pc_optimization", "ping"],
  "replace": [
    {{
      "product_id": "hwid",
      "reason": "короткая причина на русском",
      "replacement": {{
        "title": "НАЗВАНИЕ ЗАМЕНЫ",
        "type": "тип",
        "content_points": ["конкретный пункт", "ещё один пункт"]
      }}
    }}
  ]
}}
Если подходят все три, верни все три id в keep и пустой массив replace."""


def _log_replacement(game_name, product_id, reason, replacement):
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "game": game_name,
        "product_id": product_id,
        "reason": reason,
        "replacement": replacement.get("title", ""),
    }
    try:
        os.makedirs(config.LOGS_DIR, exist_ok=True)
        with open(os.path.join(config.LOGS_DIR, "mandatory_products.jsonl"), "a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
    logger.info(
        f"Обязательный товар заменён для «{game_name}»: {product_id} — {reason}. "
        f"Новая тема: {replacement.get('title', 'без названия')}"
    )


def select_mandatory_products(game_name, section_name=""):
    """Возвращает ровно три подходящих обязательных товара."""
    signals = quick_game_check(game_name, section_name)
    forced = set(signals["forced_replace"])
    decision = None

    # Если название уже однозначно говорит «мобильная/консольная офлайн-игра»,
    # не тратим запрос ИИ на очевидные замены.
    if len(forced) < len(_PRODUCT_IDS):
        try:
            response = ai_client.ai_ask(
                [{"role": "user", "content": _ai_prompt(game_name, section_name, signals)}],
                max_tokens=1200,
                temperature=0.1,
            )
            decision = _extract_json(response)
        except ai_client.AILimitReached:
            raise
        except ai_client.AIError as error:
            logger.info(f"Проверка обязательных товаров через ИИ недоступна: {error}")
        except Exception as error:
            logger.info(f"Неожиданная ошибка проверки обязательных товаров: {error}")

    keep = set()
    replacements = {}
    reasons = {}
    if decision:
        keep = {_product_id(value) for value in decision.get("keep", [])}
        keep.discard(None)
        for item in decision.get("replace", []) or []:
            if not isinstance(item, dict):
                continue
            product_id = _product_id(item)
            replacement = item.get("replacement")
            if not product_id or not isinstance(replacement, dict):
                continue
            replacements[product_id] = replacement
            reasons[product_id] = str(item.get("reason") or "ИИ считает товар неподходящим")

        # Если ИИ не указал product_id, относим замену к первому товару,
        # которого нет в keep и который ещё не получил замену.
        unassigned = [product_id for product_id in _PRODUCT_IDS if product_id not in keep and product_id not in replacements]
        for item in decision.get("replace", []) or []:
            if not isinstance(item, dict) or _product_id(item) is not None:
                continue
            replacement = item.get("replacement")
            if unassigned and isinstance(replacement, dict):
                product_id = unassigned.pop(0)
                replacements[product_id] = replacement
                reasons[product_id] = str(item.get("reason") or "ИИ считает товар неподходящим")

    result = []
    for product in config.MANDATORY_PRODUCTS:
        product_id = product["id"]
        should_replace = product_id in forced or product_id in replacements or (
            decision is not None and product_id not in keep
        )
        if should_replace:
            replacement = replacements.get(product_id) or _default_replacement(product_id, game_name, signals)
            replacement = _public_product(replacement)
            reason = reasons.get(product_id)
            if not reason:
                reason = "быстрый фильтр по названию/разделу игры"
                if product_id == "pc_optimization" and signals["pc"] is False:
                    reason = "у игры не обнаружена версия для ПК"
                elif product_id == "ping" and signals["online"] is False:
                    reason = "у игры не обнаружен сетевой режим"
                elif product_id == "hwid" and signals["anti_cheat"] is False:
                    reason = "у игры не обнаружен античит или сетевой режим"
            _log_replacement(game_name, product_id, reason, replacement)
            result.append(replacement)
        else:
            result.append(_public_product(product))

    return result
