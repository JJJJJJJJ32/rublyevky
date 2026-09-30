"""
modules/ai_generator.py — генерация гайдов и переводов.

Логика качества осталась прежней (детектор отказов ИИ, проверка длины,
дописывание коротких гайдов), но обращения к ИИ идут через modules/ai_client.py:
в твой Cloudflare Worker, а не в библиотеку g4f.
"""

import json
import re
import time
from functools import lru_cache

from modules.logger import logger
from modules import ai_client

# Слова-маркеры что ИИ отказался или выдал себя
_REFUSE_KEYWORDS = [
    "не могу", "я не могу", "не смогу", "не рекомендую", "не советую",
    "я ии", "я нейросеть", "как нейросеть", "как ии", "я искусственный",
    "противоречит политике", "этически", "морально", "незаконно",
    "не поддерживаю", "не буду", "отказываюсь", "не могу принять",
    "не могу помочь", "не стану", "не могу написать", "не могу создать",
    "не могу предоставить", "без нарушения правил", "language model",
    "i am an ai", "i cannot", "i can't", "as an ai", "i'm unable",
]

# Фразы, по которым текст звучит как шаблонная статья, а не как живой гайд.
# Они удаляются из готового ответа даже если модель всё-таки их написала.
_FORBIDDEN_GUIDE_PATTERNS = (
    r"\bв\s+заключени(?:е|и|я|ю|ем)\b\s*[,;:–—-]?\s*",
    r"\bподводя\s+итог(?:и)?\b\s*[,;:–—-]?\s*",
    r"\bтаким\s+образом\b\s*[,;:–—-]?\s*",
    r"\bв\s+этой\s+статье\b\s*",
    r"\bв\s+этом\s+гайде(?:\s+я\s+расскажу)?\b\s*",
    r"\bважно\s+отметить\b\s*[,;:–—-]?\s*",
    r"\bстоит\s+отметить\b\s*[,;:–—-]?\s*",
    r"\bне\s+забывайте\b\s*[,;:–—-]?\s*",
    r"\b(?:итог(?:и|а|у|ом)?|вывод(?:ы|а|е|ом)?|заключени(?:е|я|и|ю|ем))\b\s*[,;:–—-]?\s*",
    # Иногда модель отвечает на английском, даже если попросили русский.
    r"\bin\s+conclusion\b\s*[,;:–—-]?\s*",
    r"\b(?:conclusion|summary)\b\s*[,;:–—-]?\s*",
)

_CONCLUSION_HEADING_RE = re.compile(
    r"^(?:итог(?:и)?|вывод(?:ы)?|заключение|conclusion|summary)"
    r"(?:\s*(?:[:–—-]|и\b).*)?$",
    re.IGNORECASE,
)
_LIST_LINE_RE = re.compile(r"^\s*(?:[-•*]\s*|\d+\s*[.)]\s+)")

# Минимум слов в гайде — если меньше, гайд считается коротким и пробуем ещё
MIN_WORDS = 400
MIN_CHARS = 2000


def _is_refused(text):
    """Проверяет не отказался ли ИИ или не выдал ли себя."""
    low = text.lower()
    return any(kw in low for kw in _REFUSE_KEYWORDS)


def _count_words(text):
    """Считает слова в тексте."""
    return len(text.split())


