import os
import random
import requests
from dotenv import load_dotenv

load_dotenv()

def get_session(network_session=None, fresh=False):
    golden_key = os.getenv("GOLDEN_KEY")
    if not golden_key:
        print("   [!] ОШИБКА: GOLDEN_KEY не найден в .env")
        return None
    
    print("   [Проверка] Авторизация на FunPay...")
    # Функцию можно безопасно вызывать и без заранее созданной сетевой сессии.
    session = network_session or requests.Session()

    # Ротация сессии: если fresh=True — очищаем куки для чистого старта
    if fresh and session:
        session.cookies.clear()
    
    # Рандомный User-Agent при каждом вызове
    import config
    ua = random.choice(config.USER_AGENTS)
    print(f"   [Сеть] 🎭 User-Agent: {ua[13:40]}...")
    
    session.headers.update({
        "User-Agent": ua,
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    })
    
    try:
        session.get("https://funpay.com/", timeout=20)
        session.cookies.set("golden_key", golden_key, domain=".funpay.com")

        resp = session.get("https://funpay.com/", timeout=20)
        response_text = resp.text or ""
        response_lower = response_text.lower()
        if (
            resp.status_code == 403
            or "cf-browser-verification" in response_lower
            or "just a moment" in response_lower
        ):
            print("   [!] FunPay ответил защитой Cloudflare. Проверьте WARP/Zapret.")
            return None
        if "cp-login" in response_lower or "auth/login" in response_lower:
            print("   [!] ОШИБКА: Golden Key недействителен.")
            return None
        if resp.status_code >= 400:
            print(f"   [!] FunPay вернул HTTP {resp.status_code}.")
            return None

        print("   [Успех] Авторизация подтверждена.")
        session.headers.update({"X-Requested-With": "XMLHttpRequest"})
        return session
    except requests.exceptions.RequestException as e:
        print(f"   [!] Ошибка сети: {e}")
        return None
