# Тесты бота FunPay v18
# Запуск: .venv/bin/pytest tests/ -v

import sys
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

# Мокаем g4f до импорта модулей бота
class _MockG4f:
    class ChatCompletion:
        @staticmethod
        def create(**kwargs):
            return "Mock response"
sys.modules['g4f'] = _MockG4f()

# Мокаем dotenv — модули бота делают `from dotenv import load_dotenv`
import types as _types
_dotenv_stub = _types.ModuleType("dotenv")
_dotenv_stub.load_dotenv = lambda *args, **kwargs: False
sys.modules['dotenv'] = _dotenv_stub

# Мокаем slugify
def _mock_slugify(text, **kwargs):
    return text.lower().replace(' ', '-')[:50]
sys.modules['slugify'] = type('mod', (), {'slugify': _mock_slugify})()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from modules.lot_publisher import enforce_limits, truncate_to_bytes
from modules.ai_generator import _is_refused, _count_words, _is_guide_good, MIN_WORDS, MIN_CHARS
import main as bot_main


# ═══════════════════════════════════════════════════════════════════════
# 1. truncate_to_bytes — обрезка по байтам (эмодзи!)
# ═══════════════════════════════════════════════════════════════════════

class TestTruncateToBytes:
    """Эмодзи = 3-4 байта в UTF-8. FunPay считает байты, не символы."""

    def test_ascii_text_unchanged(self):
        text = "Hello World"
        assert truncate_to_bytes(text, 100) == text

    def test_ascii_text_truncated(self):
        text = "Hello World"
        result = truncate_to_bytes(text, 5)
        assert result == "Hello"
        assert len(result.encode('utf-8')) <= 5

    def test_russian_text(self):
        """Русские буквы = 2 байта каждая."""
        text = "Привет"
        result = truncate_to_bytes(text, 6)
        assert len(result.encode('utf-8')) <= 6
        assert result == "При"  # 3 буквы x 2 байта = 6

    def test_emoji_not_broken(self):
        """⚠️ = 6 байт (⚠=3 + ️=3). Не должно рваться посреди эмодзи."""
        text = "A⚠️B"
        result = truncate_to_bytes(text, 4)
        encoded = result.encode('utf-8')
        assert len(encoded) <= 4
        # Результат "A" (1 байт), т.к. ⚠️ = 6 байт не влезает в лимит 4
        assert result == "A"

    def test_emoji_fits_exactly(self):
        """Эмодзи вписывается ровно в лимит."""
        text = "A⚠️"
        # A=1 + ⚠️=6 = 7 байт
        result = truncate_to_bytes(text, 7)
        assert result == "A⚠️"
        assert len(result.encode('utf-8')) == 7

    def test_emoji_4bytes(self):
        """🔴 = 4 байта."""
        text = "🔴🔴🔴"
        result = truncate_to_bytes(text, 9)
        encoded = result.encode('utf-8')
        assert len(encoded) <= 9

    def test_mixed_russian_emoji(self):
        """Реальный заголовок: эмодзи + русский текст."""
        text = "⚠️🔴⚠️【 ТАКТИКА КОМАНДНЫХ ДРАК 】🔴【 АВТОВЫДАЧА 】🔴 ⚠️🔴"
        result = truncate_to_bytes(text, 120)
        assert len(result.encode('utf-8')) <= 120
        assert len(result) > 0

    def test_exact_byte_limit(self):
        text = "ABC"  # 3 байта
        assert truncate_to_bytes(text, 3) == "ABC"

    def test_empty_string(self):
        assert truncate_to_bytes("", 100) == ""

    def test_zero_limit(self):
        """Лимит 0 — возвращает пустую строку."""
        assert truncate_to_bytes("Hello", 0) == ""

    def test_multiple_emoji_truncation(self):
        """Несколько эмодзи — обрезаем целыми."""
        text = "🔴🟡🟢🔵"  # 4x4 = 16 байт
        result = truncate_to_bytes(text, 10)
        encoded = result.encode('utf-8')
        assert len(encoded) <= 10
        # Должно быть 🔴🟡 (8 байт), а не сломанный эмодзи
        assert result == "🔴🟡"


# ═══════════════════════════════════════════════════════════════════════
# 2. enforce_limits — паддинг + обрезка + байтовый лимит
# ═══════════════════════════════════════════════════════════════════════

