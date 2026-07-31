import os
import random
from dotenv import load_dotenv
import time

load_dotenv()

def get_session(network_session=None, fresh=False):
    golden_key = os.getenv("GOLDEN_KEY")
    if not golden_key:
        print("   [!] ОШИБКА: GOLDEN_KEY не найден в .env")
        return None
    
    print("   [Проверка] Авторизация на FunPay...")
    session = network_session

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
        if "cp-login" in resp.text or "auth/login" in resp.text:
            print("   [!] ОШИБКА: Golden Key недействителен.")
            return None
            
        print("   [Успех] Авторизация подтверждена.")
        session.headers.update({"X-Requested-With": "XMLHttpRequest"})
        return session
    except Exception as e:
        print(f"   [!] Ошибка сети: {e}")
        return None
