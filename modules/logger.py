import json
import os
import sys
import logging
from datetime import datetime

# Переменные, включающие подробный вывод в консоль.
# DEBUG_LOGS=1 (или DEBUG=1) показывает тайминги, модели, уровни сокращения
# и прочие внутренние сообщения. Без них в консоли только короткий итог.
DEBUG_ENV_VARS = ("DEBUG_LOGS", "DEBUG")


def debug_enabled():
    """Включён ли подробный вывод в консоль."""
    for name in DEBUG_ENV_VARS:
        raw = (os.getenv(name) or "").strip().lower()
        if raw in ("1", "true", "yes", "on", "debug", "verbose"):
            return True
    return False


class BotLogger:
    def __init__(self, log_file="data/bot_log.json", error_log="logs/errors.log", debug=None):
        self.log_file = log_file
        self.error_log = error_log
        # В обычном режиме внутренние сообщения не показываются: они всегда
        # уходят в logs/errors.log, а в консоль — только при DEBUG_LOGS=1.
        self.debug_mode = debug_enabled() if debug is None else bool(debug)

        # Создаем необходимые папки, если их нет
        os.makedirs("data", exist_ok=True)
        os.makedirs("logs", exist_ok=True)
        os.makedirs("generated_content", exist_ok=True)

        self._setup_standard_logging()
        self._init_json_log()

    def _setup_standard_logging(self):
        """Файл всегда, консоль — только в режиме отладки."""
        # У каждого экземпляра свой логгер: тесты и основной бот не мешают
        # друг другу обработчиками.
        self._logger = logging.getLogger(f"funpay_bot_{id(self):x}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

        file_handler = logging.FileHandler(self.error_log, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s - funpay_bot - %(levelname)s - %(message)s")
        )
        self._logger.addHandler(file_handler)

        if self.debug_mode:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(logging.DEBUG)
            console_handler.setFormatter(
                logging.Formatter(
                    "[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S"
                )
            )
            self._logger.addHandler(console_handler)

    def _init_json_log(self):
        if not os.path.exists(self.log_file):
            initial_data = {
                "processed_games": [],
                "statistics": {
                    "total_games_processed": 0,
                    "total_lots_created": 0,
                    "total_errors": 0,
                    "bot_start_time": datetime.now().isoformat(),
                    "last_activity": datetime.now().isoformat()
                }
            }
            self._save_json(initial_data)

    def _load_json(self):
        with open(self.log_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_json(self, data):
        with open(self.log_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def log_game_processed(self, game_data):
        data = self._load_json()
        data["processed_games"].append(game_data)
        data["statistics"]["total_games_processed"] += 1
        data["statistics"]["total_lots_created"] += (game_data.get("ruble_lots_created", 0) + game_data.get("wemod_lots_created", 0))
        data["statistics"]["last_activity"] = datetime.now().isoformat()
        self._save_json(data)

    def log_error(self, error_msg):
        """Ошибка: файл errors.log + счётчик статистики. В консоль — только DEBUG."""
        self._logger.error(str(error_msg))
        try:
            data = self._load_json()
            data["statistics"]["total_errors"] += 1
            data["statistics"]["last_activity"] = datetime.now().isoformat()
            self._save_json(data)
        except (OSError, ValueError):
            pass  # логи не должны ломать работу

    def info(self, message):
        """Внутренняя техническая информация: только файл, консоль — при DEBUG."""
        self._logger.info(str(message))

    def debug(self, message):
        """Отладочная деталь: файл всегда, консоль — при DEBUG."""
        self._logger.debug(str(message))


logger = BotLogger()