class TestEnforceLimits:

    def test_short_text_padded_ru(self):
        result = enforce_limits("Коротко", 50, 1000)
        assert len(result) >= 50

    def test_short_text_padded_en(self):
        result = enforce_limits("Short", 300, 1000, is_en=True)
        assert len(result) >= 300

    def test_long_text_truncated(self):
        text = "А" * 500
        result = enforce_limits(text, 10, 100)
        assert len(result) <= 100

    def test_byte_limit_applied(self):
        text = "⚠️🔴⚠️" + "А" * 200
        result = enforce_limits(text, 10, 1000, max_bytes=50)
        assert len(result.encode('utf-8')) <= 50

    def test_single_line(self):
        text = "Строка1\nСтрока2\r\nСтрока3"
        result = enforce_limits(text, 10, 1000, single_line=True)
        assert "\n" not in result
        assert "\r" not in result

    def test_en_characters_only(self):
        text = "Hello Привет 123!@#"
        result = enforce_limits(text, 10, 1000, is_en=True)
        for c in result:
            if c.isalpha():
                assert c.isascii(), f"Не-ASCII символ: {c}"

    def test_empty_input_padded(self):
        result = enforce_limits("", 100, 1000)
        assert len(result) >= 100

    def test_none_input_padded(self):
        result = enforce_limits(None, 100, 1000)
        assert len(result) >= 100


# ═══════════════════════════════════════════════════════════════════════
# 3. _is_refused — детекция отказов ИИ
# ═══════════════════════════════════════════════════════════════════════

class TestIsRefused:

    def test_russian_refusal(self):
        assert _is_refused("Я не могу написать этот гайд") is True
        assert _is_refused("Я не могу принять такие условия") is True
        assert _is_refused("Отказываюсь продолжать") is True

    def test_english_refusal(self):
        assert _is_refused("I am an AI language model") is True
        assert _is_refused("As an AI, I cannot") is True
        assert _is_refused("I can't do that") is True

    def test_normal_text_not_refused(self):
        assert _is_refused("В этом гайде мы разберем лучшие тактики для фарма") is False
        assert _is_refused("Step 1: Open the game and go to settings") is False
        assert _is_refused("Секреты прохождения и оптимальные билды") is False

    def test_policy_refusal(self):
        assert _is_refused("Это противоречит политике компании") is True
        assert _is_refused("Этически сомнительный контент") is True

    def test_ai_reveal(self):
        assert _is_refused("Я ИИ и не могу играть в игры") is True
        assert _is_refused("Как нейросеть я не рекомендую") is True

    def test_case_insensitive(self):
        assert _is_refused("I AM AN AI") is True
        assert _is_refused("Я НЕ МОГУ") is True

    def test_partial_match(self):
        long_text = "Ну короче, я не могу написать гайд по этой теме, потому что это нарушает правила."
        assert _is_refused(long_text) is True


# ═══════════════════════════════════════════════════════════════════════
# 4. _count_words / _is_guide_good — качество гайда
# ═══════════════════════════════════════════════════════════════════════

class TestGuideQuality:

    def test_count_words_basic(self):
        assert _count_words("Одно два три четыре пять") == 5

    def test_count_words_empty(self):
        assert _count_words("") == 0

    def test_count_words_newlines(self):
        assert _count_words("Строка1\nСтрока2\nСтрока3") == 3

    def test_guide_good_long(self):
        long_guide = "Слово " * 500
        assert _is_guide_good(long_guide) is True

    def test_guide_bad_short(self):
        short = "Короткий гайд на пару слов"
        assert _is_guide_good(short) is False

    def test_guide_bad_none(self):
        assert _is_guide_good(None) is False

    def test_guide_bad_empty(self):
        assert _is_guide_good("") is False

    def test_guide_borderline_chars(self):
        words = "а " * 400  # 400 слов но ~800 символов
        assert _is_guide_good(words) is False  # < MIN_CHARS

    def test_real_guide_format(self):
        guide = ""
        for i in range(10):
            guide += f"\n## Раздел {i+1}\n\n"
            for j in range(40):
                guide += f"Это подробный текст раздела {i+1} пункт {j+1} с конкретными советами по игре. "
        assert _is_guide_good(guide) is True


# ═══════════════════════════════════════════════════════════════════════
# 5. FunPay error detection — типы ошибок
# ═══════════════════════════════════════════════════════════════════════

