import threading
import time

import main


class _Drive:
    def create_folder(self, name):
        return "folder-id"

    def upload_file(self, path, folder_id):
        return "https://example.test/file"


def test_ideas_start_while_funpay_category_is_searching(monkeypatch):
    ideas_started = threading.Event()

    def fake_ideas(game_name):
        ideas_started.set()
        return [], 0.01

    def fake_find(session, game_name):
        assert ideas_started.wait(1), "идеи должны запускаться до поиска раздела"
        return None

    monkeypatch.setattr(main, "_generate_ideas_timed", fake_ideas)
    monkeypatch.setattr(main, "find_funpay_category", fake_find)
    monkeypatch.setattr(main, "remove_game_from_list", lambda game_name: None)
    monkeypatch.setattr(main.ai_client, "used_today", lambda: (0, 0))

    main.process_single_game(object(), {"game_name": "Тестовая игра"}, object())


def test_next_guide_is_prefetched_before_current_lot_is_published(monkeypatch, tmp_path):
    first = {"title": "Первый товар", "content_points": []}
    second = {"title": "Второй товар", "content_points": []}
    prefetch_started = threading.Event()

    monkeypatch.setattr(main, "MANDATORY_PRODUCTS", [first, second])
    monkeypatch.setattr(main, "_generate_ideas_timed", lambda game_name: ([], 0.01))
    monkeypatch.setattr(
        main,
        "find_funpay_category",
        lambda session, game_name: {"id": "1", "type": "game"},
    )
    monkeypatch.setattr(main, "is_game_processed", lambda game_id: False)
    monkeypatch.setattr(main, "get_category_info", lambda session, game_id: "Тест")
    monkeypatch.setattr(main, "gdrive", _Drive())
    monkeypatch.setattr(main.config, "GENERATED_CONTENT_DIR", str(tmp_path))
    monkeypatch.setattr(main, "generate_short_description_ruble", lambda title: title)
    monkeypatch.setattr(main, "generate_full_description_ruble", lambda idea, game: "Описание")
    monkeypatch.setattr(main, "generate_payment_message", lambda link: link)
    monkeypatch.setattr(main, "check_wemod_availability", lambda game_name: False)
    monkeypatch.setattr(main, "mark_as_processed", lambda game_id: None)
    monkeypatch.setattr(main, "remove_game_from_list", lambda game_name: None)
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(main.ai_client, "used_today", lambda: (0, 0))

    def fake_content(idea, game_name):
        if idea["title"] == second["title"]:
            prefetch_started.set()
        return "готовый гайд", 0.01

    monkeypatch.setattr(main, "_generate_content_timed", fake_content)

    def fake_publish(*args):
        assert prefetch_started.wait(1), "следующий гайд должен считаться до публикации текущего"
        return "success"

    monkeypatch.setattr(main, "publish_lot", fake_publish)

    main.process_single_game(object(), {"game_name": "Тестовая игра"}, object())
