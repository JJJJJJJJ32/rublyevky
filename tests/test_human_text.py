import modules.description_builder as description_builder
from modules.ai_generator import (
    _collapse_emoji_runs,
    _guide_keywords,
    _is_guide_good,
    _is_guide_relevant,
    _is_list_heavy,
    _is_repetitive,
    _postprocess_guide,
    ai_generate_full_content,
)
import pytest

from modules import ai_client


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


# ═══════════════════════════════════════════════════════════════════════
# 7. Пост-проверка качества гайда: связь с игрой и товаром
# ═══════════════════════════════════════════════════════════════════════

GAME = "Cyberpunk 2077"
IDEA = {
    "title": "МАНУАЛ ПО ОПТИМИЗАЦИИ ПК ДЛЯ ИГР + НАСТРОЙКА NVIDIA BOOST FPS",
    "content_points": ["Настройка Windows", "NVIDIA Boost", "Исправление FPS"],
}


def _good_guide():
    """Живой гайд, который начинается с конкретной настройки."""
    return (
        "Открой настройки Cyberpunk 2077 и сразу перейди в раздел «Графика». "
        "Снизь объемную плотность до среднего значения: в Night City это даёт "
        "самый заметный прирост FPS и почти не меняет картинку. Дальше отключи "
        "сглаживание Film Grain — на подсветке и мокром асфальте оно съедало "
        "до восьми кадров. В панели NVIDIA включи низкую задержку и поставь "
        "максимальную производительность для Cyberpunk 2077. В Windows открой "
        "параметры графики и добавь игру в список приложений с режимом "
        "«Высокая производительность». После этого перезапусти игру и проверь "
        "результат на одном и том же маршруте: выезд из района и перестрелка "
        "на мосту. Если FPS всё ещё низкий, "
        "сократи дальность прорисовки до 75 процентов и обнови драйвер "
        "видеокарты. Термопаста и профиль вентиляторов тоже стоит проверить: "
        "при температуре выше восьмидесяти градусов процессор сам снижает "
        "частоту. Настройка питания в Windows должна быть «Максимальная "
        "производительность», иначе часть ядер уходит в сон. Такой порядок "
        "действий закрывает основные причины просадок в Cyberpunk 2077 без "
        "потери качества картинки."
    )


def test_good_game_guide_is_accepted():
    assert _is_guide_relevant(_good_guide(), GAME, IDEA["title"], IDEA["content_points"]) is True
    assert _is_guide_good(_good_guide() * 12) is True


def test_artistic_text_is_rejected():
    artistic = (
        "Представь, что ты идёшь по тихой деревне, где туман ложится на крыши "
        "домов, а силуэты людей медленно растворяются в сумерках. Кажется, что "
        "здесь время остановилось, и только шум листвы напоминает о живом мире. "
        "Герой этой истории долго стоял у развилки дорог, размышляя о смысле "
        "пути и о тишине, которая окружает каждый шаг."
    )
    assert _is_guide_relevant(artistic, GAME, IDEA["title"], IDEA["content_points"]) is False


def test_text_without_game_connection_is_rejected():
    generic = (
        "Хороший текст должен быть живым и полезным. Сначала подготовь рабочее "
        "место, затем сделай глубокий вдох и приступай к делу. Важно сохранять "
        "спокойствие и не торопиться. Многие читатели отмечают, что такой "
        "подход помогает справиться с любой задачей и приходит к результату "
        "без лишнего стресса. Не забывай про перерывы и воду."
    )
    assert _is_guide_relevant(generic, GAME, IDEA["title"], IDEA["content_points"]) is False


def test_guide_without_keywords_is_rejected():
    other_game = (
        "Найди на карте точку возрождения и двигайся к северным воротам. "
        "Возьми из сундука зелье лечения и проверь прочность брони. "
        "После боя подбери ключ и открой дверь в башню."
    )
    assert _is_guide_relevant(other_game, GAME, IDEA["title"], IDEA["content_points"]) is False


def test_repetitive_guide_is_rejected():
    repeated = "Это очень важный и полезный совет по настройке игры. " * 60
    assert _is_repetitive(repeated) is True
    assert _is_guide_relevant(
        repeated, GAME, IDEA["title"], IDEA["content_points"]
    ) is False


def test_guide_with_too_many_bullets_is_rejected():
    lines = [f"- шаг {index} по настройке Cyberpunk 2077 и NVIDIA" for index in range(30)]
    lines.append("Обычный абзац с пояснением.")
    bullet_guide = "\n".join(lines)

    assert _is_list_heavy(bullet_guide) is True
    assert _is_guide_good(bullet_guide * 20) is False


def test_keywords_come_from_game_title_and_content_points():
    keywords = _guide_keywords(GAME, IDEA["title"], IDEA["content_points"])

    assert "cyberpunk" in keywords
    assert "nvidia" in keywords
    assert "windows" in keywords
    # Служебные слова не считаются ключевыми.
    assert "гайд" not in keywords


def test_forbidden_final_phrases_are_removed_not_emptied():
    source = (
        "Открой настройки Cyberpunk 2077 и снизь объемную плотность.\n\n"
        "## Итоги\n"
        "В заключение, это лучший вариант. Таким образом, всё готово.\n"
    )
    result = _postprocess_guide(source)
    low = result.lower()

    assert "итоги" not in low
    assert "в заключение" not in low
    assert "##" not in result
    # После очистки гайд не превращается в пустой текст.
    assert result.strip()
    assert "cyberpunk 2077" in low


