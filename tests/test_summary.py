import re

import modules.description_builder as description_builder
from config import LIMITS
from modules.description_builder import (
    fit_summary,
    generate_full_description_ruble,
    generate_short_description_ruble,
)


LONG_TITLE = (
    "ОЧЕНЬ ДЛИННОЕ НАЗВАНИЕ ТОВАРА ДЛЯ МАКСИМАЛЬНОГО ФАРМА "
    "В СЛОЖНОЙ ИГРОВОЙ ЛОКАЦИИ"
)

PC_TITLE = "МАНУАЛ ПО ОПТИМИЗАЦИИ ПК ДЛЯ ИГР + НАСТРОЙКА NVIDIA BOOST FPS"
PING_TITLE = "10 СПОСОБОВ КАК ПОНИЗИТЬ ВЫСОКИЙ ПИНГ"
HWID_TITLE = "[ СПОСОБ СНЯТЬ БЛОКИРОВКУ С ЖЕЛЕЗА ] [ СМЕНА HWID IP MAC АДРЕС UUID ]"


def _unmatched_brackets(text):
    """Возвращает список скобок без пары."""
    stack = []
    unmatched = []
    for char in text:
        if char in description_builder._OPENERS:
            stack.append(char)
        elif char in description_builder._CLOSERS:
            if stack and description_builder._BRACKET_PAIRS[stack[-1]] == char:
                stack.pop()
            else:
                unmatched.append(char)
    unmatched.extend(stack)
    return unmatched


def _ends_with_dangling(text):
    """True, если строка обрывается на открывающей скобке, дефисе, одиночном
    U+FE0F или оборванной декоративной конструкции."""
    if not text:
        return False
    last = text[-1]
    if last.isalnum() or last == "_":
        return False
    if last in description_builder._CLOSERS:
        return False
    if last in description_builder._DANGLING_DECOR:
        return True
    if description_builder._is_emoji_char(last):
        return False
    if last in "\ufe0f\ufe0e" and len(text) > 1 and description_builder._is_emoji_char(text[-2]):
        return False
    return True


def _assert_summary_limits(text):
    assert LIMITS["summary_min"] <= len(text) <= LIMITS["summary_max"]
    assert len(text.encode("utf-8")) <= LIMITS["summary_max_bytes"]
    # Незакрытых скобок и декоративных обрывов быть не должно.
    assert _unmatched_brackets(text) == [], repr(text)
    assert _ends_with_dangling(text) is False, repr(text)


def _template(title, index, platform="PC"):
    """Один из шести шаблонов короткого описания (как в generate_short_description_ruble)."""
    upper = title.upper()
    colors = ["🔴", "🟡", "🟢", "🔵", "🟣"]
    emojis = ["🔥", "💎", "✨", "🌟", "⚡"]
    templates = [
        f"⚠️{colors[0]}⚠️【 {upper} 】{colors[0]}【 АВТОВЫДАЧА 】{colors[0]} ⚠️{colors[0]}",
        f"💗🌸{upper}💗🌸АВТОВЫДАЧА💗🌸+БОНУС ЗА ОТЗЫВ💗🌸",
        f"【🤍】🛫✨ ◈ {upper} 🎮 ◈ 🛫✨【АВТОВЫДАЧА】",
        f"⚜️💵⚜️ {upper} ⚜️💵⚜️ {platform} ⚜️💵⚜️ АВТО-ВЫДАЧА 24/7 ⚜️💵⚜️",
        f"🟥⬛🟥 {upper} 🟥⬛🟥 2026 АКТУАЛЬНО 🟥⬛🟥 УСТОЙЧИВЫ К БЛОКАМ 🟥⬛🟥",
        f"{emojis[0]}{colors[1]}【{platform}】{emojis[1]}【{upper}】{emojis[2]}【АВТОВЫДАЧА 24/7】{colors[2]}",
    ]
    return templates[index]


