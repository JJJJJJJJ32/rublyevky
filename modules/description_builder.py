import random
import re
import string

from config import EMOJI_BANK, COLOR_BANK, LIMITS
from modules.logger import logger


# Сначала убираем из заголовка необязательные хвосты, а уже потом
# сокращаем название товара. Это лучше, чем резать строку посередине слова.
_BONUS_RE = re.compile(
    r"(?:[+➕]\s*)?(?:БОНУС|BONUS|ПОДАРОК|GIFT)\b"
    r"(?:\s+(?:(?:ЗА|FOR)\s+)?(?:ОТЗЫВ\w*|REVIEW\w*|FEEDBACK))?"
    r"(?:\s*[^\w\s]+)*",
    re.IGNORECASE,
)
_AUTO_DELIVERY_RE = re.compile(
    r"(?:"
    r"АВТО(?:МАТИЧЕСКАЯ)?[-\s]?ВЫДАЧА"
    r"|AUTO(?:MATIC)?[-\s]?DELIVERY"
    r"|INSTANT\s+(?:AUTO(?:MATIC)?\s+)?DELIVERY"
    r")"
    r"(?:\s*24\s*/\s*7)?",
    re.IGNORECASE,
)

_SUMMARY_LEVELS = {
    1: "bonus_removed",
    2: "auto_delivery_removed",
    3: "title_shortened",
    4: "words_shortened",
}


def _clean_summary_text(text, is_en=False):
    """Приводит summary к одной строке и убирает недопустимые символы для en."""
    text = str(text or "").replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text).strip()

    if is_en:
        allowed = string.ascii_letters + string.digits + string.punctuation + " "
        text = "".join(char for char in text if char in allowed)
        text = re.sub(r"\s+", " ", text).strip()

    return text


def _summary_fits(text, min_chars, max_chars, max_bytes):
    if not text or len(text) < min_chars or len(text) > max_chars:
        return False
    return max_bytes <= 0 or len(text.encode("utf-8")) <= max_bytes


def _trim_decoration_edges(text):
    """Убирает только декор по краям, не обрезая последнее слово."""
    text = text.strip()
    text = re.sub(r"^[^\w]+", "", text, flags=re.UNICODE)
    text = re.sub(r"[^\w]+$", "", text, flags=re.UNICODE)
    return text.strip()


def _replace_title(text, title, replacement):
    if not title:
        return text
    pattern = re.compile(re.escape(title.strip()), re.IGNORECASE)
    replaced, count = pattern.subn(replacement, text, count=1)
    return replaced if count else text


def _fallback_summary(is_en):
    return "Gaming guide" if is_en else "Игровой гайд"


def _fit_summary(text, title=None, is_en=False, min_chars=10, max_chars=70, max_bytes=120):
    """Возвращает (подходящий summary, уровень сокращения)."""
    original = _clean_summary_text(text, is_en=is_en)
    current = original
    level = 0

    if _summary_fits(current, min_chars, max_chars, max_bytes):
        return current, level

    # Уровень 1: бонус за отзыв не помогает покупателю понять товар.
    without_bonus = _clean_summary_text(_BONUS_RE.sub(" ", current), is_en=is_en)
    if without_bonus != current:
        current = without_bonus
        level = 1
    if _summary_fits(current, min_chars, max_chars, max_bytes):
        return _trim_decoration_edges(current), level

    # Уровень 2: авто-выдача уже подробно указана в описании лота.
    without_delivery = _clean_summary_text(_AUTO_DELIVERY_RE.sub(" ", current), is_en=is_en)
    if without_delivery != current:
        current = without_delivery
        level = max(level, 2)
    if _summary_fits(current, min_chars, max_chars, max_bytes):
        return _trim_decoration_edges(current), level

    # Уровень 3: если передали название, постепенно оставляем только его
    # первые слова. Целое слово всегда лучше обрыва по символам.
    shortest_title_version = current
    title_words = re.findall(r"\S+", str(title or "").strip())
    if title_words:
        for word_count in range(len(title_words) - 1, 0, -1):
            short_title = " ".join(title_words[:word_count])
            candidate = _clean_summary_text(
                _replace_title(current, str(title).strip(), short_title),
                is_en=is_en,
            )
            shortest_title_version = candidate
            if _summary_fits(candidate, min_chars, max_chars, max_bytes):
                return _trim_decoration_edges(candidate), 3

        # В сложном заголовке могут быть квадратные скобки, разные пробелы
        # или один очень длинный токен. Короткая нейтральная замена лучше,
        # чем разрыв такого слова.
        short_title = "GUIDE" if is_en else "ГАЙД"
        candidate = _clean_summary_text(
            _replace_title(current, str(title).strip(), short_title),
            is_en=is_en,
        )
        shortest_title_version = candidate
        if _summary_fits(candidate, min_chars, max_chars, max_bytes):
            return _trim_decoration_edges(candidate), 3

    # Уровень 4: оставляем самое длинное начало, состоящее из целых слов.
    # Заголовки с декоративными символами по краям тоже не ломаются: декор
    # снимается только после выбора целого слова.
    for source in (shortest_title_version, current):
        words = source.split()
        for word_count in range(len(words), 0, -1):
            candidate = _trim_decoration_edges(" ".join(words[:word_count]))
            if _summary_fits(candidate, min_chars, max_chars, max_bytes):
                return candidate, 4

    # Даже одно слово может оказаться длиннее лимита. В таком случае
    # выбрасываем его целиком, а не режем посередине.
    fallback = _fallback_summary(is_en)
    return fallback, 4


