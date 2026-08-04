import time
import random
import os
import atexit
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from slugify import slugify

import config
from modules.logger import logger
from modules.games_list_reader import load_games_list, remove_game_from_list
from modules.wemod_checker import check_wemod_availability
from modules.ai_generator import ai_generate_product_ideas, ai_generate_full_content
from modules.gdrive_manager import gdrive
from modules.description_builder import (
    generate_short_description_ruble, 
    generate_full_description_ruble, 
    generate_short_description_wemod, 
    generate_full_description_wemod, 
    generate_payment_message
)
from modules.funpay_auth import get_session
from modules.network import (
    setup_network, remove_funpay_routes, make_warp_session, 
    is_warp_active, heal_network, cleanup_all_funpay_routes
)
from modules.lot_publisher import publish_lot, get_category_info
from modules.duplicate_checker import is_game_processed, mark_as_processed
from modules.category_finder import find_funpay_category

atexit.register(remove_funpay_routes)

MANDATORY_PRODUCTS = [
    {"title": "МАНУАЛ ПО ОПТИМИЗАЦИИ ПК ДЛЯ ИГР + НАСТРОЙКА NVIDIA BOOST FPS", "type": "мануал", "content_points": ["Настройка Win", "NVIDIA Boost", "FPS Fix"]},
    {"title": "10 СПОСОБОВ КАК ПОНИЗИТЬ ВЫСОКИЙ ПИНГ", "type": "инструкция", "content_points": ["Сеть", "DNS", "Latency"]},
    {"title": "[ СПОСОБ СНЯТЬ БЛОКИРОВКУ С ЖЕЛЕЗА ] [ СМЕНА HWID IP MAC АДРЕС UUID ]", "type": "чит-лист", "content_points": ["HWID", "IP", "MAC", "UUID"]}
]

def _heal_and_reconnect(net_session):
    """Чинит сеть (WARP, маршруты) и пересоздаёт сессию. Возвращает (session, net_session)."""
    print("   [Сеть] 🔧 Сеть отвалилась! Запускаем авто-починку...")
    new_session, ok = heal_network()
    if ok:
        net_session = new_session
        session = get_session(network_session=net_session, fresh=True)
        if session:
            print("   [Сеть] ✅ Сеть починена, авторизация восстановлена!")
            return session, net_session
        else:
            print("   [Сеть] ⚠️ Сеть починена, но авторизация не удалась. Ждём 2 мин...")
            time.sleep(120)
            session = get_session(network_session=net_session, fresh=True)
            return session, net_session
    else:
        print("   [Сеть] ❌ Авто-починка не помогла. Ждём 5 мин...")
        time.sleep(300)
        session = get_session(network_session=net_session, fresh=True)
        return session, net_session

def _publish_with_heal(session, net_session, game_id, short, full, pay_msg, price, max_heal_attempts=3):
    """
    Публикует лот с авто-починкой сети при обрывах.
    Если сеть отваливается — чинит и ПРОБУЕТ СНОВА тот же лот.
    НЕ пропускает игру! Возвращает (result, session, net_session).
    """
    for heal_attempt in range(max_heal_attempts):
        result = publish_lot(session, {"game_id": game_id}, short, full, pay_msg, price)
        
        if result != "network_error":
            return result, session, net_session
        
        # Сеть отвалилась — чиним и пробуем ещё раз
        print(f"   [Сеть] ⚠️ Сетевая ошибка (попытка починки {heal_attempt+1}/{max_heal_attempts})...")
        session, net_session = _heal_and_reconnect(net_session)
        
        if not session:
            print("   [Сеть] ❌ Не удалось восстановить сессию. Ждём 3 мин и пробуем снова...")
            time.sleep(180)
            session = get_session(network_session=net_session, fresh=True)
            if not session:
                continue
        # Если сессия восстановлена — пробуем опубликовать лот ещё раз (цикл продолжится)
    
    # После всех попыток починки — последняя попытка публикации
    result = publish_lot(session, {"game_id": game_id}, short, full, pay_msg, price)
    return result, session, net_session

