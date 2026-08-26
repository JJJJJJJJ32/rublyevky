import os

def _game_name_from_line(line):
    """Возвращает название игры из строки ``название`` или ``название|id``."""
    return line.strip().split("|", 1)[0].strip()


def load_games_list(file_path="data/games_list.txt"):
    games = []
    if not os.path.exists(file_path):
        return games

    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        game_name = _game_name_from_line(line)
        if not game_name:
            continue

        game_id = None
        if "|" in line:
            _, possible_id = line.split("|", 1)
            game_id = possible_id.strip() or None

        games.append({
            "game_name": game_name,
            "game_id": game_id,
            "raw_line": line,
        })
    return games


def remove_game_from_list(game_name, file_path="data/games_list.txt"):
    """Удаляет только игру с точно совпадающим названием.

    Частичный поиск здесь опасен: ``Raft`` встречается внутри ``Minecraft``
    и ``World of Warcraft``.
    """
    if not os.path.exists(file_path):
        return

    temp_path = file_path + ".tmp"
    target = str(game_name).casefold().strip()

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        with open(temp_path, "w", encoding="utf-8") as f:
            for line in lines:
                if _game_name_from_line(line).casefold() != target:
                    f.write(line)

        os.replace(temp_path, file_path)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        print(f"Ошибка удаления {game_name}: {e}")
