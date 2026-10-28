"""
modules/ai_client.py — единый клиент ИИ через Cloudflare Worker.

Рабочий сценарий: только Gemini (по умолчанию gemini-3.6-flash) через HTTP
твоего Cloudflare Worker. Gemini SDK в боте не используется, ключ лежит
в секретах Worker'а, запасного пути через g4f нет — модель не подменяется.

Схема:

    бот ──► твой Worker ──► Gemini (ключ GEMINI_API_KEY в секретах Worker'а)

Что это даёт:
  - ключи ИИ не хранятся на ПК;
  - смена модели — одна строка в .env (AI_MODEL_PRIORITY);
  - бот не ломается, когда очередной внешний сервис меняет защиту.

Если Worker ещё не настроен (нет AI_WORKER_URL в .env), бот сообщает об этом
и просит сначала настроить Worker из README.md. Рабочий путь проекта — через
личный Worker.
"""

import json
import os
import re
import threading
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

from modules.logger import logger

load_dotenv()

# ─── Настройки ─────────────────────────────────────────────────────────
# Рабочий сценарий использует только Gemini. Модель вызывается через твой
# Cloudflare Worker по HTTP: Gemini SDK в боте нет и не нужен, ключ лежит
# в секретах Worker'а. Подменять Gemini другой моделью нельзя.
DEFAULT_MODELS = [
    "gemini-3.6-flash",
]

REQUEST_TIMEOUT = int(os.getenv("AI_TIMEOUT", "180"))   # секунды на один запрос
ATTEMPTS_PER_MODEL = int(os.getenv("AI_ATTEMPTS", "2"))
USAGE_LOG = os.path.join("logs", "ai_usage.jsonl")

_lock = threading.Lock()


class AIError(Exception):
    """Базовая ошибка ИИ."""


class AILimitReached(AIError):
    """Дневной лимит бесплатных нейронов исчерпан (или лимит провайдера)."""


class AIUnavailable(AIError):
    """Worker недоступен: нет URL, токен не принят, сеть, 5xx."""


class AIModelNotSupported(AIError):
    """Worker не знает такую модель: устарел Worker или опечатка в AI_MODEL_PRIORITY."""

    def __init__(self, message, available=None):
        super().__init__(message)
        self.available = [str(model) for model in (available or [])]


def get_worker_url():
    return (os.getenv("AI_WORKER_URL") or "").strip().rstrip("/")


def get_worker_token():
    return (os.getenv("AI_WORKER_TOKEN") or "").strip()


def get_ai_proxy():
    """
    Прокси для запросов к Worker'у.

    AI_PROXY_URL важнее PROXY_URL: общий PROXY_URL обычно подбирают под FunPay,
    и не каждый такой прокси годится для Cloudflare. Пусто — идём напрямую.
    """
    return (os.getenv("AI_PROXY_URL") or os.getenv("PROXY_URL") or "").strip()


def _mask_proxy(proxy):
    """Прячет логин и пароль прокси для вывода в консоль и лог."""
    if "@" not in proxy:
        return proxy
    head, tail = proxy.rsplit("@", 1)
    if "://" in head:
        scheme, _ = head.split("://", 1)
        return f"{scheme}://***@{tail}"
    return f"***@{tail}"


def get_model_priority():
    """
    Порядок моделей из .env (AI_MODEL_PRIORITY через запятую) или дефолтный.
    Пример: AI_MODEL_PRIORITY=gemini-3.6-flash
    """
    raw = (os.getenv("AI_MODEL_PRIORITY") or "").strip()
    if not raw:
        return list(DEFAULT_MODELS)
    models = [m.strip() for m in raw.split(",") if m.strip()]
    return models or list(DEFAULT_MODELS)