class TestFunPayErrors:

    def test_short_en_error(self):
        errors = [['fields[desc][en]', 'Составление слишком короткого английского описания является нарушением наших правил.']]
        err_str = str(errors).lower()
        assert 'короткого английского' in err_str

    def test_long_ru_error(self):
        errors = [['fields[summary][ru]', 'Слишком длинный текст.']]
        err_str = str(errors).lower()
        assert 'слишком длинный' in err_str

    def test_limit_reached_error(self):
        errors = 'Много предложений в разделе «Услуги», удалите ненужные.'
        err_str = str(errors).lower()
        assert any(kw in err_str for kw in ['много предложений', 'удалите ненужные'])

    def test_server_id_error(self):
        errors = [['server_id', 'Укажите сервер.']]
        err_str = str(errors).lower()
        assert 'укажите сервер' in err_str

    def test_cloudflare_keywords(self):
        resp_403 = "cf-browser-verification"
        resp_moment = "just a moment... cloudflare"
        assert "cf-browser-verification" in resp_403.lower()
        assert "cloudflare" in resp_moment.lower()


# ═══════════════════════════════════════════════════════════════════════
# 6. Config limits — проверка что лимиты адекватные
# ═══════════════════════════════════════════════════════════════════════

class TestConfigLimits:

    def test_import_config(self):
        import config
        assert hasattr(config, 'LIMITS')

    def test_summary_limits(self):
        import config
        L = config.LIMITS
        assert L['summary_min'] < L['summary_max']
        assert L['summary_max'] > 0

    def test_en_min_sufficient(self):
        import config
        assert config.LIMITS['description_min_en'] >= 500

    def test_payment_msg_min(self):
        import config
        assert config.LIMITS['payment_msg_min_en'] >= 300

    def test_byte_limit_exists(self):
        import config
        assert 'summary_max_bytes' in config.LIMITS
        assert config.LIMITS['summary_max_bytes'] > 0

    def test_user_agents_exist(self):
        import config
        assert len(config.USER_AGENTS) > 0

    def test_emoji_bank_exists(self):
        import config
        assert len(config.EMOJI_BANK) > 0


# ═══════════════════════════════════════════════════════════════════════
# 7. Тихий консольный вывод: только короткий итог по товару
# ═══════════════════════════════════════════════════════════════════════

class TestQuietConsole:

    def test_success_prints_one_short_line(self, capsys):
        import main

        main._print_lot_status(7, 15, True)
        output = capsys.readouterr().out
        assert output.strip() == "[7/15] ✅ УСПЕШНО ВЫСТАВЛЕНО!"

    def test_failure_prints_short_reason(self, capsys):
        import main

        main._print_lot_status(3, 15, False, "сетевая ошибка")
        output = capsys.readouterr().out
        assert output.strip() == "[3/15] ❌ НЕ ВЫСТАВЛЕНО: сетевая ошибка"

    def test_failure_without_reason_stays_short(self, capsys):
        import main

        main._print_lot_status(1, 2, False)
        output = capsys.readouterr().out
        assert output.strip() == "[1/2] ❌ НЕ ВЫСТАВЛЕНО: ошибка публикации"

    def test_every_failure_result_has_a_reason(self):
        import main

        for result in (
            None, "no_csrf", "cloudflare_banned", "limit_reached",
            "too_short_en", "too_long_ru", "network_error",
        ):
            assert result in main._FAILURE_REASONS, result

    def test_success_is_never_a_failure_reason(self):
        import main

        assert "success" not in main._FAILURE_REASONS

    def test_publish_lot_does_not_print_internal_messages(self):
        """Внутренних сообщений о моделях, таймингах и summary в консоли нет."""
        source = open("modules/lot_publisher.py", encoding="utf-8").read()
        assert "print(" not in source


# ═══════════════════════════════════════════════════════════════════════
# 8. Сбой ИИ не должен стирать список игр
# ═══════════════════════════════════════════════════════════════════════

class _FakeGameData(dict):
    """Минимальные данные игры, которые нужны process_single_game."""


