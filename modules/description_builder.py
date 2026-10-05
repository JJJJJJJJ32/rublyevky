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

# Парные обрамления: короткое описание не должно остаться с незакрытой скобкой.
_BRACKET_PAIRS = {
    "【": "】",
    "(": ")",
    "[": "]",
    "{": "}",
    "«": "»",
}
_OPENERS = "".join(_BRACKET_PAIRS)
_CLOSERS = "".join(_BRACKET_PAIRS.values())

# Одиночные декоративные знаки, которые выглядят оборванными в конце строки.
_DANGLING_DECOR = "◈◆◇□■●○★☆✦✧▪▫"

_EMPTY_BRACKETS_RE = re.compile(
    r"(?:【\s*】|\(\s*\)|\[\s*\]|\{\s*\}|«\s*»)"
)


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
    """Оба лимита FunPay проверяются одновременно: символы и UTF-8 байты."""
    if not text or len(text) < min_chars or len(text) > max_chars:
        return False
    return max_bytes <= 0 or len(text.encode("utf-8")) <= max_bytes


def _is_emoji_char(char):
    """Проверяет, что символ — основа эмодзи (без variation selector)."""
    code = ord(char)
    return (
        0x1F000 <= code <= 0x1FAFF
        or 0x1F1E6 <= code <= 0x1F1FF
        or 0x2190 <= code <= 0x21FF
        or 0x2300 <= code <= 0x23FF
        or 0x2B00 <= code <= 0x2BFF
        or 0x2600 <= code <= 0x27BF
        or char in "©®™"
    )


def _ends_with_complete_unit(text):
    """True, если строка не обрывается на «висячем» символе.

    Недопустимый хвост: открывающая скобка, дефис, слэш, запятая, одиночный
    variation selector (U+FE0F) и одинокий декоративный знак вроде ◈.
    """
    if not text:
        return False
    last = text[-1]
    if last.isalnum() or last == "_":
        return True
    if last in _CLOSERS:
        return True
    if last in _DANGLING_DECOR:
        return False
    if _is_emoji_char(last):
        return True
    # Variation selector допустим только вместе с базовым эмодзи.
    if last in "\ufe0f\ufe0e" and len(text) > 1 and _is_emoji_char(text[-2]):
        return True
    # Модификаторы цвета кожи — тоже часть кластера.
    if "\U0001F3FB" <= last <= "\U0001F3FF" and len(text) > 1 and _is_emoji_char(text[-2]):
        return True
    return False


def _trim_dangling_edges(text):
    """Снимает только оборванные хвосты, не трогая целые слова и оформление."""
    text = str(text or "").strip()
    while text and not _ends_with_complete_unit(text):
        text = text[:-1].rstrip()
    return text.strip()


def _drop_unmatched_brackets(text):
    """Убирает скобки без пары, чтобы не осталось незакрытых конструкций."""
    stack = []
    drop = set()
    for index, char in enumerate(text):
        if char in _OPENERS:
            stack.append((index, char))
        elif char in _CLOSERS:
            if stack and _BRACKET_PAIRS[stack[-1][1]] == char:
                stack.pop()
            else:
                drop.add(index)
    for index, _ in stack:
        drop.add(index)
    if not drop:
        return text
    return "".join(char for index, char in enumerate(text) if index not in drop)


def _read_emoji_cluster(text, start):
    """Читает один эмодзи вместе с variation selector и модификатором."""
    if start >= len(text) or not _is_emoji_char(text[start]):
        return None, start
    end = start + 1
    while end < len(text) and text[end] in "\ufe0f\ufe0e\u200d":
        end += 1
        if end < len(text) and _is_emoji_char(text[end]) and text[end - 1] == "\u200d":
            end += 1
    while end < len(text) and "\U0001F3FB" <= text[end] <= "\U0001F3FF":
        end += 1
    return text[start:end], end


def _collapse_repeated_emoji(text):
    """Схлопывает повторы одного и того же эмодзи: «🟡 🟡» → «🟡».

    После удаления бонуса или авто-выдачи по краям часто остаётся пара
    одинаковых декоративных символов. Разное оформление шаблона не трогаем.
    """
    result = []
    index = 0
    length = len(text)
    while index < length:
        cluster, next_index = _read_emoji_cluster(text, index)
        if cluster is None:
            result.append(text[index])
            index += 1
            continue
        result.append(cluster)
        index = next_index
        while index < length:
            probe = index
            while probe < length and text[probe] in " \t":
                probe += 1
            again, after = _read_emoji_cluster(text, probe)
            if again is None or again != cluster:
                break
            index = after
    return "".join(result)


