import os

def load_games_list(file_path="data/games_list.txt"):
    games = []
    if not os.path.exists(file_path): return games
    
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    for line in lines:
        line = line.strip()
        if not line: continue
        
        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
            games.append({"game_name": parts[0], "game_id": parts[1], "raw_line": line})
        else:
            games.append({"game_name": line, "game_id": None, "raw_line": line})
    return games

def remove_game_from_list(game_name, file_path="data/games_list.txt"):
    """Полностью вырезает игру из файла по названию."""
    if not os.path.exists(file_path): return
    
    temp_path = file_path + ".tmp"
    game_name_lower = str(game_name).lower().strip()
    
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        with open(temp_path, "w", encoding="utf-8") as f:
            for line in lines:
                # Если в строке есть название игры - пропускаем её
                if game_name_lower in line.lower():
                    continue
                f.write(line)
        
        os.replace(temp_path, file_path)
    except Exception as e:
        if os.path.exists(temp_path): os.remove(temp_path)
        print(f"Ошибка удаления {game_name}: {e}")
