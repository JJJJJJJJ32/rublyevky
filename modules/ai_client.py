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
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse

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

# Прямой Gemini API: резервный путь, когда провайдер блокирует *.workers.dev.
# Ключ тот же самый, что лежит в секретах Worker'а.
DIRECT_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

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


def get_gemini_key():
    """Ключ Gemini из .env — резервный путь, если Worker недоступен."""
    return (os.getenv("GEMINI_API_KEY") or "").strip()


def direct_available():
    """Можно ли спросить Gemini напрямую, минуя Worker."""
    return bool(get_gemini_key())


def _direct_base():
    return (os.getenv("GEMINI_API_BASE") or DIRECT_API_BASE).strip().rstrip("/")


# ─── Рабочий адрес Cloudflare ──────────────────────────────────────────
# Cloudflare отдаёт на один и тот же Worker разные anycast-адреса, а
# провайдер в РФ режет часть из них. Найденный рабочий адрес запоминаем
# и подставляем только для имени Worker'а: TLS-имя (SNI) остаётся прежним.

WORKER_PORT = 443
_PINNED_IPS = {}
_PIN_LOCK = threading.Lock()
_REAL_GETADDRINFO = socket.getaddrinfo


def _getaddrinfo_with_pin(host, port, *args, **kwargs):
    """Подменяет только адрес Worker'а, остальные хосты идут как обычно."""
    with _PIN_LOCK:
        pinned = _PINNED_IPS.get(str(host).lower())
    if pinned and port in (WORKER_PORT, None):
        return _REAL_GETADDRINFO(pinned, port, *args, **kwargs)
    return _REAL_GETADDRINFO(host, port, *args, **kwargs)


def _install_addr_patch():
    if socket.getaddrinfo is not _getaddrinfo_with_pin:
        socket.getaddrinfo = _getaddrinfo_with_pin


def _worker_host():
    base = get_worker_url()
    if not base:
        return ""
    return (urlparse(base).hostname or "").lower()


def worker_ip():
    """Адрес Cloudflare, через который сейчас ходим (или None)."""
    with _PIN_LOCK:
        return _PINNED_IPS.get(_worker_host())


def _resolved_ips(host):
    ips = []
    try:
        for info in _REAL_GETADDRINFO(host, WORKER_PORT, type=socket.SOCK_STREAM):
            address = info[4][0]
            if ":" not in address and address not in ips:
                ips.append(address)
    except OSError:
        pass
    return ips


def _doh_ips(host):
    """
    Спрашивает адреса у Cloudflare DoH: DNS провайдера часто отдаёт
    именно те адреса Cloudflare, которые у него же и заблокированы.
    """
    try:
        resp = requests.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": host, "type": "A"},
            headers={"accept": "application/dns-json"},
            timeout=5,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [
            str(item.get("data"))
            for item in (data.get("Answer") or [])
            if item.get("type") == 1
        ]
    except Exception:
        return []


