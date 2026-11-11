"""Журнал выставленных лотов и сохранённые идеи товаров.

Это то, что не даёт боту создавать дубли, когда игра после сбоя
возвращается в работу.
"""

import pytest

from modules import idea_cache, lot_registry


@pytest.fixture(autouse=True)
def _isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(lot_registry, "LOTS_FILE", str(tmp_path / "published_lots.json"))
    monkeypatch.setattr(idea_cache, "IDEAS_FILE", str(tmp_path / "game_ideas.json"))


# ─── Журнал лотов ───────────────────────────────────────────────────────

def test_lot_is_not_published_before_marking():
    assert lot_registry.was_published(42, "МАНУАЛ ПО ОПТИМИЗАЦИИ ПК") is False


def test_marked_lot_is_remembered():
    lot_registry.mark_published(42, "МАНУАЛ ПО ОПТИМИЗАЦИИ ПК")

    assert lot_registry.was_published(42, "мануал по оптимизации пк") is True


def test_title_comparison_ignores_case_and_spaces():
    lot_registry.mark_published(42, "  10   Способов   Фарма ")

    assert lot_registry.was_published(42, "10 способов фарма") is True


def test_lots_are_per_game():
    lot_registry.mark_published(42, "Гайд")

    assert lot_registry.was_published(43, "Гайд") is False


def test_marking_twice_keeps_single_entry():
    lot_registry.mark_published(42, "Гайд")
    lot_registry.mark_published(42, "Гайд")

    data = lot_registry._load()

    assert data["42"] == ["гайд"]


def test_empty_key_is_ignored():
    lot_registry.mark_published(42, "   ")

    assert lot_registry.was_published(42, "") is False
    assert lot_registry._load() == {}


def test_reset_game_forgets_only_that_game():
    lot_registry.mark_published(42, "Гайд")
    lot_registry.mark_published(43, "Гайд")

    lot_registry.reset_game(42)

    assert lot_registry.was_published(42, "Гайд") is False
    assert lot_registry.was_published(43, "Гайд") is True


def test_clear_forgets_everything():
    lot_registry.mark_published(42, "Гайд")
    lot_registry.mark_published(43, "Гайд")

    lot_registry.clear()

    assert lot_registry._load() == {}


def test_broken_file_does_not_crash(tmp_path):
    with open(lot_registry.LOTS_FILE, "w", encoding="utf-8") as file:
        file.write("{ это не json")

    assert lot_registry.was_published(42, "Гайд") is False


# ─── Сохранённые идеи ───────────────────────────────────────────────────

IDEAS = [
    {"title": "МАРШРУТЫ ФАРМА", "type": "гайд", "content_points": ["Локации"]},
    {"title": "БИЛД НА ВЫЖИВАНИЕ", "type": "гайд", "content_points": []},
]


def test_ideas_are_saved_and_loaded():
    idea_cache.save_ideas(42, IDEAS)

    assert idea_cache.load_ideas(42) == IDEAS


def test_ideas_are_per_game():
    idea_cache.save_ideas(42, IDEAS)

    assert idea_cache.load_ideas(43) is None


def test_empty_ideas_are_not_saved():
    idea_cache.save_ideas(42, [])

    assert idea_cache.load_ideas(42) is None


def test_saved_ideas_ignore_items_without_title():
    idea_cache.save_ideas(42, [{"title": "  "}, {"title": "ГАЙД"}])

    assert idea_cache.load_ideas(42) == [{"title": "ГАЙД"}]


def test_clear_and_forget():
    idea_cache.save_ideas(42, IDEAS)
    idea_cache.save_ideas(43, IDEAS)

    idea_cache.forget(42)
    assert idea_cache.load_ideas(42) is None
    assert idea_cache.load_ideas(43) == IDEAS

    idea_cache.clear()
    assert idea_cache.load_ideas(43) is None
