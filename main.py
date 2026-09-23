import os
import sys
import random
import time

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
from modules.network import setup_network, recheck_network, get_proxy_url
from modules.lot_publisher import publish_lot, get_category_info
from modules.duplicate_checker import is_game_processed, mark_as_processed
from modules.category_finder import find_funpay_category
from modules import ai_client

MANDATORY_PRODUCTS = [
    {"title": "МАНУАЛ ПО ОПТИМИЗАЦИИ ПК ДЛЯ ИГР + НАСТРОЙКА NVIDIA BOOST FPS", "type": "мануал", "content_points": ["Настройка Win", "NVIDIA Boost", "FPS Fix"]},
    {"title": "10 СПОСОБОВ КАК ПОНИЗИТЬ ВЫСОКИЙ ПИНГ", "type": "инструкция", "content_points": ["Сеть", "DNS", "Latency"]},
    {"title": "[ СПОСОБ СНЯТЬ БЛОКИРОВКУ С ЖЕЛЕЗА ] [ СМЕНА HWID IP MAC АДРЕС UUID ]", "type": "чит-лист", "content_points": ["HWID", "IP", "MAC", "UUID"]}
]


def _reconnect(net_session):
    """
    Пересобирает сетевую сессию и заново авторизуется.
    Никаких правок маршрутов и запуска сторонних программ — только сессия.
    Возвращает (session, net_session).
    """
    print("   [Сеть] 🔌 Проблема со связью. Пересобираем сессию...")
    net_session, ok = recheck_network(net_session)

    if not ok:
        print("   [Сеть] ⏳ Связи нет. Ждём 60 секунд и пробуем снова...")
        time.sleep(60)
        net_session, ok = recheck_network(net_session)

    if not ok:
        print("   [Сеть] ❌ FunPay недоступен. Подсказка: укажи прокси в .env (PROXY_URL=...)")
        logger.log_error("FunPay недоступен: нет связи")

    session = get_session(network_session=net_session, fresh=True)
    if session:
        print("   [Сеть] ✅ Сессия восстановлена, авторизация прошла.")
    else:
        print("   [Сеть] ⚠️ Связь есть, но авторизация не прошла. Проверь GOLDEN_KEY в .env.")
    return session, net_session


def _publish_with_heal(session, net_session, game_id, short, full, pay_msg, price, max_heal_attempts=3):
    """
    Публикует лот и переживает обрывы связи: чинит сессию и пробует тот же лот снова.
    Возвращает (result, session, net_session).
    """
    for heal_attempt in range(max_heal_attempts):
        result = publish_lot(session, {"game_id": game_id}, short, full, pay_msg, price)

        if result != "network_error":
            return result, session, net_session

        print(f"   [Сеть] ⚠️ Сетевая ошибка (попытка починки {heal_attempt + 1}/{max_heal_attempts})...")
        session, net_session = _reconnect(net_session)

        if not session:
            print("   [Сеть] ⏳ Ждём 60 секунд и пробуем снова...")
            time.sleep(60)
            session = get_session(network_session=net_session, fresh=True)

    result = publish_lot(session, {"game_id": game_id}, short, full, pay_msg, price)
    return result, session, net_session


