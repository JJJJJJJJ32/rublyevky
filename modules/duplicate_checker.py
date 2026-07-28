import json
import os

PROCESSED_FILE = "data/processed_games.json"
LOG_FILE = "data/bot_log.json"

def is_game_processed(game_id):
    # Проверка основного списка обработанных игр
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
            try:
                processed = json.load(f)
                if str(game_id) in [str(i) for i in processed]:
                    return True
            except: pass

    # Запасная проверка через лог
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                for game in data.get("processed_games", []):
                    if str(game.get("game_id")) == str(game_id):
                        return True
            except: pass
            
    return False

def mark_as_processed(game_id):
    processed = []
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
            try: processed = json.load(f)
            except: pass
    
    if str(game_id) not in [str(i) for i in processed]:
        processed.append(str(game_id))
        with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
            json.dump(processed, f, indent=2)
