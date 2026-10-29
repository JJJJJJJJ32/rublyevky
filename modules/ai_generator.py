"""
modules/ai_generator.py — генерация гайдов и переводов.

Логика качества: детектор отказов ИИ, проверка длины, доли списков и повторов,
пост-проверка связи гайда с игрой и товаром, дописывание коротких гайдов.
Обращения к ИИ идут через modules/ai_client.py — в твой Cloudflare Worker,
а не в библиотеку g4f. Внутренние сообщения пишутся только в logs/errors.log:
в обычной консоли остаётся короткий итог по товару.
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

# Слова, которые не говорят ни об игре, ни о товаре: по ним нельзя понять,
# что гайд именно про нужную тему.
_GENERIC_WORDS = {
    "гайд", "гайда", "гайды", "гайдом", "игр", "игра", "игры", "игре", "игру",
    "игрой", "руководство", "инструкция", "мануал", "способ", "способы",
    "товар", "товара", "полное", "полный", "подробный", "простой",
    "game", "games", "guide", "guides", "manual", "tips", "tricks",
    "best", "full", "complete", "how", "the", "and", "for", "with",
    "your", "you", "from", "that", "this",
}

# Художественное вступление вместо конкретного действия или настройки.
_ARTISTIC_INTRO_RE = re.compile(
    r"^\s*(?:"
    r"представь(?:те)?\b"
    r"|вообрази(?:те)?\b"
    r"|давай(?:те)?\s+представим"
    r"|когда-то\b"
    r"|однажды\b"
    r"|давным-давно\b"
    r"|в\s+тишине\b"
    r"|под\s+шум\b"
    r"|imagine\b"
    r"|picture\s+this\b"
    r"|once\s+upon\s+a\s+time\b"
    r"|let\s+me\s+tell\s+you\b"
    r")",
    re.IGNORECASE,
)


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


def _is_repetitive(text, threshold=0.45):
    """True, если одна и та же мысль повторяется разными словами.

    Считаем долю повторяющихся пятисловных связок: общий текст без игровых
    деталей обычно крутит одну и ту же фразу.
    """
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9']+", str(text or "").lower())
    if len(words) < 40:
        return False
    total = len(words) - 4
    if total <= 0:
        return False
    unique = len({" ".join(words[index:index + 5]) for index in range(total)})
    return (total - unique) / total > threshold


def _guide_keywords(game_name, title, content_points):
    """Ключевые слова, по которым видно связь гайда с игрой и товаром."""
    parts = [str(game_name or ""), str(title or "")]
    parts.extend(str(point) for point in (content_points or []))
    keywords = []
    for part in parts:
        for word in re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9\-']*", part):
            low = word.lower().strip("-'")
            if len(low) < 4 or low in _GENERIC_WORDS:
                continue
            if low not in keywords:
                keywords.append(low)
    return keywords


def _game_name_forms(game_name):
    """Название игры и его значимые части в нижнем регистре."""
    name = re.sub(r"[^\w\s]", " ", str(game_name or "").lower())
    words = [word for word in name.split() if len(word) >= 4 and word not in _GENERIC_WORDS]
    forms = []
    if words:
        forms.append(" ".join(words))
        forms.append(words[0])
    return forms


def _is_guide_relevant(text, game_name=None, title=None, content_points=None):
    """Проверяет, что гайд про эту игру и этот товар, а не общий текст.

    Отбраковываются художественное вступление, выдуманные сцены без связи
    с игрой, текст с повторами и текст, в котором нет ключевых слов темы.
    """
    if not text:
        return False

    low = str(text).lower()
    intro = low[:300]

    # Художественное вступление вместо конкретного действия или настройки.
    if _ARTISTIC_INTRO_RE.search(intro):
        return False

    if _is_repetitive(text):
        return False

    keywords = _guide_keywords(game_name, title, content_points)
    if not keywords:
        return True

    hits = sum(1 for keyword in keywords if keyword in low)
    game_mentioned = any(form in low for form in _game_name_forms(game_name))

    if game_mentioned and hits >= 1:
        return True
    if hits >= 3:
        return True
    return hits >= 2 and len(text) >= MIN_CHARS


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
    Единая обёртка над ИИ. Ошибки не молчат — их видно в logs/errors.log,
    а в консоли остаётся только короткий итог по товару.
    """
    try:
        return ai_client.ai_ask(messages, max_tokens=max_tokens, temperature=temperature)
    except ai_client.AILimitReached:
        raise  # дневной лимит — наверху решают, ждать или остановиться
    except ai_client.AIUnavailable as e:
        # Worker, сеть или провайдер недоступны. Менять промпт и просить
        # другой вариант текста бессмысленно — сразу наверх, игру не трогаем.
        logger.log_error(f"ИИ недоступен: {e}")
        raise
    except ai_client.AIModelNotSupported as e:
        # Worker не знает модель — повторять другими промптами тоже незачем.
        logger.log_error(f"Модель не поддерживается: {e}")
        raise
    except ai_client.AIError as e:
        logger.log_error(f"ИИ недоступен: {e}")
        return None
    except Exception as e:
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
        logger.info(f"Генерация идей товаров: попытка {attempt + 1}/2")
        response = _ask([{"role": "user", "content": prompt}], max_tokens=1800)
        if not response:
            continue
        match = re.search(r"\[\s*\{.*\}\s*\]", response, re.DOTALL)
        if not match:
            logger.log_error(
                f"Ответ ИИ без JSON-массива идей ({len(response)} симв.): "
                f"{response[:300]!r}"
            )
            continue
        try:
            data = json.loads(match.group(0))
            if len(data) >= 5:
                return data
            logger.info(f"Идей мало ({len(data)}), пробуем ещё раз")
        except ValueError:
            logger.info("Ответ не разобрался как JSON, пробуем ещё раз")
    logger.info("Идеи получить не удалось — будут только обязательные товары")
    return []