def process_single_game(session, game_data, net_session):
    game_name = game_data['game_name']
    found = find_funpay_category(session, game_name)
    if not found:
        print(f"   [!] Игра '{game_name}' не найдена. Пропуск.")
        remove_game_from_list(game_name)
        return session, net_session

    game_id = found['id']
    if is_game_processed(game_id):
        remove_game_from_list(game_name)
        return session, net_session

    print(f"\n🌍 РАЗДЕЛ: {get_category_info(session, game_id)} ({found['type']})")
    print(f"📊 ИГРА: {game_name.upper()}")

    # 15 товаров: 3 обязательных + 12 от ИИ
    ai_ideas = ai_generate_product_ideas(game_name) or []
    final_ideas = MANDATORY_PRODUCTS + ai_ideas[:12]

    try:
        folder_id = gdrive.create_folder(f"{game_name}_products")
        if not folder_id:
            print("   [!] Не удалось создать папку на Google Drive. Пропускаем игру.")
            logger.log_error(f"Google Drive: не создалась папка для {game_name}")
            return session, net_session

        fail_count = 0
        for i, idea in enumerate(final_ideas):
            print(f"📦 [{game_name}] {i+1}/{len(final_ideas)}: {idea['title']}")

            content = ai_generate_full_content(idea, game_name)
            if not content:
                print("      ⚠️ Гайд не сгенерирован. Пропускаем товар.")
                continue

            file_name = f"{slugify(idea['title'][:30])}_{random.randint(100, 999)}.txt"
            file_path = os.path.join(config.GENERATED_CONTENT_DIR, file_name)
            os.makedirs(config.GENERATED_CONTENT_DIR, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            try:
                link = gdrive.upload_file(file_path, folder_id)
            finally:
                # Файл нужен был только для загрузки — убираем сразу,
                # чтобы папка не забивалась обрывками при ошибках.
                if os.path.exists(file_path):
                    os.remove(file_path)

            short = generate_short_description_ruble(idea['title'])
            full = generate_full_description_ruble(idea, game_name)
            pay_msg = generate_payment_message(link)

            result, session, net_session = _publish_with_heal(
                session, net_session, game_id, short, full, pay_msg, 1
            )

            if result == "cloudflare_banned":
                print(f"   [🚫] Cloudflare требует проверку. Пауза {config.CLOUDFLARE_PAUSE // 60} мин...")
                time.sleep(config.CLOUDFLARE_PAUSE)
                session = get_session(network_session=net_session, fresh=True)
                if not session:
                    logger.log_error("Cloudflare: не удалось авторизоваться после паузы")
                    return session, net_session
                continue

            if result == "limit_reached":
                print("   [!] Лимит лотов в категории исчерпан. Переходим к следующей игре.")
                mark_as_processed(game_id)
                remove_game_from_list(game_name)
                return session, net_session

            if result == "too_short_en":
                print("      ⚠️ Английский текст слишком короткий. Пропускаем товар.")
                continue

            if result == "too_long_ru":
                print("      ⚠️ Русский текст слишком длинный. Пропускаем товар.")
                continue

            # Неудачи без конкретной ошибки считаем подряд
            if result in ("no_csrf", None):
                fail_count += 1
                logger.log_error(f"Публикация без ответа ({game_name}, товар {i + 1}): {result}")
                if fail_count >= 3:
                    print("   [!] 3 неудачи подряд без ответа. Переходим к следующей игре.")
                    mark_as_processed(game_id)
                    remove_game_from_list(game_name)
                    return session, net_session
            else:
                fail_count = 0

            time.sleep(random.randint(7, 15))

        # WeMod-лот
        if check_wemod_availability(game_name):
            print("💎 СОЗДАНИЕ WEMOD...")
            wemod_result, session, net_session = _publish_with_heal(
                session, net_session, game_id,
                generate_short_description_wemod(game_name),
                generate_full_description_wemod(game_name),
                generate_payment_message(config.WEMOD_FIXED_LINK), 60
            )
            if wemod_result == "success":
                print("      ✅ WeMod-лот выставлен.")
            elif wemod_result == "limit_reached":
                print("      ⚠️ Лимит лотов — WeMod пропущен.")
            elif wemod_result == "cloudflare_banned":
                print(f"      🚫 Cloudflare: пауза {config.CLOUDFLARE_PAUSE // 60} мин...")
                time.sleep(config.CLOUDFLARE_PAUSE)
                session = get_session(network_session=net_session, fresh=True)
            else:
                print(f"      ⚠️ WeMod-лот не выставлен ({wemod_result}).")
                logger.log_error(f"WeMod-лот не выставлен ({game_name}): {wemod_result}")

        mark_as_processed(game_id)
        remove_game_from_list(game_name)
        print(f"✨ {game_name} готово.")

    except Exception as e:
        print(f"   [!] Ошибка в игре: {e}")
        logger.log_error(f"Ошибка обработки игры {game_name}: {e}")
        remove_game_from_list(game_name)

    return session, net_session


# ═══════════════════════════════════════════════════════════════════════
# Самопроверка: py main.py --selftest
# ═══════════════════════════════════════════════════════════════════════

def run_selftest():
    """
    Проверяет всё, что нужно боту, за полминуты и печатает понятный отчёт.
    Ничего не публикует и не меняет.
    """
    print("=" * 60)
    print("САМОПРОВЕРКА БОТА")
    print("=" * 60)

    problems = []

    # 1. .env
    print("\n[1/5] Файл .env")
    if not os.path.exists(".env"):
        print("   ❌ Файла .env нет. Создай его рядом с main.py (см. .env.example)")
        problems.append(".env отсутствует")
    else:
        print("   ✅ .env найден")
    print(f"   • GOLDEN_KEY: {'✅ есть' if os.getenv('GOLDEN_KEY') else '❌ нет'}")
    print(f"   • GOOGLE_DRIVE_FOLDER_ID: {'✅ есть' if os.getenv('GOOGLE_DRIVE_FOLDER_ID') else '❌ нет'}")
    print(f"   • AI_WORKER_URL: {'✅ есть' if ai_client.get_worker_url() else '⚠️ нет (ИИ пойдёт через g4f)'}")
    print(f"   • AI_WORKER_TOKEN: {'✅ есть' if ai_client.get_worker_token() else '⚠️ нет'}")
    print(f"   • PROXY_URL: {'✅ задан' if get_proxy_url() else '— не задан (ходим напрямую)'}")

    # 2. Worker ИИ
    print("\n[2/5] Cloudflare Worker (ИИ)")
    if not ai_client.client.configured:
        print("   ⚠️ Worker не настроен — пропускаем. Инструкция: worker/README.md")
    else:
        ok, info = ai_client.client.health()
        if ok:
            print(f"   ✅ Worker отвечает: {ai_client.get_worker_url()}")
            print(f"   • дата-центр Cloudflare: {info.get('colo', '?')}")
            print(f"   • версия Worker'а: {info.get('version', '?')}")
            providers = info.get("providers") or {}
            for name, state in providers.items():
                print(f"   • {name}: {'✅ ключ есть' if state else '— ключ не задан'}")
        else:
            print(f"   ❌ {info}")
            problems.append(f"Worker: {info}")

        print("\n   Пробуем сгенерировать тестовый текст...")
        try:
            text = ai_client.ai_ask(
                [{"role": "user", "content": "Напиши одно короткое предложение по-русски: тест связи."}],
                max_tokens=60,
            )
            print(f"   ✅ ИИ ответил: {text.strip()[:120]}")
        except ai_client.AILimitReached as e:
            print(f"   ⚠️ Лимит ИИ исчерпан на сегодня: {e}")
            print("      Бот будет ждать сброса лимита (00:00 UTC = 03:00 МСК).")
        except ai_client.AIError as e:
            print(f"   ❌ ИИ не ответил: {e}")
            problems.append(f"ИИ: {e}")

    # 3. FunPay
    print("\n[3/5] FunPay")
    net_session, net_ok = setup_network()
    if not net_ok:
        print("   ❌ Связи с FunPay нет")
        problems.append("FunPay недоступен")
    else:
        session = get_session(network_session=net_session)
        if session:
            print("   ✅ Авторизация по GOLDEN_KEY прошла")
        else:
            print("   ❌ Авторизация не прошла — проверь GOLDEN_KEY")
            problems.append("Авторизация FunPay")

    # 4. Google Drive
    print("\n[4/5] Google Drive")
    ok, message = gdrive.self_test()
    print(f"   {'✅' if ok else '❌'} {message}")
    if not ok:
        problems.append(f"Google Drive: {message}")

    # 5. Расход ИИ за сутки
    print("\n[5/5] Расход ИИ за сегодня")
    count, tokens = ai_client.used_today()
    if count:
        print(f"   • запросов: {count}, токенов (по данным Worker'а): {tokens}")
    else:
        print("   • запросов ещё не было")

    print("\n" + "=" * 60)
    if problems:
        print("ИТОГ: есть проблемы —")
        for p in problems:
            print(f"  ❌ {p}")
    else:
        print("ИТОГ: всё готово ✅ можно запускать: py main.py")
    print("=" * 60)
    return not problems


def main():
    print("=== FUNPAY AUTO-BOT v19.0 (без Запрета, ИИ через Cloudflare Worker) ===\n")

    net_session, net_ok = setup_network()
    if not net_ok:
        print("   [!] Связи нет, но продолжаем: попробуем восстановить по ходу.")
        print("   [!] Подсказка: py main.py --selftest покажет полную картину.")

    while True:
        try:
            session = get_session(network_session=net_session)
            if not session:
                session, net_session = _reconnect(net_session)
                if not session:
                    print("   [!] Авторизоваться не вышло. Ждём 5 минут...")
                    time.sleep(300)
                    continue

            games = load_games_list()
            if not games:
                print("🛌 Список игр пуст. Ожидание...")
                time.sleep(3600)
                continue

            session, net_session = process_single_game(session, games[0], net_session)
            time.sleep(5)

        except KeyboardInterrupt:
            print("\n⏹ Остановлено пользователем.")
            break
        except ai_client.AILimitReached as e:
            print(f"   [AI] ⚠️ Лимит ИИ исчерпан: {e}")
            print("   [AI]    Ждём 30 минут и продолжаем с этого же места.")
            time.sleep(1800)
        except Exception as e:
            print(f"💥 Ошибка цикла: {e}")
            logger.log_error(f"Ошибка главного цикла: {e}")
            time.sleep(20)


if __name__ == "__main__":
    if "--selftest" in sys.argv or "--check" in sys.argv:
        sys.exit(0 if run_selftest() else 1)
    main()