def fit_summary(text, title=None, is_en=False):
    """Осознанно укладывает заголовок в лимиты FunPay.

    Порядок сокращения: бонус → авто-выдача → название по словам →
    общий обрез по целым словам. Функция не использует посимвольный срез.
    """
    result, level = _fit_summary(
        text,
        title=title,
        is_en=is_en,
        min_chars=LIMITS["summary_min"],
        max_chars=LIMITS["summary_max"],
        max_bytes=LIMITS["summary_max_bytes"],
    )

    if level:
        language = "en" if is_en else "ru"
        level_name = _SUMMARY_LEVELS[level]
        logger.info(
            "summary shortened: "
            f"language={language}, level={level_name}, "
            f"chars={len(_clean_summary_text(text, is_en=is_en))}->{len(result)}, "
            f"bytes={len(_clean_summary_text(text, is_en=is_en).encode('utf-8'))}->{len(result.encode('utf-8'))}"
        )

    return result


def generate_short_description_ruble(title, platform="PC"):
    """Генерирует уникальный заголовок в одном из 6 стилей."""
    e = random.sample(EMOJI_BANK, 5)
    c = random.sample(COLOR_BANK, 5)
    t = str(title).upper()
    p = str(platform).upper()

    variants = [
        f"⚠️{c[0]}⚠️【 {t} 】{c[0]}【 АВТОВЫДАЧА 】{c[0]} ⚠️{c[0]}",
        f"💗🌸{t}💗🌸АВТОВЫДАЧА💗🌸+БОНУС ЗА ОТЗЫВ💗🌸",
        f"【🤍】🛫✨ ◈ {t} 🎮 ◈ 🛫✨【АВТОВЫДАЧА】",
        f"⚜️💵⚜️ {t} ⚜️💵⚜️ {p} ⚜️💵⚜️ АВТО-ВЫДАЧА 24/7 ⚜️💵⚜️",
        f"🟥⬛🟥 {t} 🟥⬛🟥 2026 АКТУАЛЬНО 🟥⬛🟥 УСТОЙЧИВЫ К БЛОКАМ 🟥⬛🟥",
        f"{e[0]}{c[1]}【{p}】{e[1]}【{t}】{e[2]}【АВТОВЫДАЧА 24/7】{c[2]}",
    ]
    raw = random.choice(variants)
    return fit_summary(raw, title=t)