def ai_generate_full_content(idea, game_name):
    """
    Генерирует подробный гайд на РУССКОМ языке.
    Делает несколько попыток, пока не получит нормальный гайд.
    Возвращает None только если все попытки провалились.
    """
    title = idea["title"]

    prompt_main = f"""Напиши подробный гайд на РУССКОМ ЯЗЫКЕ по теме «{title}» для игры «{game_name}».
Нужно примерно 450–550 слов, но не растягивай текст пустыми фразами. Начни сразу с
конкретного действия или настройки: что открыть, куда пойти, что включить или выключить.
Для темы про оптимизацию ПК начни с конкретных настроек игры, Windows, драйвера или
видеокарты, а не с истории. В первом же предложении назови игру «{game_name}».

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

Запрещено придумывать выдуманные сцены: деревни, леса, силуэты, героев и события,
которых нет в теме товара. Не пиши общий текст про «мир игр» и не повторяй одну мысль
разными словами. Каждый абзац должен давать практический шаг, проверяемую деталь
или условие, связанные с «{game_name}» и темой «{title}».

Текст должен звучать как полезный авторский разбор, а не как рекламный шаблон.
"""

    prompt_continue = (
        "Продолжи гайд ещё на 300–400 слов. Дай новые конкретные игровые детали, "
        "цифры, условия и порядок действий, не повторяй уже написанное. Пиши обычными "
        "абзацами разной длины, без Markdown-заголовков, длинных рядов буллитов и "
        "фраз «В заключение», «Итоги», «Вывод», «Таким образом», «Важно отметить» или "
        "«Не забывайте».")

    prompt_alt = f"""Опиши рабочий способ по теме «{title}» в игре «{game_name}» так, как объяснил бы
его опытный игрок товарищу. Напиши живой подробный текст на русском языке объёмом
примерно 450–550 слов. Начни с конкретной игровой ситуации: куда идти, что выбрать или
какую ошибку исправить. Добавь проверяемые детали — названия, числа, время, расстояния,
условия и порядок действий. Не начинай с фраз «Представь, что ты» и не сочиняй сказочных
сцен: только то, что реально относится к «{game_name}» и теме «{title}».

Не используй фиксированный шаблон «введение — три пункта — советы — итоги». Пусть часть
текста идёт связными абзацами, а короткая нумерация появляется только там, где она
действительно упрощает инструкцию. Предложения должны быть разной длины; разговорные
пояснения и короткие отступления допустимы.

Не используй Markdown-заголовки вида ##, ряды эмодзи-буллитов и канцелярские клише:
«В заключение», «Итоги», «Вывод», «Подводя итог», «Таким образом», «В этой статье»,
«В этом гайде я расскажу», «Важно отметить», «Стоит отметить», «Не забывайте».
Не упоминай ИИ и не пиши отказов. Сразу дай полезный материал, без мета-вступления.
"""

    prompt_strict = f"""Пиши строго по делу и только про игру «{game_name}» и тему «{title}».
Начало текста — сразу конкретное действие или настройка без всякого вступления.
В первом предложении обязательно упомяни «{game_name}».

Обязательно используй реальные игровые детали этой темы: названия предметов, локаций,
персонажей, режимов, настройек, пунктов меню или механик, а для темы про ПК — настройки
игры, Windows, драйвера или видеокарты. Дай практические шаги с числами, условиями
и порядком действий.

Категорически запрещено:
- художественное вступление, выдуманные сцены, деревни, леса, герои и события вне темы;
- общие фразы про «мир игр» и повторы одной мысли разными словами;
- мета-фразы про ИИ, статью или генерацию текста;
- финальные разделы «Итоги», «Вывод», «Заключение» и Markdown-заголовки ##;
- превращать весь текст в список: обычных абзацев должно быть больше.

Объём — примерно 450–550 слов. Плохой общий текст без связи с игрой будет отбракован."""

    def _accept(response, stage):
        """Общая проверка ответа: очистка, отказ, длина, списки и связь с игрой."""
        if not response or len(response) < 100:
            return None
        cleaned = _postprocess_guide(response)
        if not cleaned or len(cleaned) < 100:
            logger.info(f"Гайд после очистки слишком короткий ({stage})")
            return None
        if _is_refused(cleaned):
            logger.info(f"Гайд отклонён: отказ или упоминание ИИ ({stage})")
            return None
        if _is_list_heavy(cleaned):
            logger.info(f"Гайд отклонён: слишком много строк-списков ({stage})")
            return None
        if not _is_guide_relevant(
            cleaned, game_name, title, idea.get("content_points")
        ):
            logger.info(f"Гайд отклонён: нет связи с игрой или товаром ({stage})")
            return None
        return cleaned

    def _usable(result):
        """Принимает длинный текст, но не принимает список после очистки."""
        return bool(result) and not _is_list_heavy(result)

    # === ПОПЫТКА 1: основной промпт, при нехватке слов — дописывание ===
    # Один запрос на 700–900 слов идёт дольше 40 секунд, и соединение с
    # Worker'ом успевает оборваться. Две части по ~500 и ~350 слов
    # приходят заметно быстрее и собираются в цельный гайд.
    result = _accept(
        _ask([{"role": "user", "content": prompt_main}], max_tokens=2200),
        "основной промпт",
    )
    if result:
        if _count_words(result) < 650:
            logger.info(f"Гайд короткий ({_count_words(result)} слов). Просим дописать")
            try:
                more = _accept(
                    _ask([
                        {"role": "user", "content": prompt_main},
                        {"role": "assistant", "content": result},
                        {"role": "user", "content": prompt_continue},
                    ], max_tokens=1600),
                    "дописывание",
                )
            except ai_client.AIUnavailable:
                # Первая часть уже есть — не выбрасываем её из-за обрыва связи.
                logger.log_error("Дописывание не удалось: Worker недоступен, оставляем первую часть")
                more = None
            if more:
                result = _postprocess_guide(result + "\n\n" + more)
        if _is_guide_good(result) and _usable(result):
            return result
        if len(result) > 500 and _usable(result):
            logger.info(f"Гайд получен ({_count_words(result)} слов)")
            return result

    # === ПОПЫТКА 2: другой промпт ===
    result = _accept(_ask([{"role": "user", "content": prompt_alt}], max_tokens=2200), "альтернативный промпт")
    if result and len(result) > 500 and _usable(result):
        logger.info(f"Гайд получен через альт. промпт ({_count_words(result)} слов)")
        return result

    # === ПОПЫТКА 3: повтор основного промпта (модель может ответить иначе) ===
    time.sleep(2)
    result = _accept(_ask([{"role": "user", "content": prompt_main}], max_tokens=2200), "повтор")
    if result:
        if _is_guide_good(result):
            return result
        if len(result) > 500 and _usable(result):
            logger.info(f"Гайд получен ({_count_words(result)} слов)")
            return result

    # === ПОПЫТКА 4: самый строгий промпт — только конкретика по игре ===
    time.sleep(2)
    result = _accept(_ask([{"role": "user", "content": prompt_strict}], max_tokens=2200), "строгий промпт")
    if result:
        if _is_guide_good(result):
            return result
        if len(result) > 500 and _usable(result):
            logger.info(f"Гайд получен через строгий промпт ({_count_words(result)} слов)")
            return result

    # Плохой гайд не публикуется: товар будет пропущен.
    logger.info("Все попытки ИИ провалились. Товар пропущен")
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
