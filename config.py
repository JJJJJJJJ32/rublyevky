# Лимиты FunPay 2026
LIMITS = {
    "summary_min": 10,
    "summary_max": 70,           # FunPay считает по байтам, эмодзи = 3-4 байта каждый
    "summary_max_bytes": 120,    # Байтовый лимит для summary (FunPay считает байты, не символы)
    "description_min_ru": 200,
    "description_max_ru": 3000,
    "description_min_en": 500,   # FunPay требует минимум ~500 для desc[en], 300 недостаточно
    "payment_msg_min_en": 300,   # FunPay требует минимум 300 символов для английских полей
    "payment_msg_max": 1000
}

# Фиксированная ссылка для WeMod товаров
WEMOD_FIXED_LINK = "https://drive.google.com/drive/folders/1qyFeH_ZTZH_iWpaVdJgCgCW1ls5CQ_gu?usp=sharing"

# Настройки
FUNPAY_BASE_URL = "https://funpay.com"
DATA_DIR = "data"
LOGS_DIR = "logs"
GENERATED_CONTENT_DIR = "generated_content"

# Антибан: пауза при ошибке Cloudflare (секунды)
CLOUDFLARE_PAUSE = 900  # 15 минут

# Пул User-Agent для рандомизации
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 OPR/108.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

EMOJI_BANK = ["🔥", "💎", "✨", "🌟", "⚡", "🎯", "🏆", "🎮", "💫", "🎁", "🔑", "🌈", "⚔️", "🛡️", "👑", "🚀", "💥", "⭐", "🏮", "💖", "🎪", "🎨", "🎭", "📌", "📍"]
COLOR_BANK = ["🔴", "🟡", "🟢", "🔵", "🟣", "🟠", "⚪", "🟤", "⚫"]
