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
    """Выбирает один из 6 стилей подробного описания."""
    t = idea["title"].upper()
    gn = game_name.upper()
    points = "\n".join([f"• {p}" for p in idea.get("content_points", [])])
    e = random.sample(EMOJI_BANK, 5)

    # ВИД 1: ПОШАГОВЫЙ ГАЙД (Ваш пример 1)
    v1 = f"""
🕒АВТОВЫДАЧА 24/7🕒
Это значит, что вы получите товар автоматически после оплаты (даже если я не в сети) ✅
⚙️ ПОСЛЕ ОПЛАТЫ ВЫ ПОЛУЧИТЕ: ПОДРОБНОЕ РУКОВОДСТВО ПО {gn}, В КОТОРОМ:
────────────────────────────
{points}
──────────── ⋆⋅☆⋅⋆ ────────────
💻 ВСЕГДА АКТУАЛЬНЫЙ И НАДЕЖНЫЙ ТОВАР💮
🚀 МОМЕНТАЛЬНАЯ ДОСТАВКА 24/7💮
💸 ЛУЧШАЯ ЦЕНА НА РЫНКЕ💮
🤝 ПРИЯТНОЕ ОБЩЕНИЕ И УВАЖИТЕЛЬНОЕ ОТНОШЕНИЕ💮
🛠️ ПОМОЩЬ В СЛУЧАЕ ВОЗНИКНОВЕНИЯ ПРОБЛЕМ💮
──────────── ⋆⋅☆⋅⋆ ────────────
❌ПЕРЕПРОДАЖА ТОВАРА ЗАПРЕЩЕНА❌
"""

    # ВИД 2: АКЦЕНТ НА НАДЕЖНОСТИ (Ваш пример 2)
    v2 = f"""
🕒АВТОВЫДАЧА 24/7🕒
🎙️ {gn} - ЛУЧШЕЕ ПРЕДЛОЖЕНИЕ! 🎙️
✨ Чистый материал: Вы становитесь первым владельцем ✨
✅ {t} ВКЛЮЧЕНО
✅ Надёжно, полностью в твоём распоряжении.
✅ Быстрая выдача – сразу после оплаты.
----------------------------------------------------
‼️ Подумайте об этом перед покупкой ‼️
⚠️ Соблюдайте все правила, чтобы все было хорошо
⚠️ Оплачивая товар — вы подтверждаете своё согласие!
🎥 Важно: начинайте запись экрана с момента оплаты. Это нужно в случае спорных ситуаций.
"""

    # ВИД 3: ИНЖЕКТОР СТИЛЬ (Ваш пример 3)
    v3 = f"""
🕒АВТОВЫДАЧА 24/7🕒
💎 Добро пожаловать!
💬 Мы предлагаем только качественные товары по {gn}. Гарантируем надежность каждой покупки.
📦 После покупки вы получаете:
✅ Ссылку на скачивание и детальные инструкции.
✅ Лучший контент в мире по теме: {t}.
📌 Важно знать:
1. Возврат невозможен после получения ссылки.
2. Передача товара третьим лицам запрещена.
3. Работает на всех устройствах.
✨ Оставьте отзыв после покупки — это помогает нам становиться лучше!
"""

    # ВИД 4: АККАУНТ СТИЛЬ (Ваш пример 4)
    v4 = f"""
🕒АВТОВЫДАЧА 24/7🕒
📌 Если объявление активно — товар в наличии!
🌸🎄🩸 Информация о товаре:
◾️ {t} для игры {gn}.
◾️ Материал чистый, проверен на момент публикации.
◾️ Доступно сразу после оплаты.
🌸🎄🩸 Ответственность:
◾️ После получения — проверьте товар сразу.
◾️ Продавец не несёт ответственности за ваши действия после получения ссылки.
⛔️ ВАЖНО: Товар не подлежит возврату.
🌸🎄🩸◾️ СПАСИБО ЗА ПОКУПКУ! ◾️🩸🎄🌸
"""

    # ВИД 5: УСТОЙЧИВОСТЬ К БЛОКАМ (Ваш пример 5)
    v5 = f"""
🕒АВТОВЫДАЧА 24/7🕒
► Данные выдаются автоматически, оплачивайте в любое время!
● Тема: {t}
● Игра: {gn}
● Можно заходить с любого IP!
✔ Всегда в наличии: Возможна покупка крупных партий.
Гарантия на момент получения.
❗Гарантия на замену действует в течение недели после покупки, при соблюдении правил.❗
"""

    # ВИД 6: ОРИГИНАЛ
    re_emojis = random.sample(EMOJI_BANK, 5)
    v6 = f"""
🕒АВТОВЫДАЧА 24/7🕒
{re_emojis[0]} Мгновенный доступ к {gn} {re_emojis[0]}
{re_emojis[1]} Выдача происходит сразу после оплаты {re_emojis[1]}
📌 СОДЕРЖАНИЕ ТОВАРА:
{points}
✅ Проверенная информация
✅ Множество довольных клиентов
✅ Поддержка на связи
🎁 ЗА ПОЛОЖИТЕЛЬНЫЙ ОТЗЫВ — ПОДАРОК! 🎁
"""
    return random.choice([v1, v2, v3, v4, v5, v6]).strip()