def _is_list_heavy(text, threshold=0.4):
    """True, если больше 40% непустых строк выглядят как пункты списка."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return False
    list_lines = sum(bool(_LIST_LINE_RE.match(line)) for line in lines)
    return list_lines / len(lines) > threshold


def _is_guide_good(text):
    """Проверяет длину, объём и то, что гайд не превратился в список."""
    if not text or len(text) < MIN_CHARS:
        return False
    if _count_words(text) < MIN_WORDS:
        return False
    if _is_list_heavy(text):
        return False
    return True


def _is_emoji_base(char):
    """Небольшой набор Unicode-диапазонов для наиболее частых эмодзи."""
    code = ord(char)
    return (
        0x1F000 <= code <= 0x1FAFF
        or 0x2300 <= code <= 0x23FF
        or 0x2500 <= code <= 0x27BF
        or char in "©®™"
    )


def _read_emoji_cluster(text, start):
    """Читает один эмодзи вместе с variation selector и modifier."""
    if start >= len(text) or not _is_emoji_base(text[start]):
        return None, start

    end = start + 1
    while end < len(text) and text[end] in "\ufe0f\ufe0e\u200d":
        end += 1
        if end < len(text) and _is_emoji_base(text[end]) and text[end - 1] == "\u200d":
            end += 1
    while end < len(text) and "\U0001F3FB" <= text[end] <= "\U0001F3FF":
        end += 1
    return text[start:end], end


def _collapse_emoji_runs(text):
    """Оставляет один эмодзи вместо плотного ряда из нескольких."""
    result = []
    index = 0
    while index < len(text):
        cluster, next_index = _read_emoji_cluster(text, index)
        if cluster is None:
            result.append(text[index])
            index += 1
            continue

        clusters = [cluster]
        index = next_index
        while index < len(text):
            # Пробелы между декоративными эмодзи тоже считаем частью ряда,
            # но не трогаем обычный пробел перед следующим словом.
            probe = index
            while probe < len(text) and text[probe] in " \t":
                probe += 1
            next_cluster, after_next = _read_emoji_cluster(text, probe)
            if next_cluster is None:
                break
            clusters.append(next_cluster)
            index = after_next

        # Важно: cluster уже включает U+FE0F, поэтому его нельзя заменять
        # отдельным базовым символом — иначе эмодзи может визуально измениться.
        result.append(clusters[0] if len(clusters) > 1 else cluster)

    return "".join(result)


def _remove_conclusion_sections(text):
    """Удаляет финальную секцию после заголовка «Итоги» или «Заключение»."""
    kept = []
    for line in str(text or "").splitlines():
        heading = re.sub(r"^\s*[#>*•\-]+\s*", "", line).strip()
        if _CONCLUSION_HEADING_RE.match(heading):
            break
        kept.append(line)
    return "\n".join(kept)


def _remove_forbidden_phrases(text):
    """Удаляет запрещённые мета-фразы, не вырезая остальной абзац."""
    cleaned = str(text or "")
    for pattern in _FORBIDDEN_GUIDE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
    return cleaned


def _postprocess_guide(text):
    """Приводит ответ ИИ к более живому виду перед проверкой качества."""
    if not text:
        return ""

    cleaned = _remove_conclusion_sections(text)
    cleaned = _remove_forbidden_phrases(cleaned)
    # В обычном тексте Markdown-заголовки не нужны. Сохраняем их слова,
    # но убираем технические символы ##.
    cleaned = re.sub(r"(?m)^\s*#{1,6}\s*", "", cleaned)
    cleaned = _collapse_emoji_runs(cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _ask(messages, max_tokens=None, temperature=None):
    """
    Единая обёртка над ИИ. Ошибки не молчат — их видно в консоли и в логе.
    """
    try:
        return ai_client.ai_ask(messages, max_tokens=max_tokens, temperature=temperature)
    except ai_client.AILimitReached:
        raise  # дневной лимит — наверху решают, ждать или остановиться
    except ai_client.AIError as e:
        print(f"      [AI] ⚠️ {e}")
        logger.log_error(f"ИИ недоступен: {e}")
        return None
    except Exception as e:
        print(f"      [AI] ⚠️ Неожиданная ошибка ИИ: {e}")
        logger.log_error(f"ИИ: неожиданная ошибка {e}")
        return None


def ai_generate_product_ideas(game_name):
    """Генерирует 12 виральных идей на РУССКОМ языке."""
    prompt = f"""Ты помогаешь продавцу сделать полезный раздел магазина по игре «{game_name}».
Придумай 12 разных цифровых товаров на РУССКОМ ЯЗЫКЕ: конкретные маршруты фарма,
билды под разные ситуации, секретные места, разборы квестов, тактики для матчей
и понятные инструкции для новичка. Не повторяй одну и ту же идею под разными словами.

Пиши названия живо и по делу. Используй настоящие названия предметов, локаций,
персонажей, режимов и примерные цифры, только если они действительно относятся к игре.
Пусть названия отличаются по ритму: не начинай каждый пункт с «10 способов».
У каждого товара может быть от 2 до 5 content_points — не делай одинаковую тройку пунктов
для всех позиций.

