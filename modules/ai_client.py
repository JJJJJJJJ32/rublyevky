"""
modules/ai_client.py — единый клиент ИИ через Cloudflare Worker.

Зачем: раньше генерация висела на библиотеке g4f (бесплатная обёртка над чужими
сайтами). Она разваливается каждую неделю, потому что сайты меняют защиту.
Теперь все запросы идут в СВОЙ Cloudflare Worker:

    бот ──► твой Worker ──► 1) модели самого Cloudflare (Workers AI, без ключей)
                         └─► 2) Gemini / Groq / OpenRouter (ключ лежит в Worker'е)

Что это даёт:
  - ключи ИИ не хранятся на ПК;
  - смена модели — одна строка в настройках Worker'а, бот не перезапускается;
  - бот не ломается, когда очередной внешний сервис меняет защиту.

Если Worker ещё не настроен (нет AI_WORKER_URL в .env) — клиент честно падает
на g4f (если библиотека установлена), чтобы бот не простаивал.
"""

import json
import os
import threading
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Настройки ─────────────────────────────────────────────────────────
# Порядок моделей: пробуем по очереди, пока какая-нибудь не ответит.
DEFAULT_MODELS = [
    "@cf/openai/gpt-oss-120b",        # умная, ~4-5 игр в сутки на бесплатном лимите
    "@cf/qwen/qwen3-30b-a3b-fp8",     # дешевле по лимиту, ~9 игр в сутки
    "@cf/openai/gpt-oss-20b",         # запасная
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


def get_worker_url():
    return (os.getenv("AI_WORKER_URL") or "").strip().rstrip("/")


def get_worker_token():
    return (os.getenv("AI_WORKER_TOKEN") or "").strip()


def get_model_priority():
    """
    Порядок моделей из .env (AI_MODEL_PRIORITY через запятую) или дефолтный.
    Пример: AI_MODEL_PRIORITY=@cf/openai/gpt-oss-120b,gemini-2.5-flash
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
                    self._session = requests.Session()
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
                        last_error = AIError(f"{model_name}: статус {resp.status_code} — {_extract_error(resp)}")
                        break  # смысла повторять ту же модель нет, идём к следующей

                    data = resp.json()
                    text = _extract_text(data)
                    if not text:
                        last_error = AIError(f"{model_name}: пустой ответ")
                        continue

                    _log_usage(model_name, data.get("usage"), elapsed, len(text))
                    print(f"      [AI] ✅ {model_name} ответила за {elapsed} с ({len(text)} симв.)")
                    return text

                except AILimitReached:
                    raise
                except requests.exceptions.ConnectionError as e:
                    # Адрес один для всех моделей: если он недоступен,
                    # перебирать модели бессмысленно — сразу понятная ошибка.
                    raise AIUnavailable(_short_network_error(e))
                except requests.exceptions.Timeout:
                    last_error = AIError(f"{model_name}: таймаут {self.timeout} с")
                except requests.exceptions.RequestException as e:
                    last_error = AIUnavailable(_short_network_error(e))
                time.sleep(2 * attempt)

        raise last_error or AIError("Ни одна модель не ответила")

    def chat_chain(self, messages, models=None, max_tokens=None, temperature=None):
        """
        Пробует модели по очереди, пока какая-нибудь не ответит.
        Возвращает (текст, имя модели). Удобно для генерации гайдов.
        """
        chain = list(models or self.models)
        last_error = None
        for model_name in chain:
            try:
                text = self.chat(messages, model=model_name, max_tokens=max_tokens, temperature=temperature)
                return text, model_name
            except (AILimitReached, AIUnavailable):
                raise  # лимит и недоступный адрес — не повод перебирать модели
            except AIError as e:
                last_error = e
                print(f"      [AI] ⚠️ {model_name} не справилась: {e}. Пробуем следующую...")
        raise last_error or AIError("Все модели перебраны, ответа нет")


# ─── Вспомогательные ───────────────────────────────────────────────────

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


# ─── Общая точка входа ─────────────────────────────────────────────────

client = AIClient()


def ai_ask(messages, models=None, max_tokens=None, temperature=None):
    """
    Главная функция для остального кода.
    1. Если Worker настроен — идём в него.
    2. Если нет — пробуем g4f (если установлен), чтобы бот не простаивал.
    Возвращает текст ответа.
    """
    if client.configured:
        text, _ = client.chat_chain(messages, models=models, max_tokens=max_tokens, temperature=temperature)
        return text

    return _ask_g4f(messages, models)


def _ask_g4f(messages, models=None):
    """Запасной путь: старая бесплатная библиотека. Работает, пока её не сломают."""
    try:
        import g4f  # noqa: F401
    except ImportError:
        raise AIUnavailable(
            "ИИ не настроен: в .env нет AI_WORKER_URL, а библиотека g4f не установлена. "
            "Поднимите Cloudflare Worker (см. worker/README.md) и впишите адрес в .env."
        )

    fallback_models = [m for m in (models or ["gpt-4o", "gpt-3.5-turbo"]) if not str(m).startswith("@cf/")]
    last_error = None
    for model_name in fallback_models:
        try:
            response = g4f.ChatCompletion.create(model=model_name, messages=messages)
            if response and str(response).strip():
                return str(response).strip()
        except Exception as e:  # g4f бросает что угодно
            last_error = e
    raise AIUnavailable(f"И g4f не смог ответить: {last_error}")