def _run_game_without_guides(monkeypatch, capsys):
    """
    Прогоняет одну игру, где ИИ не отдал ни одного гайда.
    Возвращает (removed_games, marked_games).
    """
    removed = []
    marked = []

    monkeypatch.setattr(bot_main, "_generate_ideas_timed", lambda name: ([], 0.01))
    monkeypatch.setattr(bot_main, "_generate_content_timed", lambda idea, game: (None, 0.01))
    monkeypatch.setattr(
        bot_main,
        "find_funpay_category",
        lambda session, name: {"id": 42, "type": "games", "game_id": 42},
    )
    monkeypatch.setattr(bot_main, "get_category_info", lambda session, game_id: "Games")
    monkeypatch.setattr(bot_main, "is_game_processed", lambda game_id: False)
    monkeypatch.setattr(bot_main, "build_final_ideas", lambda *a, **k: [{"title": "PC Optimization"}])
    monkeypatch.setattr(bot_main.gdrive, "create_folder", lambda name: "folder-id")
    monkeypatch.setattr(bot_main, "remove_game_from_list", lambda name: removed.append(name))
    monkeypatch.setattr(bot_main, "mark_as_processed", lambda game_id: marked.append(game_id))
    monkeypatch.setattr(
        bot_main,
        "check_wemod_availability",
        lambda name: (_ for _ in ()).throw(AssertionError("WeMod не должен запускаться")),
    )
    monkeypatch.setattr(bot_main, "_publish_with_heal", lambda *a, **k: ("success", None, None))
    monkeypatch.setattr(bot_main.time, "sleep", lambda seconds: None)

    result = bot_main.process_single_game(
        session=object(),
        game_data=_FakeGameData({"game_name": "Some Game"}),
        net_session=object(),
    )
    captured = capsys.readouterr()
    return removed, marked, result, captured.out


class TestAiOutageKeepsGamesList:

    def test_game_is_not_removed_when_no_guides(self, monkeypatch, capsys):
        removed, marked, result, output = _run_game_without_guides(monkeypatch, capsys)

        assert removed == [], "игру нельзя убирать из списка, пока ИИ не выдал гайды"
        assert marked == [], "игру нельзя помечать обработанной без гайдов"
        assert "остаётся в списке" in output
        assert "УСПЕШНО ВЫСТАВЛЕНО" not in output

    def test_nothing_is_published_without_guides(self, monkeypatch, capsys):
        published = []

        def _publish(session, net_session, game_id, short, full, pay_msg, price, **kwargs):
            published.append(game_id)
            return "success", session, net_session

        monkeypatch.setattr(bot_main, "_publish_with_heal", _publish)
        _run_game_without_guides(monkeypatch, capsys)
        assert published == []


# ═══════════════════════════════════════════════════════════════════════
# 9. Проверка модели перед началом работы (Worker должен её знать)
# ═══════════════════════════════════════════════════════════════════════

class TestPreflightAi:

    def test_unknown_model_returns_false(self, monkeypatch, capsys):
        monkeypatch.setattr(
            bot_main.ai_client,
            "check_model_support",
            lambda models=None: (["gemini-3.6-flash"], ["gemini-2.5-flash"]),
        )
        monkeypatch.setattr(bot_main.time, "sleep", lambda seconds: None)

        assert bot_main._preflight_ai() is False
        output = capsys.readouterr().out
        assert "не знает модель" in output
        assert "wrangler deploy" in output

    def test_known_model_returns_true(self, monkeypatch):
        monkeypatch.setattr(bot_main.ai_client, "check_model_support", lambda models=None: ([], ["gemini-3.6-flash"]))
        assert bot_main._preflight_ai() is True

    def test_silent_worker_returns_true(self, monkeypatch):
        """Старый Worker без списка моделей не должен блокировать запуск."""
        monkeypatch.setattr(bot_main.ai_client, "check_model_support", lambda models=None: ([], []))
        assert bot_main._preflight_ai() is True


# ═══════════════════════════════════════════════════════════════════════
# 10. Ожидание гайда не должно выглядеть как зависание
# ═══════════════════════════════════════════════════════════════════════