def _ip_works(host, ip, timeout=4.0):
    """Быстрая проверка: TLS с именем Worker'а + GET /health (он без токена)."""
    try:
        context = ssl.create_default_context()
        with socket.create_connection((ip, WORKER_PORT), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                request = (
                    f"GET /health HTTP/1.1\r\nHost: {host}\r\n"
                    "User-Agent: rublyevky-bot\r\nConnection: close\r\n\r\n"
                )
                tls.sendall(request.encode("ascii"))
                head = tls.recv(64)
        return head.startswith(b"HTTP/1.1 200") or head.startswith(b"HTTP/1.0 200")
    except (OSError, ssl.SSLError, ValueError):
        return False


def _first_working(host, candidates, timeout):
    """Проверяет адреса параллельно и возвращает первый отвечающий."""
    if not candidates:
        return None
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
        results = list(pool.map(lambda ip: _ip_works(host, ip, timeout), candidates))
    for ip, works in zip(candidates, results):
        if works:
            return ip
    return None


def ensure_worker_ip(timeout=3.0):
    """
    Ищет рабочий адрес Cloudflare для Worker'а и запоминает его.

    Адреса проверяются параллельно, поэтому запуск не растягивается
    на несколько таймаутов подряд. Возвращает выбранный адрес или None.
    """
    host = _worker_host()
    if not host:
        return None

    candidates = _resolved_ips(host)
    found = _first_working(host, candidates, timeout)
    if not found:
        # DNS провайдера часто отдаёт именно заблокированные адреса,
        # поэтому при неудаче спрашиваем ещё и Cloudflare DoH.
        extra = [ip for ip in _doh_ips(host) if ip not in candidates]
        found = _first_working(host, extra, timeout)

    if found:
        with _PIN_LOCK:
            _PINNED_IPS[host] = found
        logger.info(f"[AI] Рабочий адрес Cloudflare: {found}")
        return found

    with _PIN_LOCK:
        _PINNED_IPS.pop(host, None)
    return None


def switch_worker_ip():
    """Перепроверяет адреса. True — если найден другой рабочий адрес."""
    host = _worker_host()
    with _PIN_LOCK:
        current = _PINNED_IPS.get(host)
    found = ensure_worker_ip()
    return bool(found) and found != current


_install_addr_patch()


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

    # ─── Прямой Gemini API (обход блокировки Worker'а) ───────────────────

    @staticmethod
    def _direct_payload(messages, max_tokens=None, temperature=None):
        """Переводит сообщения формата OpenAI в тело запроса Gemini."""
        contents, system_parts = [], []
        for message in messages or []:
            role = str(message.get("role") or "user")
            text = message.get("content")
            if isinstance(text, list):
                text = "".join(
                    part.get("text", "") for part in text if isinstance(part, dict)
                )
            text = str(text or "")
            if role == "system":
                system_parts.append(text)
                continue
            contents.append(
                {
                    "role": "model" if role == "assistant" else "user",
                    "parts": [{"text": text}],
                }
            )

        payload = {"contents": contents}
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}
        config = {}
        if max_tokens:
            config["maxOutputTokens"] = int(max_tokens)
        if temperature is not None:
            config["temperature"] = float(temperature)
        if config:
            payload["generationConfig"] = config
        return payload

    def direct_chat(self, model_name, messages, max_tokens=None, temperature=None):
        """
        Запрос напрямую в Gemini API (generativelanguage.googleapis.com).

        Нужен, когда провайдер режет *.workers.dev: FunPay и Google при этом
        открываются, поэтому бот продолжает работать без домена и прокси.
        """
        key = get_gemini_key()
        if not key:
            raise AIUnavailable("GEMINI_API_KEY не задан — прямой путь в Gemini недоступен")

        url = f"{_direct_base()}/models/{model_name}:generateContent"
        started = time.time()
        try:
            resp = self.session.post(
                url,
                headers={"Content-Type": "application/json", "x-goog-api-key": key},
                json=self._direct_payload(messages, max_tokens, temperature),
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as e:
            raise AIUnavailable(f"Gemini напрямую недоступен: {_short_network_error(e)}")

        elapsed = round(time.time() - started, 1)
        error_text = _extract_error(resp)

        if resp.status_code == 429:
            raise AILimitReached(error_text or f"{model_name}: лимит Gemini исчерпан")
        if resp.status_code in (401, 403):
            raise AIUnavailable(f"Gemini отклонил GEMINI_API_KEY: {error_text}")
        if resp.status_code == 400 and "api key" in error_text.lower():
            raise AIUnavailable(f"Gemini отклонил GEMINI_API_KEY: {error_text}")
        if resp.status_code == 404:
            raise AIModelNotSupported(f"{model_name}: {error_text}")
        if resp.status_code >= 500:
            raise AIUnavailable(f"Gemini вернул {resp.status_code}: {error_text}")
        if resp.status_code != 200:
            raise AIError(f"{model_name}: Gemini вернул {resp.status_code}: {error_text}")

        data = resp.json()
        text = _extract_gemini_text(data)
        if not text:
            raise AIError(f"{model_name}: Gemini вернул пустой ответ")

        _log_usage(model_name, _gemini_usage(data), elapsed, len(text))
        logger.info(
            f"[AI] {model_name} ответила напрямую через Gemini API за {elapsed} с ({len(text)} симв.)"
        )
        return text

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
            # Провайдер режет часть адресов Cloudflare — ищем рабочий и пробуем ещё раз.
            if switch_worker_ip():
                try:
                    resp = self.session.get(self._url("/health"), headers=self._headers(), timeout=30)
                except requests.exceptions.RequestException as second:
                    return False, _short_network_error(second)
            else:
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

    def chat(self, messages, model=None, max_tokens=None, temperature=None, attempts=None, _depth=0):
        """
        Один запрос к указанной модели (или к первой доступной из списка).
        Возвращает текст ответа. Бросает AIError-наследников при проблемах.
        """
        if not self.configured and not direct_available():
            raise AIUnavailable(
                "AI_WORKER_URL не задан — Worker не настроен, "
                "и GEMINI_API_KEY для прямого пути тоже нет"
            )

        models = [model] if model else list(self.models)
        attempts = attempts or ATTEMPTS_PER_MODEL
        last_error = None

        for model_name in models:
            for attempt in range(1, attempts + 1):
                if not self.configured:
                    # Worker'а нет вовсе — сразу идём в Gemini API.
                    return self.direct_chat(model_name, messages, max_tokens, temperature)

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
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                    # Адрес один для всех моделей: если он недоступен,
                    # перебирать модели бессмысленно. Сначала пробуем другой
                    # адрес Cloudflare (провайдер режет часть из них), потом —
                    # прямой Gemini, если в .env есть ключ.
                    reason = _short_network_error(e)
                    logger.info(f"[AI] Worker недоступен: {reason}")
                    if _depth < 1 and switch_worker_ip():
                        logger.info("[AI] Повторяю запрос через рабочий адрес Cloudflare")
                        return self.chat(
                            messages,
                            model=model_name,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            attempts=attempts,
                            _depth=_depth + 1,
                        )
                    if not direct_available():
                        raise AIUnavailable(reason)
                    logger.info("[AI] Иду в Gemini API напрямую (GEMINI_API_KEY из .env)")
                    try:
                        return self.direct_chat(model_name, messages, max_tokens, temperature)
                    except AILimitReached:
                        raise
                    except AIError as direct_error:
                        raise AIUnavailable(
                            f"{reason}; напрямую Gemini тоже не ответил: {direct_error}"
                        ) from direct_error
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


def _extract_gemini_text(data):
    """Достаёт текст из нативного ответа Gemini API."""
    try:
        candidates = data.get("candidates") or []
        if not candidates:
            return ""
        parts = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    except Exception:
        return ""


def _gemini_usage(data):
    """Приводит usageMetadata Gemini к тем же ключам, что у Worker'а."""
    meta = data.get("usageMetadata") or {}
    return {
        "total_tokens": meta.get("totalTokenCount"),
        "output_tokens": meta.get("candidatesTokenCount"),
    }


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
    if not client.configured and not direct_available():
        raise AIUnavailable(
            "ИИ не настроен: в .env нет AI_WORKER_URL и нет GEMINI_API_KEY. "
            "Поднимите Cloudflare Worker (см. README.md, раздел 4) "
            "и впишите его адрес в .env — или добавьте ключ Gemini для прямого пути."
        )
    text, _ = client.chat_chain(messages, models=models, max_tokens=max_tokens, temperature=temperature)
    return text
