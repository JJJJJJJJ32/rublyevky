import json
import os
import logging
from datetime import datetime

class BotLogger:
    def __init__(self, log_file="data/bot_log.json", error_log="logs/errors.log"):
        self.log_file = log_file
        self.error_log = error_log
        
        # Создаем необходимые папки, если их нет
        os.makedirs("data", exist_ok=True)
        os.makedirs("logs", exist_ok=True)
        os.makedirs("generated_content", exist_ok=True)
        
        self._setup_standard_logging()
        self._init_json_log()

    def _setup_standard_logging(self):
        logging.basicConfig(
            filename=self.error_log,
            level=logging.ERROR,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

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
        logging.error(error_msg)
        data = self._load_json()
        data["statistics"]["total_errors"] += 1
        data["statistics"]["last_activity"] = datetime.now().isoformat()
        self._save_json(data)

    def info(self, message):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] INFO: {message}")

logger = BotLogger()