class AIClient:
    """
    Клиент к Worker'у. Совместим с форматом OpenAI:
    POST {base}/v1/chat/completions  {"model": ..., "messages": [...]}
    """

    def __init__(self, base_url=None, token=None, session=None, timeout=None, models=None):
        self.base_url = (base_url if base_url is not None else get_worker_url()).rstrip("/")
        self.token = token if token is not None else get_worker_token()
        self.timeout = timeout or REQUEST_TIMEOUT
        self.models = list(models or get_model_priority())
        self._session = session
        self._session_lock = threading.Lock()

    # ─── Служебное ─────────────────────────────────────────────────────

    @property
    def configured(self):
        """Настроен ли Worker (есть URL и токен)."""
        return bool(self.base_url)

    @property
    def session(self):
        if self._session is None:
            with self._session_lock:
                if self._session is None:
                    session = requests.Session()
                    proxy = get_ai_proxy()
                    if proxy:
                        session.proxies.update({"http": proxy, "https": proxy})
                        logger.info(f"[AI] Запросы к Worker'у идут через прокси {_mask_proxy(proxy)}")
                    self._session = session
        return self._session

    def _url(self, path):
        return f"{self.base_url}{path}"

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    # ─── Проверка связи ────────────────────────────────────────────────

    def health(self):
        """
        Проверяет Worker. Возвращает (успех: bool, инфо: dict или текст ошибки).
        """
        if not self.configured:
            return False, "AI_WORKER_URL не задан в .env"
        try:
            resp = self.session.get(self._url("/health"), headers=self._headers(), timeout=30)
        except requests.exceptions.RequestException as e:
            return False, _short_network_error(e)

        if resp.status_code == 401:
            return False, "Worker отклонил токен (проверь AI_WORKER_TOKEN)"
        if resp.status_code != 200:
            return False, f"Worker ответил статусом {resp.status_code}"

        try:
            data = resp.json()
        except ValueError:
            return False, "Worker вернул не JSON (возможно, это не Worker или страница-заглушка провайдера)"

        return True, data

    def supported_models(self):
        """Список моделей, которые знает Worker (пусто, если Worker не ответил).

        Актуальный Worker отдаёт /v1/models, старый — только /health с полем
        models. Если Worker не отвечает или не сообщает список, возвращаем []:
        это не ошибка, просто проверить нечем.
        """
        if not self.configured:
            return []
        try:
            resp = self.session.get(self._url("/v1/models"), headers=self._headers(), timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                models = [
                    str(item.get("id"))
                    for item in (data.get("data") or [])
                    if isinstance(item, dict) and item.get("id")
                ]
                if models:
                    return models
        except Exception:
            pass  # Worker старый или недоступен — проверяем через /health

        ok, info = self.health()
        if ok and isinstance(info, dict):
            return [str(model) for model in (info.get("models") or []) if model]
        return []

    # ─── Основной метод ────────────────────────────────────────────────

    def chat(self, messages, model=None, max_tokens=None, temperature=None, attempts=None):
        """
        Один запрос к указанной модели (или к первой доступной из списка).
        Возвращает текст ответа. Бросает AIError-наследников при проблемах.
        """
        if not self.configured:
            raise AIUnavailable("AI_WORKER_URL не задан — Worker не настроен")

        models = [model] if model else list(self.models)
        attempts = attempts or ATTEMPTS_PER_MODEL
        last_error = None

        for model_name in models:
            for attempt in range(1, attempts + 1):
                payload = {
                    "model": model_name,
                    "messages": messages,
                    "stream": False,
                }
                if max_tokens:
                    payload["max_tokens"] = max_tokens
                if temperature is not None:
                    payload["temperature"] = temperature

                try:
                    started = time.time()
                    resp = self.session.post(
                        self._url("/v1/chat/completions"),
                        headers=self._headers(),
                        json=payload,
                        timeout=self.timeout,
                    )
                    elapsed = round(time.time() - started, 1)

                    if resp.status_code == 429:
                        raise AILimitReached(_extract_error(resp) or "лимит ИИ исчерпан")
                    if resp.status_code == 401:
                        raise AIUnavailable("Worker отклонил токен (AI_WORKER_TOKEN)")
                    if resp.status_code >= 500:
                        raise AIUnavailable(f"Worker вернул {resp.status_code}: {_extract_error(resp)}")
                    if resp.status_code != 200:
                        last_error = _model_error(model_name, resp.status_code, _extract_error(resp))
                        break  # смысла повторять ту же модель нет, идём к следующей

                    data = resp.json()
                    text = _extract_text(data)
                    if not text:
                        last_error = AIError(f"{model_name}: пустой ответ")
                        continue

                    _log_usage(model_name, data.get("usage"), elapsed, len(text))
                    # Время, модель и объём ответа — только в logs/ai_usage.jsonl
                    # и logs/errors.log. В обычной консоли этого нет.
                    logger.info(
                        f"[AI] {model_name} ответила за {elapsed} с ({len(text)} симв.)"
                    )
                    return text

                except AILimitReached:
                    raise
                except requests.exceptions.ConnectionError as e:
                    # Адрес один для всех моделей: если он недоступен,
                    # перебирать модели бессмысленно — сразу понятная ошибка.
                    logger.info(f"[AI] Worker недоступен: {_short_network_error(e)}")
                    raise AIUnavailable(_short_network_error(e))
                except requests.exceptions.Timeout as e:
                    # Таймаут — та же недоступность Worker'а. Ждать ещё 180
                    # секунд на повтор бессмысленно: сразу наверх, игру не трогаем.
                    logger.info(f"[AI] Worker не ответил вовремя: {_short_network_error(e)}")
                    raise AIUnavailable(_short_network_error(e))
                except requests.exceptions.RequestException as e:
                    last_error = AIUnavailable(_short_network_error(e))
                    logger.info(f"[AI] {last_error}")
                time.sleep(2 * attempt)

        raise last_error or AIError("Ни одна модель не ответила")

    def chat_chain(self, messages, models=None, max_tokens=None, temperature=None):
        """
        Пробует модели по очереди, пока какая-нибудь не ответит.
        Возвращает (текст, имя модели). Удобно для генерации гайдов.
        """
        chain = list(models or self.models)
        last_error = None
        for index, model_name in enumerate(chain):
            try:
                text = self.chat(messages, model=model_name, max_tokens=max_tokens, temperature=temperature)
                return text, model_name
            except AILimitReached as e:
                # Лимит одной модели не означает, что закончились все
                # провайдеры. Например, после Cloudflare пробуем Gemini.
                last_error = e
                if index + 1 < len(chain):
                    logger.info(
                        f"[AI] {model_name}: лимит исчерпан. "
                        f"Пробуем следующую модель {chain[index + 1]}"
                    )
                    continue
                # Цепочка из одного элемента: подменять модель нельзя,
                # поэтому отдаём понятную ошибку наверх.
                raise AILimitReached(
                    f"модель {model_name} ответила лимитом (429): {e}. "
                    "Запасных моделей в цепочке нет — дождитесь сброса лимита "
                    "или проверьте квоту провайдера."
                ) from e
            except AIUnavailable:
                # Адрес Worker'а один для всех моделей: если он совсем
                # недоступен, повторять тот же запрос к нему бессмысленно.
                raise
            except AIError as e:
                last_error = e
                logger.info(f"[AI] {model_name} не справилась: {e}")
        raise last_error or AIError("Все модели перебраны, ответа нет")


# ─── Вспомогательные ───────────────────────────────────────────────────

def _available_from_error(error_text):
    """Достаёт список моделей из ответа Worker'а «Доступные: a, b, c»."""
    match = re.search(r"доступные\s*:\s*(.+)$", str(error_text or ""), re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    return [item.strip() for item in match.group(1).split(",") if item.strip()]


def _model_error(model_name, status, error_text):
    """Понятная ошибка, если Worker не знает запрошенную модель."""
    if status == 400 and "не поддерживает" in str(error_text or "").lower():
        available = _available_from_error(error_text)
        return AIModelNotSupported(f"{model_name}: {error_text}", available)
    return AIError(f"{model_name}: статус {status} — {error_text}")


def _short_network_error(exc):
    """Короткое объяснение сетевой проблемы вместо многоэтажного текста requests."""
    text = str(exc)
    low = text.lower()
    if "connection refused" in low or "errno 111" in low:
        return "Worker не отвечает: соединение отклонено (проверь AI_WORKER_URL)"
    if "name or service not known" in low or "getaddrinfo" in low or "nodename nor servname" in low:
        return "не могу найти адрес Worker'а — проверь AI_WORKER_URL (возможно, опечатка)"
    if "timed out" in low or "timeout" in low:
        return "Worker не отвечает (таймаут) — проверь интернет или адрес"
    if "certificate" in low or "ssl" in low:
        return "проблема с защищённым соединением к Worker'у (SSL)"
    return f"сетевая ошибка: {text[:150]}"


def _extract_text(data):
    """Достаёт текст из ответа в формате OpenAI."""
    try:
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, list):  # некоторые провайдеры отдают список блоков
                return "".join(part.get("text", "") for part in content if isinstance(part, dict))
            if content:
                return str(content)
            return ""
        # Запасной формат (нативный ответ Workers AI)
        if isinstance(data.get("response"), str):
            return data["response"]
    except Exception:
        pass
    return ""


def _extract_error(resp):
    """Достаёт человекочитаемую ошибку из ответа Worker'а."""
    try:
        data = resp.json()
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("message") or json.dumps(err, ensure_ascii=False)
        if isinstance(err, str):
            return err
        if data.get("message"):
            return data["message"]
    except Exception:
        pass
    text = (resp.text or "").strip()
    return text[:300] if text else ""


def _log_usage(model, usage, elapsed, chars):
    """Пишет расход в logs/ai_usage.jsonl — видно, сколько съедает бот."""
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "seconds": elapsed,
        "chars": chars,
        "usage": usage or {},
    }
    try:
        os.makedirs("logs", exist_ok=True)
        with _lock:
            with open(USAGE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # логи не должны ломать работу


def used_today():
    """Сколько запросов к ИИ ушло за сегодня (по локальному логу)."""
    today = datetime.now().strftime("%Y-%m-%d")
    count, tokens = 0, 0
    if not os.path.exists(USAGE_LOG):
        return 0, 0
    try:
        with open(USAGE_LOG, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if str(rec.get("ts", "")).startswith(today):
                    count += 1
                    usage = rec.get("usage") or {}
                    tokens += int(usage.get("total_tokens") or usage.get("output_tokens") or 0)
    except OSError:
        pass
    return count, tokens


def check_model_support(models=None):
    """Проверяет, что Worker знает все модели из цепочки.

    Возвращает (неподдерживаемые, доступные). Пустой список доступных моделей
    значит, что Worker не сообщил свой список (старая версия) — тогда
    предупреждать нечем и проверка считается пройденной.
    """
    chain = list(models or get_model_priority())
    available = client.supported_models()
    if not available:
        return [], []

    def _known(model):
        name = str(model)
        return name in available or name.startswith("openrouter/")

    return [model for model in chain if not _known(model)], available


# ─── Общая точка входа ─────────────────────────────────────────────────

client = AIClient()


def ai_ask(messages, models=None, max_tokens=None, temperature=None):
    """
    Главная функция для остального кода.
    Все запросы идут в свой Cloudflare Worker по HTTP. Других путей нет:
    ни Gemini SDK, ни g4f в боте не используются.
    Возвращает текст ответа.
    """
    if not client.configured:
        raise AIUnavailable(
            "ИИ не настроен: в .env нет AI_WORKER_URL. "
            "Поднимите Cloudflare Worker (см. README.md, раздел 4) "
            "и впишите его адрес в .env."
        )
    text, _ = client.chat_chain(messages, models=models, max_tokens=max_tokens, temperature=temperature)
    return text