def _finalize_summary(text, is_en=False):
    """Приводит кандидат к безопасному виду: баланс скобок и целые хвосты."""
    cleaned = _clean_summary_text(text, is_en=is_en)
    cleaned = _EMPTY_BRACKETS_RE.sub(" ", cleaned)
    cleaned = _collapse_repeated_emoji(cleaned)
    cleaned = _drop_unmatched_brackets(cleaned)
    cleaned = _trim_dangling_edges(cleaned)
    # Обрезка хвоста могла открыть висячую скобку — проверяем ещё раз.
    cleaned = _drop_unmatched_brackets(cleaned)
    cleaned = _trim_dangling_edges(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _replace_title(text, title, replacement):
    if not title:
        return text
    pattern = re.compile(re.escape(title.strip()), re.IGNORECASE)
    replaced, count = pattern.subn(replacement, text, count=1)
    return replaced if count else text


def _fallback_summary(is_en):
    return "Gaming guide" if is_en else "Игровой гайд"


def _fit_summary(text, title=None, is_en=False, min_chars=10, max_chars=70, max_bytes=120):
    """Возвращает (подходящий summary, уровень сокращения).

    Сначала собираются все осмысленные кандидаты: исходный заголовок, вариант
    без бонуса, вариант без авто-выдачи, варианты с названием, сокращённым по
    целым словам, и обрезка по целым словам. Ни один кандидат не режется
    посимвольно и не режется по байтам, поэтому эмодзи-кластеры и U+FE0F не
    рвутся. Из всех кандидатов выбирается самый длинный, который проходит
    сразу оба лимита FunPay: 70 символов и 120 UTF-8 байт.
    """
    original = _clean_summary_text(text, is_en=is_en)
    title_text = _clean_summary_text(title, is_en=is_en)

    candidates = []  # (уровень сокращения, готовый вариант)

    def _add(level, value):
        ready = _finalize_summary(value, is_en=is_en)
        if ready:
            candidates.append((level, ready))

    # Уровень 0: заголовок уже укладывается в лимиты.
    _add(0, original)

    # Уровень 1: бонус за отзыв не помогает покупателю понять товар.
    without_bonus = _clean_summary_text(_BONUS_RE.sub(" ", original), is_en=is_en)
    _add(1, without_bonus)

    # Уровень 2: авто-выдача уже подробно указана в описании лота.
    base = _clean_summary_text(_AUTO_DELIVERY_RE.sub(" ", without_bonus), is_en=is_en)
    _add(2, base)

    # Уровень 3: название сокращается целыми словами, оформление остаётся.
    title_words = title_text.split()
    if title_words:
        for word_count in range(len(title_words) - 1, 0, -1):
            short_title = " ".join(title_words[:word_count])
            _add(3, _replace_title(base, title_text, short_title))

        # В сложном заголовке могут быть квадратные скобки, разные пробелы
        # или один очень длинный токен. Короткая нейтральная замена лучше,
        # чем разрыв такого слова.
        _add(3, _replace_title(base, title_text, "GUIDE" if is_en else "ГАЙД"))

    # Уровень 4: оставляем самые длинные начала из целых слов. Заголовки с
    # декоративными символами по краям тоже не ломаются: декор снимается
    # только после выбора целого слова.
    for source in (base, without_bonus, original):
        words = source.split()
        for word_count in range(len(words), 0, -1):
            _add(4, " ".join(words[:word_count]))

    best = None
    best_level = 4
    best_key = None
    for level, candidate in candidates:
        if not _summary_fits(candidate, min_chars, max_chars, max_bytes):
            continue
        # Сначала менее агрессивный этап (бонус и авто-выдача убираются раньше
        # названия), а внутри этапа — самый длинный полезный вариант.
        key = (-level, len(candidate), -len(candidate.encode("utf-8")))
        if best_key is None or key > best_key:
            best, best_level, best_key = candidate, level, key

    if best is not None:
        return best, best_level

    # Даже одно слово может оказаться длиннее лимита. В таком случае
    # выбрасываем его целиком, а не режем посередине.
    return _fallback_summary(is_en), 4


def fit_summary(text, title=None, is_en=False):
    """Осознанно укладывает заголовок в лимиты FunPay.

    Порядок сокращения: бонус → авто-выдача → название по словам →
    общий обрез по целым словам. Функция не использует посимвольный срез
    и не режет строку по байтам. Уровень сокращения пишется только в
    logs/errors.log: в обычной консоли этого сообщения нет.
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
        source = _clean_summary_text(text, is_en=is_en)
        logger.info(
            "summary shortened: "
            f"language={language}, level={level_name}, "
            f"chars={len(source)}->{len(result)}, "
            f"bytes={len(source.encode('utf-8'))}->{len(result.encode('utf-8'))}"
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