def test_long_title_fits_all_six_summary_styles(monkeypatch):
    """Каждый исходный стиль должен укладываться без посимвольного мусора."""
    for style_index in range(6):
        monkeypatch.setattr(
            description_builder.random,
            "choice",
            lambda sequence, index=style_index: sequence[index],
        )
        summary = generate_short_description_ruble(LONG_TITLE)
        _assert_summary_limits(summary)


def test_all_six_templates_keep_useful_words_and_decoration():
    """Все шесть шаблонов: лимиты, незакрытые скобки и сохранность оформления."""
    for index in range(6):
        raw = _template(PC_TITLE, index)
        summary = fit_summary(raw, title=PC_TITLE)

        _assert_summary_limits(summary)
        assert "БОНУС" not in summary
        assert "АВТО" not in summary
        # Название не должно схлопываться в первое слово, если позволяют лимиты.
        assert "МАНУАЛ" in summary
        assert len(summary) > 20, repr(summary)
        # Хотя бы часть оформления шаблона остаётся.
        assert description_builder._is_emoji_char(summary[0]) or summary[0] in "【"


def test_russian_summary_with_cyrillic_and_emoji():
    raw = _template("ТАКТИКА КОМАНДНЫХ ДРАК В СЛОЖНОЙ ИГРЕ", 0)
    summary = fit_summary(raw, title="ТАКТИКА КОМАНДНЫХ ДРАК В СЛОЖНОЙ ИГРЕ")

    _assert_summary_limits(summary)
    assert "ТАКТИКА" in summary
    assert "⚠️" in summary


def test_summary_removes_bonus_and_auto_delivery_before_word_cut():
    raw = (
        f"💗🌸{LONG_TITLE}💗🌸АВТО-ВЫДАЧА 24/7💗🌸"
        "+БОНУС ЗА ОТЗЫВ💗🌸"
    )
    summary = fit_summary(raw, title=LONG_TITLE)

    _assert_summary_limits(summary)
    assert "БОНУС" not in summary
    assert "АВТО" not in summary


def test_english_summary_uses_the_same_safe_limits():
    raw = (
        "TOP SECRETS FOR MAXIMUM FARM IN A VERY LONG GAME TITLE "
        "AUTOMATIC DELIVERY 24/7 BONUS FOR REVIEW EXTRA WORDS "
        "AND MORE DETAILS"
    )
    summary = fit_summary(raw, is_en=True)

    assert LIMITS["summary_min"] <= len(summary) <= LIMITS["summary_max"]
    assert len(summary.encode("utf-8")) <= LIMITS["summary_max_bytes"]
    assert all(ord(char) < 128 for char in summary)
    assert re.search(r"\w$", summary)
    assert "BONUS" not in summary
    assert "DELIVERY" not in summary


def test_all_six_full_descriptions_have_explicit_auto_delivery(monkeypatch):
    idea = {"title": LONG_TITLE, "content_points": ["Фарм", "Билды"]}

    for style_index in range(6):
        monkeypatch.setattr(
            description_builder.random,
            "choice",
            lambda sequence, index=style_index: sequence[index],
        )
        full_description = generate_full_description_ruble(idea, "Тестовая игра")
        assert "🕒АВТОВЫДАЧА 24/7🕒" in full_description


def test_seventy_characters_limit_is_respected():
    for title in (PC_TITLE, PING_TITLE, HWID_TITLE, LONG_TITLE):
        for index in range(6):
            summary = fit_summary(_template(title, index), title=title)
            assert len(summary) <= LIMITS["summary_max"], repr(summary)


def test_hundred_twenty_utf8_bytes_limit_is_respected():
    for title in (PC_TITLE, PING_TITLE, HWID_TITLE, LONG_TITLE):
        for index in range(6):
            summary = fit_summary(_template(title, index), title=title)
            assert len(summary.encode("utf-8")) <= LIMITS["summary_max_bytes"], repr(summary)