def process_single_game(session, game_data, net_session):
    game_name = game_data['game_name']
    found = find_funpay_category(session, game_name)
    if not found:
        print(f"   [!] Игра '{game_name}' не найдена. Пропуск.")
        remove_game_from_list(game_name); return session, net_session
    
    game_id = found['id']
    if is_game_processed(game_id):
        remove_game_from_list(game_name); return session, net_session

    print(f"\n🌍 РАЗДЕЛ: {get_category_info(session, game_id)} ({found['type']})")
    print(f"📊 ИГРА: {game_name.upper()}")

    # 15 товаров: 3 ваших + 12 от ИИ
    ai_ideas = ai_generate_product_ideas(game_name) or []
    final_ideas = MANDATORY_PRODUCTS + ai_ideas[:12]

    try:
        folder_id = gdrive.create_folder(f"{game_name}_products")
        if not folder_id: return session, net_session

        fail_count = 0
        # Предзагрузка: генерируем контент следующего лота пока текущий публикуется
        ai_pool = ThreadPoolExecutor(max_workers=1)
        next_content_future = None

        for i, idea in enumerate(final_ideas):
            print(f"📦 [{game_name}] {i+1}/{len(final_ideas)}: {idea['title']}")
            
            # Берём контент: из предзагрузки или генерируем сейчас
            if next_content_future is not None:
                print(f"      ⚡ Берём предзагруженный контент...")
                content = next_content_future.result()
                next_content_future = None
            else:
                content = ai_generate_full_content(idea, game_name)
            
            # Запускаем предзагрузку СЛЕДУЮЩЕГО лота в фоне
            if i + 1 < len(final_ideas):
                next_idea = final_ideas[i + 1]
                next_content_future = ai_pool.submit(ai_generate_full_content, next_idea, game_name)
            
            if not content:
                print(f"      ⚠️ Гайд не сгенерирован. Пропускаем товар.")
                continue
            file_name = f"{slugify(idea['title'][:30])}_{random.randint(100,999)}.txt"
            file_path = f"generated_content/{file_name}"
            os.makedirs("generated_content", exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f: f.write(content)
            
            link = gdrive.upload_file(file_path, folder_id)
            short = generate_short_description_ruble(idea['title'])
            full = generate_full_description_ruble(idea, game_name)
            pay_msg = generate_payment_message(link)
            
            # Публикуем с авто-починкой — бот НЕ пропускает игру при обрывах!
            result, session, net_session = _publish_with_heal(
                session, net_session, game_id, short, full, pay_msg, 1
            )
            
            if result == "cloudflare_banned":
                print(f"   [🚫] Cloudflare заблокировал IP. Пауза {config.CLOUDFLARE_PAUSE // 60} мин...")
                time.sleep(config.CLOUDFLARE_PAUSE)
                session = get_session(network_session=net_session, fresh=True)
                if not session:
                    time.sleep(300); return session, net_session
                continue
            if result == "limit_reached":
                print(f"   [!] Лимит лотов исчерпан. Переходим к следующей игре.")
                mark_as_processed(game_id)
                remove_game_from_list(game_name)
                return session, net_session
            
            # Короткий английский текст — не считается неудачей
            if result == "too_short_en":
                print(f"      ⚠️ Английский текст слишком короткий. Пропускаем товар.")
                continue
            
            # Длинный русский текст — не считается неудачей
            if result == "too_long_ru":
                print(f"      ⚠️ Русский текст слишком длинный. Пропускаем товар.")
                continue
            
            # Считаем подряд неудачные публикации БЕЗ конкретной ошибки
            if result in ("no_csrf", None):
                fail_count += 1
                if fail_count >= 3:
                    print(f"   [!] 3 неудачи подряд без ответа. Переходим к следующей игре.")
                    mark_as_processed(game_id)
                    remove_game_from_list(game_name)
                    return session, net_session
            else:
                fail_count = 0
            
            if os.path.exists(file_path): os.remove(file_path)
            time.sleep(random.randint(3, 6))

        if check_wemod_availability(game_name):
            print(f"💎 СОЗДАНИЕ WEMOD...")
            wemod_result, session, net_session = _publish_with_heal(
                session, net_session, game_id,
                generate_short_description_wemod(game_name),
                generate_full_description_wemod(game_name),
                generate_payment_message(config.WEMOD_FIXED_LINK), 60
            )

        mark_as_processed(game_id)
        remove_game_from_list(game_name)
        print(f"✨ {game_name} Готово.")
    except Exception as e:
        print(f"   [!] Ошибка в игре: {e}")
        remove_game_from_list(game_name)
    
    return session, net_session

def main():
    print("=== FUNPAY FINAL-STABLE BOT v18.0 STARTED ===\n")
    net_session, net_ok = setup_network()
    
    # Подсказка про WARP
    if is_warp_active():
        print("   [Сеть] ☁️ WARP обнаружен. Zapret нужен для стабильности — бот запустит его сам!")
        print("   [Сеть]   (Положите Zapret в Desktop/zapret или C:/zapret)")
    
    while True:
        try:
            session = get_session(network_session=net_session)
            if not session:
                print("   [!] Не удалось авторизоваться. Пробуем авто-починку...")
                session, net_session = _heal_and_reconnect(net_session)
                if not session:
                    print("   [!] Ждём 5 мин...")
                    time.sleep(300); continue
            games = load_games_list()
            if not games:
                print("🛌 Список пуст. Ожидание..."); time.sleep(3600); continue
            session, net_session = process_single_game(session, games[0], net_session)
            time.sleep(5)
        except KeyboardInterrupt: break
        except Exception as e:
            print(f"💥 Ошибка цикла: {e}")
            time.sleep(20)

if __name__ == "__main__":
    main()
