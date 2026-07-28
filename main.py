import time
import random
import os
from datetime import datetime
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
from modules.lot_publisher import publish_lot, get_category_info
from modules.duplicate_checker import is_game_processed, mark_as_processed
from modules.category_finder import find_funpay_category

MANDATORY_PRODUCTS = [
    {"title": "МАНУАЛ ПО ОПТИМИЗАЦИИ ПК ДЛЯ ИГР + НАСТРОЙКА NVIDIA BOOST FPS", "type": "мануал", "content_points": ["Настройка Win", "NVIDIA Boost", "FPS Fix"]},
    {"title": "10 СПОСОБОВ КАК ПОНИЗИТЬ ВЫСОКИЙ ПИНГ", "type": "инструкция", "content_points": ["Сеть", "DNS", "Latency"]},
    {"title": "[ СПОСОБ СНЯТЬ БЛОКИРОВКУ С ЖЕЛЕЗА ] [ СМЕНА HWID IP MAC АДРЕС UUID ]", "type": "чит-лист", "content_points": ["HWID", "IP", "MAC", "UUID"]}
]

def process_single_game(session, game_data):
    game_name = game_data['game_name']
    found = find_funpay_category(session, game_name)
    if not found:
        print(f"   [!] Игра '{game_name}' не найдена. Пропуск.")
        remove_game_from_list(game_name); return
    
    game_id = found['id']
    if is_game_processed(game_id):
        remove_game_from_list(game_name); return

    print(f"\n🌍 РАЗДЕЛ: {get_category_info(session, game_id)} ({found['type']})")
    print(f"📊 ИГРА: {game_name.upper()}")

    # 15 товаров: 3 ваших + 12 от ИИ
    ai_ideas = ai_generate_product_ideas(game_name) or []
    final_ideas = MANDATORY_PRODUCTS + ai_ideas[:12]

    try:
        folder_id = gdrive.create_folder(f"{game_name}_products")
        if not folder_id: return

        for i, idea in enumerate(final_ideas):
            print(f"📦 [{game_name}] {i+1}/{len(final_ideas)}: {idea['title']}")
            
            content = ai_generate_full_content(idea, game_name)
            file_name = f"{slugify(idea['title'][:30])}_{random.randint(100,999)}.txt"
            file_path = f"generated_content/{file_name}"
            os.makedirs("generated_content", exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f: f.write(content)
            
            link = gdrive.upload_file(file_path, folder_id)
            short = generate_short_description_ruble(idea['title'])
            full = generate_full_description_ruble(idea, game_name)
            pay_msg = generate_payment_message(link)
            
            publish_lot(session, {"game_id": game_id}, short, full, pay_msg, 1)
            
            if os.path.exists(file_path): os.remove(file_path)
            time.sleep(random.randint(7, 15))

        if check_wemod_availability(game_name):
            print(f"💎 СОЗДАНИЕ WEMOD...")
            publish_lot(session, {"game_id": game_id}, generate_short_description_wemod(game_name), generate_full_description_wemod(game_name), generate_payment_message(config.WEMOD_FIXED_LINK), 60)

        mark_as_processed(game_id)
        remove_game_from_list(game_name)
        print(f"✨ {game_name} Готово.")
    except Exception as e:
        print(f"   [!] Ошибка в игре: {e}")
        remove_game_from_list(game_name)

def main():
    print("=== FUNPAY FINAL-STABLE BOT v15.0 STARTED ===\n")
    while True:
        try:
            session = get_session()
            if not session:
                time.sleep(300); continue
            games = load_games_list()
            if not games:
                print("🛌 Список пуст. Ожидание..."); time.sleep(3600); continue
            process_single_game(session, games[0])
            time.sleep(5)
        except KeyboardInterrupt: break
        except Exception as e:
            print(f"💥 Ошибка цикла: {e}")
            time.sleep(20)

if __name__ == "__main__":
    main()