def generate_full_description_ruble(idea, game_name):
    """Выбирает один из 6 более живых стилей подробного описания."""
    title = idea["title"].upper()
    game = game_name.upper()
    points = "\n".join(f"• {point}" for point in idea.get("content_points", []))
    sampled_emojis = random.sample(EMOJI_BANK, 3)

    # В каждом стиле ровно одна строка про способ выдачи. Остальные формулировки
    # отличаются, чтобы карточки не выглядели копиями друг друга.
    v1 = f"""
🕒АВТОВЫДАЧА 24/7🕒
Ссылка появляется сразу после оплаты — ждать ответа продавца не нужно.

Внутри лежит подробный материал по игре {game} и теме «{title}»:
{points}

Если открываешь файл впервые, начни с первого раздела и повторяй действия по порядку.
Там, где результат зависит от уровня персонажа, режима или экипировки, это отдельно
отмечено. Если что-то в игре выглядит иначе после обновления, напиши продавцу — разберёмся.
"""

    v2 = f"""
🕒АВТОВЫДАЧА 24/7🕒
Для {game}, когда нужен не общий совет из поиска, а понятный план под конкретную задачу.
Материал «{title}» собран короткими блоками: сначала подготовка, затем сам маршрут или
настройка, а после — несколько вариантов на случай, если основной способ не подошёл.

Что встретится внутри:
{points}

Сохрани файл после покупки: к нему удобно возвращаться перед новым заходом в игру.
"""

    v3 = f"""
🕒АВТОВЫДАЧА 24/7🕒
Допустим, ты застрял на одном месте или тратишь слишком много времени на повторяющиеся
действия. В этом материале по {game} разобрана именно такая ситуация — без длинной рекламы
и лишней теории.

Тема: {title}
{points}

Сначала проверь условия из первого абзаца. Это важно: один и тот же приём может работать
по-разному в одиночном режиме и в группе. В конце есть несколько запасных вариантов,
которые помогают, если нужный предмет или точка ещё недоступны.
"""

    v4 = f"""
🕒АВТОВЫДАЧА 24/7🕒
Объявление активно — файл можно получить в любое время, в том числе ночью.

Для игры {game} подготовлен материал «{title}». Он рассчитан на обычный игровой процесс:
без обещаний невозможного и без предложений покупать ещё пять похожих инструкций.
{points}

Перед применением сверяй название режима и текущую версию игры. Небольшая деталь вроде
неподходящего уровня сложности часто меняет маршрут, поэтому такие условия указаны рядом
с советом, а не спрятаны в конце.
"""

    v5 = f"""
🕒АВТОВЫДАЧА 24/7🕒
Здесь собрана практическая заметка для {game}: что подготовить, куда направиться и на каком
шаге обычно теряется время. Тема — «{title}».

Основные ориентиры:
{points}

Не обязательно проходить всё за один вечер. Сначала попробуй базовый вариант, а затем
перейди к дополнительным настройкам, если игра ведёт себя иначе. Так проще понять, какой
совет дал результат именно в твоём случае.
"""

    v6 = f"""
🕒АВТОВЫДАЧА 24/7🕒
{sampled_emojis[0]} Материал по {game} уже ждёт после оплаты.

Тема «{title}» объяснена человеческим языком: без повторяющихся лозунгов и без ровных
абзацев, написанных под копирку.
{points}

Открой файл, найди свою ситуацию и двигайся по шагам. Если игра предлагает несколько
вариантов выбора, в тексте указано, чем они отличаются и когда какой вариант выгоднее.
{sampled_emojis[1]} Вопросы по содержанию можно задать продавцу после покупки.
"""

    return random.choice([v1, v2, v3, v4, v5, v6]).strip()

def generate_short_description_wemod(game_name):
    c = random.sample(COLOR_BANK, 2)
    return f"{c[0]}{c[1]}{c[0]}【МОД МЕНЮ НА {game_name.upper()}】ЧИТАЙТЕ ОПИСАНИЕ{c[0]}{c[1]}{c[0]}"


def generate_full_description_wemod(game_name):
    """Расширенное описание WeMod — достаточно подробное для перевода на английский."""
    return f"""
🕒АВТОВЫДАЧА 24/7🕒
После оплаты откроется ссылка на мод для {game_name}. Внутри есть файл и инструкция по
установке: что распаковать, где найти игру и какие клавиши использовать. Выдача работает
круглосуточно, поэтому не нужно ждать, пока продавец появится в сети.

Перед запуском проверь версию игры и закрой лишние программы, которые могут мешать моду.
Сначала прочитай инструкцию целиком, затем включай только нужные функции. Если меняется
патч, напиши продавцу — информация обновляется под новые версии.

Что важно знать заранее:
• товар предназначен для личного использования;
• после получения ссылки проверь файл и напиши, если возникла техническая проблема;
• некоторые функции могут повлиять на аккаунт в сетевых режимах, поэтому тестировать их
  лучше на отдельном профиле;
• возврат после скачивания не предусмотрен.

Поддержка отвечает на вопросы по установке и настройке. Сохрани сообщение с ссылкой и
название версии мода, чтобы при обращении можно было быстро разобраться в ситуации.
""".strip()

def generate_payment_message(link):
    return f"Привет👋 Спасибо за покупку! Твой товар здесь: {link}\n\nОставь отзыв 🌟 и получишь подарок!"
