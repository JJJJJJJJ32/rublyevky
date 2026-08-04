"""
modules/network.py — Маршрутизация FunPay-трафика + авто-починка WARP.

Проблема: глобальный VPN перехватывает весь трафик, но Cloudflare на FunPay
обрывает VPN-соединения (SSL: UNEXPECTED_EOF_WHILE_READING, ConnectTimeout).

Решение (автоматическое, по порядку):
  1. Если WARP активен — используем обычную сессию (WARP пропускает FunPay)
  2. DirectSession — HTTPAdapter с source_address привязкой к локальному IP
  3. Системные маршруты — ip-route / route для IP FunPay через шлюз
     (только если WARP НЕ активен, иначе маршруты конфликтуют)

Авто-починка при обрывах:
  - heal_network() — чинит WARP, удаляет старые маршруты, пересоздаёт сессию
  - cleanup_all_funpay_routes() — удаляет ВСЕ маршруты к FunPay IP из таблицы
  - warp_cli_reconnect() — перезапускает WARP при обрыве
"""

import socket
import subprocess
import sys
import re
import requests
import os
import time


# ─── Helper: subprocess.run с правильной кодировкой на русской Windows ──
def _run(cmd, timeout=10, **kwargs):
    """subprocess.run с правильной обработкой кодировки на русской Windows.
    На Windows системные команды (route, ipconfig, tasklist) выдают текст
    в OEM-кодировке (cp866), а не в системной cp1251. Без явного указания
    encoding Python пытается декодировать в cp1251 и получает UnicodeDecodeError.
    """
    # На Windows используем oem/utf-8 с заменой ошибок, на Linux — utf-8
    encoding = 'oem' if sys.platform == 'win32' else 'utf-8'
    return subprocess.run(
        cmd, capture_output=True, text=True,
        encoding=encoding, errors='replace',
        timeout=timeout, **kwargs
    )

# ─── Домены FunPay для маршрутизации ───────────────────────────────────
FUNPAY_DOMAINS = [
    "funpay.com",
    "cdn.funpay.com",
    "api.funpay.com",
]

# Кэш найденного локального IP/шлюза
_local_ip_cache = None
_local_gw_cache = None
_local_iface_cache = None
_added_routes = []
_warp_detected = False


# ═══════════════════════════════════════════════════════════════════════
# 0. Определение и управление WARP
# ═══════════════════════════════════════════════════════════════════════

def is_warp_active():
    """Проверяет, запущен ли Cloudflare WARP (процесс)."""
    global _warp_detected
    if _warp_detected:
        return True
    
    if sys.platform == "win32":
        try:
            result = _run(
                ["tasklist", "/FI", "IMAGENAME eq WarpClient.exe"],
                timeout=5
            )
            if "WarpClient" in result.stdout:
                _warp_detected = True
                return True
            # Альтернативное имя процесса
            result2 = _run(
                ["tasklist", "/FI", "IMAGENAME eq Cloudflare WARP.exe"],
                timeout=5
            )
            if "Cloudflare" in result2.stdout:
                _warp_detected = True
                return True
        except:
            pass
    else:
        try:
            result = _run(
                ["pgrep", "-f", "warp-svc"],
                timeout=5
            )
            if result.returncode == 0:
                _warp_detected = True
                return True
        except:
            pass
    
    return False


def warp_cli_status():
    """Проверяет статус WARP через warp-cli. Возвращает True если подключён."""
    if sys.platform != "win32":
        return False
    try:
        result = _run(
            ["warp-cli", "status"],
            timeout=10
        )
        output = result.stdout.lower()
        # "Status: Connected" или "Status: Connect" (в процессе)
        if "connected" in output:
            return True
        # "Status: Registration Missing" — нужно перерегистрировать
        if "registration missing" in output or "registration missing" in output:
            return False
        return False
    except Exception:
        return False