def test_both_limits_are_checked_together():
    """Кириллица стоит двух байт: символьный и байтовый лимит связаны."""
    title = "ОЧЕНЬ ДЛИННОЕ НАЗВАНИЕ ТОВАРА ДЛЯ МАКСИМАЛЬНОГО ФАРМА"
    for index in range(6):
        summary = fit_summary(_template(title, index), title=title)
        assert len(summary) <= LIMITS["summary_max"]
        assert len(summary.encode("utf-8")) <= LIMITS["summary_max_bytes"]
        # Байтовый лимит не должен быть исчерпан впустую при короткой строке.
        assert len(summary) > 10


def test_variation_selector_is_never_broken():
    raw = _template("ТАКТИКА КОМАНДНЫХ ДРАК В СЛОЖНОЙ ИГРЕ", 0)
    summary = fit_summary(raw, title="ТАКТИКА КОМАНДНЫХ ДРАК В СЛОЖНОЙ ИГРЕ")

    _assert_summary_limits(summary)
    for index, char in enumerate(summary):
        if char == "\ufe0f":
            assert index > 0 and description_builder._is_emoji_char(summary[index - 1]), repr(summary)
    assert "\ufe0f\ufe0f" not in summary


def test_no_unclosed_brackets_left():
    for title in (HWID_TITLE, PC_TITLE, LONG_TITLE):
        for index in range(6):
            summary = fit_summary(_template(title, index), title=title)
            assert _unmatched_brackets(summary) == [], repr(summary)


def test_words_are_never_cut_in_half():
    title = PC_TITLE
    raw = _template(title, 0)
    summary = fit_summary(raw, title=title)

    source_words = set(re.findall(r"[A-Za-zА-Яа-яЁё0-9+]+", raw.upper()))
    for word in summary.split():
        cleaned = word.strip("【】[]()«»")
        if not re.search(r"[A-Za-zА-Яа-яЁё0-9]", cleaned):
            continue  # декоративный символ, а не слово
        assert cleaned in source_words, (cleaned, summary)


def test_longest_allowed_candidate_is_chosen():
    """Раньше возвращалось только первое слово названия (32–37 символов)."""
    raw = _template(PC_TITLE, 0)
    assert len(raw) >= 88

    summary = fit_summary(raw, title=PC_TITLE)

    _assert_summary_limits(summary)
    assert len(summary) > 45, repr(summary)
    assert "НАСТРОЙКА" in summary or "NVIDIA" in summary, repr(summary)


def test_no_early_shortening_from_ninety_to_thirty_seven():
    """90 символов превращались в 37, хотя лимиты позволяют сохранить больше."""
    raw = _template(PC_TITLE, 0)
    assert len(raw) == 90
    assert len(raw.encode("utf-8")) >= 160

    summary = fit_summary(raw, title=PC_TITLE)

    assert len(summary) >= 50, repr(summary)
    assert len(summary.encode("utf-8")) >= 100, repr(summary)
    assert "МАНУАЛ ПО ОПТИМИЗАЦИИ ПК ДЛЯ ИГР" in summary, repr(summary)


def test_short_title_keeps_whole_template():
    summary = fit_summary(_template("ФАРМ ДЕНЕГ", 0), title="ФАРМ ДЕНЕГ")

    _assert_summary_limits(summary)
    assert "ФАРМ ДЕНЕГ" in summary
    assert "АВТОВЫДАЧА" in summary


def test_summary_shortening_is_written_to_file_not_console(tmp_path, monkeypatch, capsys):
    from modules.logger import BotLogger

    file_logger = BotLogger(error_log=str(tmp_path / "errors.log"))
    monkeypatch.setattr(description_builder, "logger", file_logger)

    fit_summary(f"{LONG_TITLE} АВТОВЫДАЧА 24/7 +БОНУС ЗА ОТЗЫВ", title=LONG_TITLE)

    output = capsys.readouterr().out
    assert "summary shortened" not in output

    log_text = (tmp_path / "errors.log").read_text(encoding="utf-8")
    assert "summary shortened" in log_text
    assert "level=" in log_text
