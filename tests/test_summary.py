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


def _assert_summary_limits(text):
    assert LIMITS["summary_min"] <= len(text) <= LIMITS["summary_max"]
    assert len(text.encode("utf-8")) <= LIMITS["summary_max_bytes"]
    # Последний символ — буква или цифра, а не половина слова/эмодзи.
    assert re.search(r"\w$", text, flags=re.UNICODE), repr(text)


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


def test_summary_shortening_is_logged(capsys):
    fit_summary(f"{LONG_TITLE} АВТОВЫДАЧА 24/7 +БОНУС ЗА ОТЗЫВ")
    output = capsys.readouterr().out
    assert "summary shortened" in output
    assert "level=" in output
