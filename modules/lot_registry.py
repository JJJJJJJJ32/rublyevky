"""Какие лоты уже выставлены — чтобы повтор игры не создавал дубли.

Лот публикуется один раз. Если игра упала на середине (ИИ не ответил,
оборвалась сеть), бот возвращается к ней в следующем круге и продолжает
с того же места: уже выставленные товары пропускаются.

Данные лежат в ``data/published_lots.json``::

    {
      "12345": ["мануал по оптимизации пк для игр", "mandatory:hwid"]
    }

Ключ игры — её id на FunPay, поэтому запись переживает переименование.
Сбросить историю можно командой ``py main.py --reset-lots`` или удалив
файл вручную.
"""

import json
import os
import threading

LOTS_FILE = os.path.join("data", "published_lots.json")

_lock = threading.Lock()


def lot_key(title):
    """Нормализованный ключ товара: регистр и лишние пробелы не важны."""
    return " ".join(str(title or "").split()).lower()


def _load():
    if not os.path.exists(LOTS_FILE):
        return {}
    try:
        with open(LOTS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data):
    folder = os.path.dirname(LOTS_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(LOTS_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def was_published(game_id, key):
    """True, если лот с таким ключом уже выставлен для этой игры."""
    key = lot_key(key)
    if not key:
        return False
    with _lock:
        keys = _load().get(str(game_id)) or []
    return key in keys


def mark_published(game_id, key):
    """Запоминает выставленный лот. Повторный вызов ничего не меняет."""
    key = lot_key(key)
    if not key:
        return
    with _lock:
        data = _load()
        keys = data.setdefault(str(game_id), [])
        if key not in keys:
            keys.append(key)
            _save(data)


def reset_game(game_id):
    """Забывает лоты одной игры (нужно после полного завершения игры)."""
    with _lock:
        data = _load()
        if str(game_id) in data:
            data.pop(str(game_id), None)
            _save(data)


def clear():
    """Полностью очищает историю выставленных лотов."""
    with _lock:
        _save({})