class TestGuideHeartbeat:

    def test_fast_guide_is_silent(self, capsys):
        """Быстрый гайд не добавляет в консоль лишних строк."""
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: ("гайд", 0.1))
            result = bot_main._await_content(future)

        assert result == ("гайд", 0.1)
        assert "ещё считается" not in capsys.readouterr().out

    def test_slow_guide_prints_heartbeat(self, monkeypatch, capsys):
        monkeypatch.setattr(bot_main, "GUIDE_HEARTBEAT_SECONDS", 0.05)

        def _slow():
            time.sleep(0.4)
            return "длинный гайд", 0.4

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_slow)
            result = bot_main._await_content(future)

        assert result == ("длинный гайд", 0.4)
        output = capsys.readouterr().out
        assert "Гайд ещё считается" in output
        # heartbeat не выдаёт ни модели, ни текста гайда
        assert "gemini" not in output and "длинный гайд" not in output

    def test_heartbeat_repeats(self, monkeypatch, capsys):
        """Если гайд считается минуты, строка «всё ещё считаем» повторяется."""
        monkeypatch.setattr(bot_main, "GUIDE_HEARTBEAT_SECONDS", 0.05)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: time.sleep(2.2) or ("гайд", 2.2))
            bot_main._await_content(future)

        assert capsys.readouterr().out.count("Гайд ещё считается") >= 2


# ═══════════════════════════════════════════════════════════════════════
# 11. Сбой поиска раздела и сбой ИИ не должны стирать список игр
# ═══════════════════════════════════════════════════════════════════════

def _run_game_with_error(monkeypatch, capsys, error):
    """Прогоняет игру, где поиск раздела падает с заданной ошибкой."""
    removed = []
    marked = []

    monkeypatch.setattr(bot_main, "_generate_ideas_timed", lambda name: ([], 0.01))
    monkeypatch.setattr(
        bot_main,
        "find_funpay_category",
        lambda session, name: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(bot_main, "remove_game_from_list", lambda name: removed.append(name))
    monkeypatch.setattr(bot_main, "mark_as_processed", lambda game_id: marked.append(game_id))

    bot_main.process_single_game(
        session=object(),
        game_data=_FakeGameData({"game_name": "Some Game"}),
        net_session=object(),
    )
    return removed, marked, capsys.readouterr().out


class TestLookupFailureKeepsGamesList:

    def test_network_error_keeps_game(self, monkeypatch, capsys):
        removed, marked, output = _run_game_with_error(
            monkeypatch, capsys, bot_main.CategoryLookupError("сетевая ошибка")
        )

        assert removed == [], "сетевой сбой не должен удалять игру из списка"
        assert marked == []
        assert "Не удалось проверить игру на FunPay" in output
        assert "остаётся в списке" in output

    def test_ai_error_keeps_game(self, monkeypatch, capsys):
        """Недоступный Worker отпускает игру в главный цикл, но не удаляет её."""
        removed = []
        monkeypatch.setattr(bot_main, "_generate_ideas_timed", lambda name: ([], 0.01))
        monkeypatch.setattr(
            bot_main,
            "find_funpay_category",
            lambda session, name: (_ for _ in ()).throw(
                bot_main.ai_client.AIUnavailable("Worker недоступен")
            ),
        )
        monkeypatch.setattr(bot_main, "remove_game_from_list", lambda name: removed.append(name))
        monkeypatch.setattr(bot_main, "mark_as_processed", lambda game_id: removed.append(game_id))

        with pytest.raises(bot_main.ai_client.AIUnavailable):
            bot_main.process_single_game(
                session=object(),
                game_data=_FakeGameData({"game_name": "Some Game"}),
                net_session=object(),
            )

        assert removed == [], "сбой ИИ не должен удалять игру из списка"
        output = capsys.readouterr().out
        assert "ИИ недоступен" in output
        assert "остаётся в списке" in output

    def test_plain_ai_error_keeps_game(self, monkeypatch, capsys):
        """Обычная ошибка ИИ не роняет обработку и не удаляет игру."""
        removed, marked, output = _run_game_with_error(
            monkeypatch, capsys, bot_main.ai_client.AIError("пустой ответ")
        )

        assert removed == [], "ошибка ИИ не должна удалять игру из списка"
        assert marked == []
        assert "ИИ не ответил" in output
        assert "остаётся в списке" in output

    def test_unexpected_error_keeps_game(self, monkeypatch, capsys):
        removed, marked, output = _run_game_with_error(
            monkeypatch, capsys, RuntimeError("неожиданный сбой")
        )

        assert removed == [], "неожиданный сбой не должен удалять игру из списка"
        assert marked == []
        assert "Ошибка в игре" in output
        assert "остаётся в списке" in output
