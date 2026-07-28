import os

def check_wemod_availability(game_name, file_path="data/wemod_games_list.txt"):
    """
    Проверяет наличие игры в локальном списке WeMod.
    """
    if not os.path.exists(file_path):
        return False
        
    with open(file_path, "r", encoding="utf-8") as f:
        wemod_games = [line.strip().lower() for line in f.readlines() if line.strip()]
        
    return game_name.strip().lower() in wemod_games