def test_cleanup_to_nothing_is_not_published():
    """Если после очистки от гайда ничего не осталось — он не публикуется."""
    source = "## Итоги\nВ заключение, всё готово. Вывод: отлично.\n"
    result = _postprocess_guide(source)

    assert result.strip() == ""
    assert _is_guide_good(result) is False


def test_bad_guide_is_not_published_and_strict_retry_saves_it(monkeypatch):
    """Плохой ответ не публикуется: бот пробует строгий промпт."""
    bad = (
        "Представь, что ты идёшь по тихой деревне, где туман ложится на крыши "
        "домов, а силуэты людей медленно растворяются в сумерках. Кажется, что "
        "здесь время остановилось, и только шум листвы напоминает о живом мире. "
        "Герой этой истории долго стоял у развилки дорог, размышляя о смысле "
        "пути и о тишине, которая окружает каждый шаг."
    )
    calls = []

    def fake_ask(messages, **kwargs):
        calls.append(messages[0]["content"])
        return bad if len(calls) < 4 else _good_guide()

    monkeypatch.setattr("modules.ai_generator._ask", fake_ask)
    monkeypatch.setattr("modules.ai_generator.time.sleep", lambda seconds: None)

    result = ai_generate_full_content(IDEA, GAME)

    assert result is not None
    assert "Cyberpunk 2077" in result
    assert len(calls) == 4


def test_all_bad_guides_are_not_published(monkeypatch):
    bad = (
        "Представь, что ты идёшь по тихой деревне, где туман ложится на крыши "
        "домов, а силуэты людей медленно растворяются в сумерках. Кажется, что "
        "здесь время остановилось, и только шум листвы напоминает о живом мире. "
        "Герой этой истории долго стоял у развилки дорог, размышляя о смысле "
        "пути и о тишине, которая окружает каждый шаг."
    )

    monkeypatch.setattr("modules.ai_generator._ask", lambda messages, **kwargs: bad)
    monkeypatch.setattr("modules.ai_generator.time.sleep", lambda seconds: None)

    assert ai_generate_full_content(IDEA, GAME) is None


def test_short_guide_is_extended_in_second_request(monkeypatch):
    """Гайд просят двумя короткими запросами: длинный ответ обрывается по сети."""
    part2 = (
        "Дальше по Cyberpunk 2077 проверь ещё один параметр в настройках графики: "
        "отключи трассировку лучей на слабой видеокарте и сравни FPS на том же "
        "маршруте. Если просадки остались, снизь качество теней и обнови драйвер."
    )
    calls = []

    def fake_ask(messages, **kwargs):
        calls.append(messages[0]["content"])
        return _good_guide() if len(calls) == 1 else part2

    monkeypatch.setattr("modules.ai_generator._ask", fake_ask)
    monkeypatch.setattr("modules.ai_generator.time.sleep", lambda seconds: None)

    result = ai_generate_full_content(IDEA, GAME)

    assert len(calls) == 2, "короткий гайд должен дописываться вторым запросом"
    assert result is not None
    assert "Дальше по Cyberpunk 2077" in result
    assert "Открой настройки Cyberpunk 2077" in result


def test_continuation_failure_keeps_first_part(monkeypatch):
    """Если дописывание оборвалось, уже полученная часть не выбрасывается."""
    calls = []

    def fake_ask(messages, **kwargs):
        calls.append(messages[0]["content"])
        if len(calls) == 1:
            return _good_guide()
        raise ai_client.AIUnavailable("обрыв связи")

    monkeypatch.setattr("modules.ai_generator._ask", fake_ask)

    result = ai_generate_full_content(IDEA, GAME)

    assert len(calls) == 2
    assert result is not None
    assert "Открой настройки Cyberpunk 2077" in result


def test_unavailable_worker_stops_guide_attempts(monkeypatch):
    """Если Worker недоступен, не тратим четыре попытки на разные промпты."""
    calls = []

    def fake_ask(messages, **kwargs):
        calls.append(messages[0]["content"])
        raise ai_client.AIUnavailable("Worker не отвечает")

    monkeypatch.setattr("modules.ai_generator._ask", fake_ask)

    with pytest.raises(ai_client.AIUnavailable):
        ai_generate_full_content(IDEA, GAME)

    assert len(calls) == 1


def test_ask_propagates_unavailable(monkeypatch):
    import modules.ai_generator as ai_generator

    def boom(*args, **kwargs):
        raise ai_client.AIUnavailable("нет Worker")

    monkeypatch.setattr(ai_generator.ai_client, "ai_ask", boom)

    with pytest.raises(ai_client.AIUnavailable):
        ai_generator._ask([{"role": "user", "content": "тест"}])


def test_ask_swallows_ordinary_ai_error(monkeypatch):
    import modules.ai_generator as ai_generator

    def boom(*args, **kwargs):
        raise ai_client.AIError("пустой ответ")

    monkeypatch.setattr(ai_generator.ai_client, "ai_ask", boom)

    assert ai_generator._ask([{"role": "user", "content": "тест"}]) is None