def generate_short_description_wemod(game_name):
    c = random.sample(COLOR_BANK, 2)
    return f"{c[0]}{c[1]}{c[0]}【МОД МЕНЮ НА {game_name.upper()}】ЧИТАЙТЕ ОПИСАНИЕ{c[0]}{c[1]}{c[0]}"


def generate_full_description_wemod(game_name):
    """Расширенное описание WeMod — минимум 500+ символов для перевода на английский."""
    template = f"""
🌱 Что вы получаете сразу после оплаты: 🌱
🌱 Мгновенный доступ к моду для {game_name} 🌱
🌱 Выдача происходит АВТОМАТИЧЕСКИ 24/7, даже ночью 🌱
🌱 Полная инструкция по установке и настройке внутри 🌱
🎁 ПОДАРОК ЗА ОТЛИЧНЫЙ ОТЗЫВ (5 ЗВЕЗД) 🎁

💮 Почему же я? 💮
🔋 Всегда с уважением обращаюсь с покупателями 🔋
🔋 Быстрая поддержка и помощь 🔋
🔋 Гарантия работоспособности на момент покупки 🔋
🔋 Регулярные обновления мода под новые патчи 🔋

❗❗❗ Если я не в сети, это не значит, что я вам не отвечу ❗❗❗
❗❗❗ Если у вас есть вопросы, задавайте их смело ❗❗❗

📋 Как использовать:
1. Скачайте файл по ссылке после оплаты
2. Распакуйте архив в удобную папку
3. Запустите мод от имени администратора
4. Выберите {game_name} из списка игр
5. Активируйте нужные функции через горячие клавиши

⚠️⚠️⚠️ У ЭТОГО СОФТА ЕСТЬ ВЕРОЯТНОСТЬ БАНА ⚠️⚠️⚠️
⚠️⚠️⚠️ ЕСЛИ ВАС ЗАБАНИЛИ, НЕ ПИШИТЕ ЧТО ЭТО ИЗ-ЗА МЕНЯ И НЕ ОСТАВЛЯЙТЕ ПЛОХОЙ ОТЗЫВ ⚠️⚠️⚠️
---------⚠️ Я НЕ НЕСУ ОТВЕТСТВЕННОСТИ ЗА ВАШ АККАУНТ ПОСЛЕ ИСПОЛЬЗОВАНИЯ СОФТА ⚠️---------
---------⚠️ ЕСЛИ ВАС ЗАБАНИЛИ, ВОЗВРАТ ТОВАРА НЕ ПРЕДУСМОТРЕН! ⚠️---------

🎮 Используйте на свой страх и риск 🎮
📌 Рекомендуем тестировать на второстепенных аккаунтах 📌
"""
    return template.strip()


def generate_payment_message(link):
    return f"Привет👋 Спасибо за покупку! Твой товар здесь: {link}\n\nОставь отзыв 🌟 и получишь подарок!"