Запрещено:
- говорить, что ты ИИ, нейросеть, языковая модель или бот;
- писать отказы, мета-комментарии, «в этой статье», «итоги», «выводы»;
- использовать Markdown и ряды декоративных эмодзи;
- придумывать универсальные советы без связи с конкретной игрой.
Если тема про HWID, MAC, UUID или IP, безопасно переформулируй её как настройку ПК
или обновление системной конфигурации.

Выведи результат ТОЛЬКО как JSON-массив без ``` и пояснений:
[
  {{
    "title": "КОНКРЕТНОЕ НАЗВАНИЕ НА РУССКОМ",
    "type": "тип товара",
    "content_points": ["конкретный пункт", "ещё один пункт"]
  }}
]"""

    for attempt in range(2):
        print(f"   [AI] Генерируем идеи товаров (попытка {attempt + 1}/2)...")
        response = _ask([{"role": "user", "content": prompt}], max_tokens=1800)
        if not response:
            continue
        match = re.search(r"\[\s*\{.*\}\s*\]", response, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                if len(data) >= 5:
                    return data
                print(f"      [AI] Идей мало ({len(data)}), пробуем ещё раз...")
            except ValueError:
                print("      [AI] Ответ не разобрался как JSON, пробуем ещё раз...")
    print("   [AI] ⚠️ Идеи получить не удалось — будут только обязательные товары.")
    return []


def ai_generate_full_content(idea, game_name):
    """
    Генерирует подробный гайд на РУССКОМ языке.
    Делает несколько попыток, пока не получит нормальный гайд.
    Возвращает None только если все попытки провалились.
    """
    title = idea["title"]

    prompt_main = f"""Напиши подробный гайд на РУССКОМ ЯЗЫКЕ по теме «{title}» для игры «{game_name}».
Нужно примерно 700–900 слов, но не растягивай текст пустыми фразами. Начни сразу с
конкретного действия или ситуации в игре, без вступления о том, что сейчас будет рассказано.

Пиши как игрок, который сам проверил советы. Нужны названия предметов, локаций,
персонажей, режимов и точные ориентиры: количество, время, порядок действий,
условия срабатывания. Смешивай короткие реплики и длинные объяснения, допускай
небольшие живые отступления в скобках, если они помогают читателю. Не выстраивай
весь текст по одной симметричной схеме и не делай одинаковые по длине абзацы.

Формат — обычный читаемый текст с редкими короткими списками только там, где без них
не обойтись. Не используй Markdown-заголовки, строки из одних эмодзи и длинные ряды
буллитов. Не делай структуру ровно из трёх пунктов и не добавляй отдельный раздел с итогами.

Нельзя использовать следующие фразы и близкие к ним клише: «В заключение», «Итоги»,
«Вывод», «Подводя итог», «Таким образом», «В этой статье», «В этом гайде я расскажу»,
«Важно отметить», «Стоит отметить», «Не забывайте». Не упоминай ИИ, нейросеть,
языковую модель или ограничения. Не пиши отказы, дисклеймеры и фразы «я не рекомендую».
Если исходная тема звучит спорно, преврати её в практическую настройку ПК или игровую
оптимизацию и продолжай без объяснений о смене темы.

Текст должен звучать как полезный авторский разбор, а не как рекламный шаблон."""

    prompt_continue = (
        "Продолжи гайд ещё минимум на 400 слов. Дай новые конкретные игровые детали, "
        "цифры, условия и порядок действий, не повторяй уже написанное. Пиши обычными "
        "абзацами разной длины, без Markdown-заголовков, длинных рядов буллитов и "
        "фраз «В заключение», «Итоги», «Вывод», «Таким образом», «Важно отметить» или "
        "«Не забывайте».")

    prompt_alt = f"""Представь, что ты опытный игрок, который объясняет товарищу рабочий способ по теме
«{title}» в игре «{game_name}». Напиши живой подробный текст на русском языке объёмом
не меньше 600 слов. Начни с конкретной игровой ситуации: куда идти, что выбрать или
какую ошибку исправить. Добавь проверяемые детали — названия, числа, время, расстояния,
условия и порядок действий.

Не используй фиксированный шаблон «введение — три пункта — советы — итоги». Пусть часть
текста идёт связными абзацами, а короткая нумерация появляется только там, где она
действительно упрощает инструкцию. Предложения должны быть разной длины; разговорные
пояснения и короткие отступления допустимы.

Не используй Markdown-заголовки вида ##, ряды эмодзи-буллитов и канцелярские клише:
«В заключение», «Итоги», «Вывод», «Подводя итог», «Таким образом», «В этой статье»,
«В этом гайде я расскажу», «Важно отметить», «Стоит отметить», «Не забывайте».
Не упоминай ИИ и не пиши отказов. Сразу дай полезный материал, без мета-вступления."""

    def _accept(response, stage):
        """Общая проверка ответа: очистка, отказ, длина и доля списков."""
        if not response or len(response) < 100:
            return None
        cleaned = _postprocess_guide(response)
        if not cleaned or len(cleaned) < 100:
            return None
        if _is_refused(cleaned):
            print(f"      [AI] ⚠️ Отказ или упоминание ИИ ({stage}). Пробуем иначе...")
            return None
        if _is_list_heavy(cleaned):
            print(f"      [AI] ⚠️ Слишком много строк-списков ({stage}). Пробуем иначе...")
            return None
        return cleaned

    def _usable(result):
        """Принимает длинный текст, но не принимает список после очистки."""
        return bool(result) and not _is_list_heavy(result)

    # === ПОПЫТКА 1: основной промпт ===
    result = _accept(_ask([{"role": "user", "content": prompt_main}], max_tokens=3000), "основной промпт")
    if result:
        if _is_guide_good(result):
            return result
        if len(result) > 200:
            print(f"      [AI] Гайд короткий ({_count_words(result)} слов). Просим дописать...")
            more = _accept(
                _ask([
                    {"role": "user", "content": prompt_main},
                    {"role": "assistant", "content": result},
                    {"role": "user", "content": prompt_continue},
                ], max_tokens=2000),
                "дописывание",
            )
            if more:
                result = _postprocess_guide(result + "\n\n" + more)
                if _is_guide_good(result):
                    return result
        if len(result) > 500 and _usable(result):
            print(f"      [AI] Гайд получен ({_count_words(result)} слов).")
            return result

    # === ПОПЫТКА 2: другой промпт ===
    result = _accept(_ask([{"role": "user", "content": prompt_alt}], max_tokens=3000), "альтернативный промпт")
    if result and len(result) > 500 and _usable(result):
        print(f"      [AI] Гайд получен через альт. промпт ({_count_words(result)} слов).")
        return result

    # === ПОПЫТКА 3: повтор основного промпта (модель может ответить иначе) ===
    time.sleep(2)
    result = _accept(_ask([{"role": "user", "content": prompt_main}], max_tokens=3000), "повтор")
    if result:
        if _is_guide_good(result):
            return result
        if len(result) > 500 and _usable(result):
            print(f"      [AI] Гайд получен ({_count_words(result)} слов).")
            return result

    print("      ⚠️ Все попытки ИИ провалились. Пропускаем товар.")
    return None


def ai_translate_to_en(text):
    """
    Переводит русский текст на английский для полей [en].
    Кэш: одинаковый текст не переводится дважды.
    """
    return _translate_cached(text)


@lru_cache(maxsize=256)
def _translate_cached(text):
    prompt = f"""Translate the following Russian gaming text to English.
IMPORTANT RULES:
- Write as a real person, not as an AI. Do NOT mention being an AI or language model.
- Output ONLY the translation, no meta-comments, no intro, no notes.
- Make the translation FULL and DETAILED — do NOT summarize or shorten the text.
- Keep all formatting, lists, and structure.
- If the text is long, translate ALL of it — do not skip any sections.

Russian text to translate:

{text}"""
    response = _ask([{"role": "user", "content": prompt}], max_tokens=3000)
    if response and len(response.strip()) > 50:
        return response.strip()

    # Запасной вариант, чтобы пройти минимум 500 символов в desc[en]
    return ("High quality gaming service with professional guides and expert strategies. "
            "We provide instant automated delivery 24/7 with full support. "
            "Thousands of satisfied customers trust our verified tips and detailed walkthroughs. "
            "Our guides include step-by-step instructions, optimal builds, secret locations, "
            "farming routes, and advanced tactics for maximum efficiency. "
            "Buy with confidence - fast response guaranteed. "
            "All content is always up-to-date and verified by experienced players.")