def warp_cli_reconnect():
    """Перезапускает WARP: disconnect → connect. Возвращает True если удалось."""
    if sys.platform != "win32":
        return False
    
    print("   [WARP] 🔄 Перезапуск WARP...")
    
    try:
        # Шаг 1: Отключаем
        _run(["warp-cli", "disconnect"], timeout=10)
        time.sleep(3)
        
        # Шаг 2: Подключаем
        result = _run(["warp-cli", "connect"], timeout=15)
        time.sleep(5)
        
        # Шаг 3: Проверяем статус
        if warp_cli_status():
            print("   [WARP] ✅ WARP переподключён успешно!")
            return True
        else:
            print("   [WARP] ⚠️ WARP не подключился. Пробуем полную перерегистрацию...")
            return _warp_full_reregister()
    except Exception as e:
        print(f"   [WARP] ❌ Ошибка перезапуска: {e}")
        return False


def _warp_full_reregister():
    """Полная перерегистрация WARP: delete → new → MASQUE → connect."""
    print("   [WARP] 🔧 Полная перерегистрация WARP...")
    try:
        _run(["warp-cli", "registration", "delete"], timeout=10)
        time.sleep(2)
        _run(["warp-cli", "registration", "new"], timeout=10)
        time.sleep(2)
        _run(["warp-cli", "tunnel", "protocol", "set", "MASQUE"], timeout=10)
        time.sleep(1)
        _run(["warp-cli", "tunnel", "masque-options", "set", "h2-only"], timeout=10)
        time.sleep(2)
        _run(["warp-cli", "connect"], timeout=15)
        time.sleep(5)
        
        if warp_cli_status():
            print("   [WARP] ✅ Перерегистрация успешна! WARP подключён.")
            return True
        else:
            print("   [WARP] ❌ Перерегистрация не помогла. WARP не подключается.")
            return False
    except Exception as e:
        print(f"   [WARP] ❌ Ошибка перерегистрации: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════
# 1. Определение локального (не-VPN) интерфейса
# ═══════════════════════════════════════════════════════════════════════

def _is_vpn_interface(name):
    """Возвращает True, если имя интерфейса похоже на VPN-туннель."""
    vpn_prefixes = ("tun", "tap", "wg", "utun", "ppp", "wgp", "nordvpn",
                    "mullvad", "proton", "openvpn", "ovpn", "ipsec",
                    "l2tp", "sstp", "vlan", "virbr", "docker", "veth",
                    "br-", "vnic", "hyper-v")
    return name.lower().startswith(vpn_prefixes)


def detect_local_interface():
    """
    Определяет IP-адрес, шлюз и имя интерфейса локальной сети (не VPN).
    Возвращает (ip, gateway, interface) или (None, None, None).
    Результат кэшируется.
    """
    global _local_ip_cache, _local_gw_cache, _local_iface_cache
    if _local_ip_cache:
        return _local_ip_cache, _local_gw_cache, _local_iface_cache

    if sys.platform == "win32":
        _detect_windows()
    else:
        _detect_linux()

    return _local_ip_cache, _local_gw_cache, _local_iface_cache


def _detect_linux():
    global _local_ip_cache, _local_gw_cache, _local_iface_cache

    try:
        result = _run(
            ["ip", "route", "show", "default"],
            timeout=10
        )
        # Сначала ищем маршрут через НЕ-VPN интерфейс
        for line in result.stdout.strip().splitlines():
            m = re.match(r'default\s+via\s+(\S+)\s+dev\s+(\S+)', line)
            if m:
                gw, iface = m.group(1), m.group(2)
                if not _is_vpn_interface(iface):
                    _local_gw_cache = gw
                    _local_iface_cache = iface
                    break

        # Если все маршруты через VPN — берём первый
        if not _local_gw_cache:
            for line in result.stdout.strip().splitlines():
                m = re.match(r'default\s+via\s+(\S+)\s+dev\s+(\S+)', line)
                if m:
                    _local_gw_cache = m.group(1)
                    _local_iface_cache = m.group(2)
                    break

        # IP-адрес интерфейса
        if _local_iface_cache:
            result = _run(
                ["ip", "addr", "show", "dev", _local_iface_cache],
                timeout=10
            )
            for line in result.stdout.strip().splitlines():
                m = re.match(r'\s+inet\s+(\d+\.\d+\.\d+\.\d+)', line)
                if m:
                    _local_ip_cache = m.group(1)
                    break

    except Exception as e:
        print(f"   [Сеть] Ошибка определения интерфейса (Linux): {e}")

def _detect_windows():
    global _local_ip_cache, _local_gw_cache, _local_iface_cache

    try:
        result = _run(
            ["ipconfig", "/all"],
            timeout=10
        )
        lines = result.stdout.strip().splitlines()

        current_iface = None
        current_ip = None
        current_gw = None
        is_vpn = False

        for line in lines:
            line_stripped = line.strip()

            # Новая секция адаптера
            if any(line_stripped.startswith(p) for p in
                   ("Ethernet adapter", "Wireless LAN adapter",
                    "Адаптер Ethernet", "Адаптер беспроводной сети")):
                # Сохраняем предыдущий не-VPN адаптер
                if current_iface and not is_vpn and current_ip and current_gw:
                    if not _local_ip_cache:
                        _local_ip_cache = current_ip
                        _local_gw_cache = current_gw
                        _local_iface_cache = current_iface

                current_iface = line_stripped
                current_ip = None
                current_gw = None
                is_vpn = False

            # Определяем VPN-адаптеры
            if any(kw in line_stripped.lower() for kw in
                   ("tunnel", "vpn", "wireguard", "tap", "wintun", "openvpn")):
                is_vpn = True

            # IP-адрес
            ip_match = re.match(r'IPv4 Address[\. ]+:\s+(\d+\.\d+\.\d+\.\d+)', line_stripped)
            if not ip_match:
                ip_match = re.match(r'Адрес IPv4[\. ]+:\s+(\d+\.\d+\.\d+\.\d+)', line_stripped)
            if ip_match:
                current_ip = ip_match.group(1)

            # Шлюз
            gw_match = re.match(r'Default Gateway[\. ]+:\s+(\d+\.\d+\.\d+\.\d+)', line_stripped)
            if not gw_match:
                gw_match = re.match(r'Основной шлюз[\. ]+:\s+(\d+\.\d+\.\d+\.\d+)', line_stripped)
            if gw_match:
                current_gw = gw_match.group(1)

        # Последний адаптер
        if current_iface and not is_vpn and current_ip and current_gw:
            if not _local_ip_cache:
                _local_ip_cache = current_ip
                _local_gw_cache = current_gw
                _local_iface_cache = current_iface

    except Exception as e:
        print(f"   [Сеть] Ошибка определения интерфейса (Windows): {e}")


# ═══════════════════════════════════════════════════════════════════════
# 2. Разрешение DNS-имён FunPay в IP-адреса
# ═══════════════════════════════════════════════════════════════════════

def resolve_domain(domain):
    """Разрешает домен в список IP-адресов."""
    try:
        return list(set(
            addr[4][0]
            for addr in socket.getaddrinfo(domain, 443, socket.AF_INET)
        ))
    except Exception:
        return []

def resolve_all_funpay_ips():
    """Разрешает все домены FunPay в IP-адреса."""
    ips = set()
    for domain in FUNPAY_DOMAINS:
        ips.update(resolve_domain(domain))
    return list(ips)


# ═══════════════════════════════════════════════════════════════════════
# 3. Управление системными маршрутами
# ═══════════════════════════════════════════════════════════════════════

def add_funpay_routes():
    """
    Добавляет системные маршруты для IP-адресов FunPay
    через локальный шлюз (минуя VPN).
    НЕ добавляет маршруты если WARP активен (конфликт!).
    Требует root/admin привилегий.
    Возвращает True, если хотя бы один маршрут добавлен.
    """
    global _added_routes

    # WARP активен — маршруты НЕ добавляем, они конфликтуют!
    if is_warp_active():
        print("   [Сеть] ☁️ WARP активен — маршруты НЕ добавляем (конфликтуют с WARP)")
        return False

    ip, gw, iface = detect_local_interface()
    if not gw:
        print("   [Сеть] Не удалось определить локальный шлюз. Маршруты не добавлены.")
        return False

    funpay_ips = resolve_all_funpay_ips()
    if not funpay_ips:
        print("   [Сеть] Не удалось разрешить домены FunPay.")
        return False

    added = 0
    for fp_ip in funpay_ips:
        try:
            if sys.platform == "win32":
                result = _run(
                    ["route", "add", fp_ip, "mask", "255.255.255.255", gw],
                    timeout=10
                )
                if result.returncode == 0 or "уже существует" in result.stdout or "already exists" in result.stdout.lower():
                    _added_routes.append(fp_ip)
                    added += 1
            else:
                cmd = ["ip", "route", "add", f"{fp_ip}/32", "via", gw]
                if iface:
                    cmd += ["dev", iface]
                result = _run(
                    cmd, timeout=10
                )
                if result.returncode == 0 or "File exists" in result.stderr:
                    _added_routes.append(fp_ip)
                    added += 1
        except Exception:
            pass

    if added > 0:
        print(f"   [Сеть] ✅ Добавлено {added} маршрутов к FunPay через шлюз {gw}")
        # На Linux: добавляем policy routing для source_address
        if sys.platform != "win32" and ip and iface:
            _add_policy_route(ip, gw, iface)
    else:
        print("   [Сеть] ⚠️ Не удалось добавить маршруты. Запустите от имени root/admin.")

    return added > 0


def _add_policy_route(source_ip, gateway, interface):
    """На Linux добавляет policy routing для исходящего IP."""
    try:
        table_id = "100"
        _run(
            ["ip", "rule", "add", "from", source_ip, "table", table_id],
            timeout=10
        )
        _run(
            ["ip", "route", "add", "default", "via", gateway, "dev", interface, "table", table_id],
            timeout=10
        )
        print(f"   [Сеть] ✅ Policy routing: from {source_ip} → {gateway} (table {table_id})")
    except Exception:
        pass

def remove_funpay_routes():
    """Удаляет добавленные маршруты (при завершении бота)."""
    if not _added_routes:
        return
    for fp_ip in _added_routes:
        try:
            if sys.platform == "win32":
                _run(
                    ["route", "delete", fp_ip],
                    timeout=10
                )
            else:
                _run(
                    ["ip", "route", "del", f"{fp_ip}/32"],
                    timeout=10
                )
        except Exception:
            pass
    print(f"   [Сеть] 🧹 Удалено {len(_added_routes)} маршрутов FunPay.")
    _added_routes.clear()


def cleanup_all_funpay_routes():
    """
    Удаляет ВСЕ маршруты к FunPay IP из таблицы маршрутизации.
    В отличие от remove_funpay_routes() — удаляет не только те, что бот добавил,
    но и ВСЕ маршруты к FunPay IP (включая от предыдущих запусков бота).
    Это нужно потому что старые маршруты конфликтуют с WARP.
    """
    print("   [Сеть] 🧹 Поиск и удаление ВСЕХ маршрутов к FunPay...")
    
    funpay_ips = resolve_all_funpay_ips()
    if not funpay_ips:
        # Если DNS не работает — пробуем известные IP FunPay
        funpay_ips = _get_known_funpay_ips()
    
    removed = 0
    if sys.platform == "win32":
        # На Windows: сканируем таблицу маршрутов и удаляем все к FunPay IP
        try:
            result = _run(
                ["route", "print"],
                timeout=10
            )
            for line in result.stdout.splitlines():
                # Ищем строки вида: 5.254.85.152  255.255.255.255  192.168.0.1  ...
                for fp_ip in funpay_ips:
                    if fp_ip in line:
                        try:
                            _run(
                                ["route", "delete", fp_ip],
                                timeout=10
                            )
                            removed += 1
                            print(f"   [Сеть] 🗑️ Удалён маршрут: {fp_ip}")
                        except:
                            pass
        except Exception as e:
            print(f"   [Сеть] ⚠️ Не удалось прочитать таблицу маршрутов: {e}")
    else:
        for fp_ip in funpay_ips:
            try:
                result = _run(
                    ["ip", "route", "del", f"{fp_ip}/32"],
                    timeout=10
                )
                if result.returncode == 0:
                    removed += 1
                    print(f"   [Сеть] 🗑️ Удалён маршрут: {fp_ip}")
            except:
                pass
    
    # Также очищаем кэш добавленных маршрутов
    _added_routes.clear()
    
    if removed > 0:
        print(f"   [Сеть] 🧹 Удалено {removed} маршрутов к FunPay.")
    else:
        print("   [Сеть] 🧹 Конфликтующих маршрутов не найдено.")


def _get_known_funpay_ips():
    """Возвращает известные IP-адреса FunPay (хардкод на случай если DNS не работает)."""
    return [
        "5.254.85.152",
        "185.104.210.59",
        "185.104.210.60",
        "185.104.210.61",
        "5.254.85.151",
        "5.254.85.153",
    ]


# ═══════════════════════════════════════════════════════════════════════
# 4. DirectSession — requests.Session с привязкой к локальному IP
# ═══════════════════════════════════════════════════════════════════════

class DirectHTTPAdapter(requests.adapters.HTTPAdapter):
    """
    HTTPAdapter, привязывающий исходящие соединения к конкретному локальному IP.
    """

    def __init__(self, source_ip=None, **kwargs):
        self.source_ip = source_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        if self.source_ip:
            kwargs['source_address'] = (self.source_ip, 0)
        return super().init_poolmanager(*args, **kwargs)


class DirectSession(requests.Session):
    """
    requests.Session, маршрутизирующий трафик через локальный интерфейс (минуя VPN).
    """

    def __init__(self, source_ip=None, **kwargs):
        super().__init__(**kwargs)
        if source_ip:
            adapter = DirectHTTPAdapter(source_ip=source_ip)
            self.mount("https://", adapter)
            self.mount("http://", adapter)


# ═══════════════════════════════════════════════════════════════════════
# 5. Публичный API
# ═══════════════════════════════════════════════════════════════════════

def get_direct_session():
    """
    Создаёт DirectSession с привязкой к локальному IP (если найден).
    """
    ip, gw, iface = detect_local_interface()

    if ip:
        print(f"   [Сеть] 🔌 Локальный IP: {ip} (шлюз: {gw}, интерфейс: {iface})")
        return DirectSession(source_ip=ip)
    else:
        print("   [Сеть] ⚠️ Локальный IP не найден. FunPay-трафик пойдёт через VPN.")
        return DirectSession(source_ip=None)


def test_funpay_connectivity(session):
    """Проверяет, доступен ли FunPay через данную сессию."""
    try:
        resp = session.get("https://funpay.com/", timeout=15)
        if resp.status_code == 200 and "funpay" in resp.text.lower():
            print("   [Сеть] ✅ FunPay доступен напрямую!")
            return True
        else:
            print(f"   [Сеть] ⚠️ FunPay вернул статус {resp.status_code}")
            return False
    except Exception as e:
        err_str = str(e)
        if "10013" in err_str:
            print(f"   [Сеть] ❌ WinError 10013 — WARP конфликтует с DirectSession")
        elif "timed out" in err_str.lower() or "ConnectTimeout" in err_str:
            print(f"   [Сеть] ❌ FunPay таймаут — WARP/маршруты не работают")
        elif "UNEXPECTED_EOF" in err_str:
            print(f"   [Сеть] ❌ SSL EOF — Cloudflare заблокировал IP")
        else:
            print(f"   [Сеть] ❌ FunPay недоступен: {e}")
        return False


def make_warp_session():
    """Создаёт новую обычную сессию для работы через WARP."""
    return requests.Session()


def heal_network():
    """
    Автоматическая починка сети при обрывах.
    1. Удаляет ВСЕ старые маршруты к FunPay (конфликтуют с WARP)
    2. Проверяет статус WARP
    3. Перезапускает WARP если не подключён
    4. Создаёт новую сессию и проверяет подключение
    Возвращает (session, success).
    """
    print("   [Сеть] 🔧 АВТО-ПОЧИНКА СЕТИ...")
    
    # Шаг 1: Удаляем ВСЕ маршруты к FunPay — они конфликтуют с WARP
    cleanup_all_funpay_routes()
    time.sleep(2)
    
    # Шаг 2: Проверяем WARP
    warp_running = is_warp_active()
    warp_connected = warp_cli_status()
    
    if warp_running and not warp_connected:
        print("   [Сеть] ⚠️ WARP запущен но НЕ подключён! Перезапускаем...")
        warp_cli_reconnect()
        time.sleep(5)
    elif not warp_running:
        print("   [Сеть] ⚠️ WARP не запущен! Пытаемся запустить...")
        # На Windows пробуем warp-cli
        if sys.platform == "win32":
            try:
                _run(["warp-cli", "connect"], timeout=15)
                time.sleep(5)
                if warp_cli_status():
                    print("   [Сеть] ✅ WARP запущен и подключён!")
                else:
                    print("   [Сеть] ❌ WARP не подключается. Запустите WARP вручную.")
            except:
                print("   [Сеть] ❌ warp-cli не найден. Запустите WARP вручную.")
        else:
            print("   [Сеть] ❌ Запустите WARP вручную.")
    
    # Шаг 3: Создаём новую сессию и проверяем
    new_session = make_warp_session()
    if test_funpay_connectivity(new_session):
        print("   [Сеть] ✅ Сеть починена! FunPay доступен.")
        return new_session, True
    
    # Шаг 4: Если не помогло — пробуем ещё раз переподключить WARP
    print("   [Сеть] ⚠️ Первая попытка не помогла. Пробуем переподключить WARP ещё раз...")
    if warp_cli_reconnect():
        time.sleep(5)
        new_session = make_warp_session()
        if test_funpay_connectivity(new_session):
            print("   [Сеть] ✅ Сеть починена после второго WARP-переподключения!")
            return new_session, True
    
    print("   [Сеть] ❌ Не удалось починить сеть автоматически.")
    print("   [Сеть]   Попробуйте вручную:")
    print("   [Сеть]   1. Откройте WARP → нажмите «Отключить» → «Подключить»")
    print("   [Сеть]   2. В командной строке: route delete 5.254.85.152")
    print("   [Сеть]   3. В командной строке: route delete 185.104.210.59")
    return new_session, False


def setup_network():
    """
    Полная настройка сети для работы с FunPay.
    Вызывать один раз при старте бота.
    Возвращает (session, success).
    """
    global _warp_detected
    print("   [Сеть] Настройка маршрутизации FunPay...")

    # Проверяем WARP первым делом
    warp_on = is_warp_active()
    if warp_on:
        print("   [Сеть] ☁️ Обнаружен Cloudflare WARP!")
        
        # Удаляем старые маршруты от предыдущих запусков (они конфликтуют с WARP!)
        cleanup_all_funpay_routes()
        
        # Проверяем что WARP реально подключён
        if not warp_cli_status():
            print("   [Сеть] ⚠️ WARP запущен но НЕ подключён. Переподключаем...")
            warp_cli_reconnect()
        
        print("   [Сеть] ⚠️ ВАЖНО: Zapret должен быть ЗАКРЫТ перед запуском бота!")
        print("   [Сеть]   (Zapret's WinDivert блокирует Python сокеты)")
        
        vpn_session = make_warp_session()
        if test_funpay_connectivity(vpn_session):
            print("   [Сеть] ✅ FunPay доступен через WARP. Используем эту сессию.")
            return vpn_session, True
        else:
            print("   [Сеть] ⚠️ WARP не отвечает. Пробуем авто-починку...")
            return heal_network()

    # 1. Пробуем напрямую (минуя VPN) — если FunPay не заблокирован
    direct_session = get_direct_session()
    if test_funpay_connectivity(direct_session):
        return direct_session, True

    # 2. Пробуем через обычную сессию — если WARP или другой VPN пропускает
    print("   [Сеть] Прямой доступ не работает. Пробуем через VPN/WARP...")
    vpn_session = requests.Session()
    if test_funpay_connectivity(vpn_session):
        print("   [Сеть] ✅ FunPay доступен через VPN/WARP. Используем эту сессию.")
        return vpn_session, True

    # 3. Пробуем системные маршруты (только если WARP НЕ активен!)
    print("   [Сеть] Попытка добавить системные маршруты...")
    if add_funpay_routes():
        if test_funpay_connectivity(direct_session):
            return direct_session, True

    print("   [Сеть] ❌ Не удалось подключиться к FunPay.")
    print("   [Сеть]   Решения:")
    print("   [Сеть]   1. Настройте Запрет + WARP (см. README)")
    print("   [Сеть]   2. Настройте split-tunneling VPN (исключите funpay.com)")
    return vpn_session, False
