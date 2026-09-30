import modules.description_builder as description_builder
from modules.ai_generator import (
    _collapse_emoji_runs,
    _is_guide_good,
    _is_list_heavy,
    _postprocess_guide,
)


FORBIDDEN_PHRASES = (
    "в заключение",
    "в заключении",
    "итоги",
    "вывод",
    "подводя итог",
    "таким образом",
    "в этой статье",
    "в этом гайде я расскажу",
    "важно отметить",
    "стоит отметить",
    "не забывайте",
)


def test_postprocess_removes_forbidden_phrases_and_markdown():
    source = """## Основной маршрут
В этой статье я разберу маршрут.
Важно отметить, что время зависит от уровня.
Не забывайте проверить инвентарь.

## Итоги
В заключение, это лучший вариант. Таким образом, всё готово.
"""

    result = _postprocess_guide(source)
    low = result.lower()

    assert "##" not in result
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in low


def test_postprocess_removes_the_whole_conclusion_section():
    source = """Сначала возьми предмет и дойди до северных ворот.

Заключение:
Эта строка уже не должна попасть в готовый гайд.
"""

    result = _postprocess_guide(source)

    assert "северных ворот" in result
    assert "эта строка уже не должна" not in result.lower()
    assert "заключение" not in result.lower()


def test_emoji_runs_are_collapsed_without_orphaning_variation_selector():
    result = _collapse_emoji_runs("🔥 🌟 ✨ Проверь ⚠️ и продолжай.")

    assert result.startswith("🔥")
    assert "🌟" not in result
    assert "✨" not in result
    assert "⚠️" in result
    assert "⚠" not in result.replace("⚠️", "")


def test_list_heavy_guide_is_rejected():
    list_text = "\n".join([
        "- шаг один",
        "• шаг два",
        "1. шаг три",
        "2. шаг четыре",
        "- шаг пять",
        "Обычный абзац с пояснением.",
        "Ещё один обычный абзац.",
        "Третий обычный абзац.",
        "Четвёртый обычный абзац.",
        "Пятый обычный абзац.",
    ])

    assert _is_list_heavy(list_text) is True
    assert _is_guide_good((list_text + " ") * 80) is False


def test_reasonable_number_of_list_lines_is_allowed():
    text = "\n".join([
        "Обычный абзац с подробным объяснением маршрута.",
        "Ещё один абзац с условиями и примером.",
        "- один короткий пункт",
        "Третий абзац с конкретными деталями.",
        "Четвёртый абзац с проверкой результата.",
    ])

    assert _is_list_heavy(text) is False


def test_lot_descriptions_keep_one_delivery_line_per_style(monkeypatch):
    idea = {"title": "Маршрут фарма", "content_points": ["Локация", "Порядок действий"]}

    for style_index in range(6):
        monkeypatch.setattr(
            description_builder.random,
            "choice",
            lambda sequence, index=style_index: sequence[index],
        )
        description = description_builder.generate_full_description_ruble(idea, "Тестовая игра")
        assert description.count("🕒АВТОВЫДАЧА 24/7🕒") == 1
