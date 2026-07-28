import os
from dotenv import load_dotenv
import time

load_dotenv()

def get_session(network_session=None):
    golden_key = os.getenv("GOLDEN_KEY")
    if not golden_key:
        print("   [!] ОШИБКА: GOLDEN_KEY не найден в .env")
        return None
    
    print("   [Проверка] Авторизация на FunPay...")
    session = network_session
    
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
