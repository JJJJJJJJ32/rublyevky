"""Идеи товаров, уже придуманные для игры, — чтобы повтор их не пересочинял.

ИИ каждый раз формулирует названия по-своему. Если игра прервалась (ИИ не
ответил, оборвалась сеть) и вернулась в работу, новые названия не совпали бы
с уже выставленными лотами, и бот создал бы дубли. Поэтому идеи сохраняются
на диск и переиспользуются при повторе.

Данные лежат в ``data/game_ideas.json``::

    {
      "12345": [{"title": "...", "type": "...", "content_points": ["..."]}]
    }

Очищается вместе с историей лотов командой ``py main.py --reset-lots``.
"""

import json
import os
import threading

from modules.logger import logger

IDEAS_FILE = os.path.join("data", "game_ideas.json")

_lock = threading.Lock()


def _load():
    if not os.path.exists(IDEAS_FILE):
        return {}
    try:
        with open(IDEAS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data):
    folder = os.path.dirname(IDEAS_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(IDEAS_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def load_ideas(game_id):
    """Возвращает сохранённые идеи игры или None, если их ещё нет."""
    with _lock:
        ideas = _load().get(str(game_id))
    if not isinstance(ideas, list) or not ideas:
        return None
    return [idea for idea in ideas if isinstance(idea, dict) and idea.get("title")]


def save_ideas(game_id, ideas):
    """Запоминает идеи игры. Пустой список не сохраняется."""
    ideas = [
        idea for idea in (ideas or [])
        if isinstance(idea, dict) and str(idea.get("title") or "").strip()
    ]
    if not ideas:
        return
    with _lock:
        data = _load()
        data[str(game_id)] = ideas
        try:
            _save(data)
        except OSError as error:
            logger.log_error(f"Не удалось сохранить идеи игры {game_id}: {error}")


def forget(game_id):
    """Забывает идеи одной игры (после полного завершения)."""
    with _lock:
        data = _load()
        if str(game_id) in data:
            data.pop(str(game_id), None)
            _save(data)


def clear():
    """Полностью очищает сохранённые идеи."""
    with _lock:
        _save({})
