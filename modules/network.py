"""
modules/network.py — минимальный сетевой слой.

Что делает:
  1. Собирает requests.Session (с прокси из .env, если он указан).
  2. Проверяет, доступен ли FunPay, и объясняет причину, если нет.
  3. Больше ничего: не запускает сторонние программы, не меняет маршруты,
     не требует прав администратора.

Почему так (история): раньше этот файл весил 900+ строк и управлял
Запретом (winws.exe), Cloudflare WARP и системными маршрутами (route add/delete).
Это лечило не ту болезнь: бот воевал с системой вместо того, чтобы торговать.
Если FunPay открывается напрямую — ничего не нужно вообще.
Если когда-нибудь перестанет — достаточно указать прокси в .env:
    PROXY_URL=http://user:pass@host:port
"""

import os
import socket
import time
import requests
from dotenv import load_dotenv

load_dotenv()

FUNPAY_BASE_URL = "https://funpay.com"
DEFAULT_TIMEOUT = 20

# Сколько раз пробуем достучаться до FunPay, прежде чем сдаться
CONNECT_ATTEMPTS = 3
RETRY_PAUSE = 5


def get_proxy_url():
    """Прокси из .env. Пусто или не задан — ходим напрямую."""
    raw = os.getenv("FUNPAY_PROXY_URL") or os.getenv("PROXY_URL") or ""
    raw = raw.strip()
    return raw or None


def build_session(proxy_url=None):
    """
    Создаёт обычную requests.Session.
    Если задан прокси — весь трафик сессии идёт через него.
    """
    session = requests.Session()
    session.headers.update({
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    })

    proxy = proxy_url if proxy_url is not None else get_proxy_url()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
        print(f"   [Сеть] 🌐 Работаем через прокси: {_mask_proxy(proxy)}")
    return session


def _mask_proxy(proxy):
    """Прячет логин и пароль прокси в логах."""
    if "@" in proxy:
        head, tail = proxy.rsplit("@", 1)
        scheme = head.split("//")[0] if "//" in head else ""
        return f"{scheme}//***@{tail}"
    return proxy


def _classify_error(exc):
    """Превращает исключение requests в понятную человеку причину."""
    text = str(exc).lower()

    if "name or service not known" in text or "getaddrinfo" in text or "temporary failure in name resolution" in text:
        return "DNS не смог найти адрес FunPay (проверь интернет или DNS)"
    if "timed out" in text or "timeout" in text:
        return "Timeout — соединение не установилось (нужен прокси или VPN)"
    if "unexpected_eof" in text or "sslerror" in text or "ssl" in text:
        return "SSL-соединение оборвано (похоже на блокировку провайдером — нужен прокси)"
    if "connection refused" in text:
        return "Соединение отклонено (проверь прокси)"
    if "connection reset" in text:
        return "Соединение сброшено (нужен прокси)"
    return f"Сетевая ошибка: {exc}"


def check_funpay(session=None):
    """
    Проверяет доступность FunPay.
    Возвращает (успех: bool, сообщение: str).
    """
    own_session = session is None
    session = session or build_session()

    last_reason = "неизвестная ошибка"
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            resp = session.get(FUNPAY_BASE_URL, timeout=DEFAULT_TIMEOUT)
            body = resp.text.lower()

            if resp.status_code == 403 or "just a moment" in body or "cf-browser-verification" in body:
                return False, "Cloudflare требует проверку браузера (403) — попробуй прокси в .env"
            if resp.status_code != 200:
                return False, f"FunPay ответил статусом {resp.status_code}"
            if "funpay" not in body:
                return False, "Ответ пришёл, но это не страница FunPay (возможно, подмена провайдером)"
            return True, "FunPay доступен"

        except requests.exceptions.RequestException as e:
            last_reason = _classify_error(e)
            if attempt < CONNECT_ATTEMPTS:
                print(f"   [Сеть] ⚠️ Попытка {attempt}/{CONNECT_ATTEMPTS}: {last_reason}. Повтор...")
                time.sleep(RETRY_PAUSE)
        except Exception as e:
            last_reason = f"Неожиданная ошибка: {e}"
            break

    if own_session:
        session.close()
    return False, last_reason


def setup_network():
    """
    Готовит сессию для работы. Вызывается один раз при старте бота.
    Возвращает (session, успех: bool).

    Никаких Запретов, WARP и маршрутов: только сессия и проверка связи.
    """
    print("   [Сеть] Проверяем доступ к FunPay...")

    if get_proxy_url():
        session = build_session()
        ok, message = check_funpay(session)
        print(f"   [Сеть] {'✅' if ok else '❌'} {message}")
        return session, ok

    # Сначала пробуем напрямую
    direct = build_session()
    ok, message = check_funpay(direct)
    if ok:
        print("   [Сеть] ✅ FunPay доступен напрямую (прокси не нужен).")
        return direct, True

    print(f"   [Сеть] ⚠️ Напрямую не получилось: {message}")
    print("   [Сеть]   Если так будет всегда — впиши прокси в .env:")
    print("   [Сеть]   PROXY_URL=http://логин:пароль@адрес:порт")
    return direct, False


def recheck_network(session=None):
    """
    Быстрая перепроверка связи (используется, когда запрос упал с сетевой ошибкой).
    Возвращает (session, успех: bool). Сессию пересоздаёт только если она мертва.
    """
    session = session or build_session()
    ok, message = check_funpay(session)
    if ok:
        return session, True

    print(f"   [Сеть] 🔌 Связь пропала: {message}")
    print("   [Сеть]   Пересобираем сессию...")
    try:
        session.close()
    except Exception:
        pass

    fresh = build_session()
    ok, message = check_funpay(fresh)
    print(f"   [Сеть] {'✅ Связь восстановлена' if ok else f'❌ Всё ещё нет связи: {message}'}")
    return fresh, ok


def get_local_ip():
    """Локальный IP — только для вывода в лог, никакой маршрутизации."""
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "неизвестен"
