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


# ─── Безопасный запуск системных команд ────────────────────────────────
def _run(cmd, timeout=10, **kwargs):
    """Запускает системную команду и безопасно читает её вывод на Windows.

    route, ipconfig и tasklist на русской Windows обычно используют OEM
    (cp866), а не UTF-8. errors='replace' не даёт проблемной кодировке
    сломать проверку сети.
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", timeout)
    kwargs.setdefault("encoding", "oem" if sys.platform == "win32" else "utf-8")
    kwargs.setdefault("errors", "replace")
    return subprocess.run(cmd, **kwargs)


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
_zapret_path = None  # Путь к Zapret (авто-определяется)


# ═══════════════════════════════════════════════════════════════════════
# 0a. Определение и управление Zapret
# ═══════════════════════════════════════════════════════════════════════

# Имена exe-файлов Zapret (проверяем процесс)
_ZAPRET_PROCESS_NAMES = ["winws.exe", "zapret.exe"]


def is_zapret_active():
    """Проверяет, запущен ли Zapret (winws.exe или zapret.exe).

    Состояние не кэшируем: Zapret может быть остановлен после предыдущей
    проверки, и бот должен это заметить.
    """
    if sys.platform != "win32":
        return False

    for process_name in _ZAPRET_PROCESS_NAMES:
        try:
            result = _run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}"],
                timeout=5,
            )
            if process_name.lower() in result.stdout.lower():
                return True
        except (OSError, subprocess.SubprocessError):
            continue

    return False


def _find_zapret_dir():
    """Ищет папку Zapret на диске. Имя может быть ЛЮБЫМ — главное чтобы внутри были признаки Zapret.
    Ищет в: папка проекта, Desktop, C:/, D:/, домашняя папка.
    Возвращает путь или None."""
    global _zapret_path
    if _zapret_path and os.path.isdir(_zapret_path):
        return _zapret_path

    # Сначала проверяем прямые пути (точное имя zapret)
    direct_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "zapret"),
        os.path.join(os.path.expanduser("~"), "Desktop", "zapret"),
        r"C:\zapret",
        r"D:\zapret",
        os.path.join(os.path.expanduser("~"), "zapret"),
    ]

    for path in direct_paths:
        if os.path.isdir(path) and _is_zapret_folder(path):
            _zapret_path = path
            return path

    # Потом ищем ЛЮБУЮ папку содержащую "zapret" в имени
    search_roots = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(os.path.expanduser("~"), "Desktop"),
        "C:\\",
        "D:\\",
        os.path.expanduser("~"),
    ]

    for root in search_roots:
        try:
            for name in os.listdir(root):
                full = os.path.join(root, name)
                if os.path.isdir(full) and "zapret" in name.lower():
                    if _is_zapret_folder(full):
                        _zapret_path = full
                        return full
        except (PermissionError, FileNotFoundError):
            pass

    return None


def _is_zapret_folder(path):
    """Проверяет что папка действительно Zapret — по ключевым признакам."""
    # Стандартный Zapret: service_install, general, bin/winws.exe
    if os.path.exists(os.path.join(path, "service_install")):
        return True
    if os.path.exists(os.path.join(path, "service")):
        return True
    if os.path.exists(os.path.join(path, "bin", "winws.exe")):
        return True
    # Модифицированный Zapret: config_by_user.bat, winws.exe в корне
    if os.path.exists(os.path.join(path, "winws.exe")):
        return True
    if os.path.exists(os.path.join(path, "config_by_user.bat")):
        return True
    if os.path.exists(os.path.join(path, "zapret.bat")):
        return True
    return False


def start_zapret():
    """Запускает Zapret если он не запущен. Возвращает True если успешно."""
    if is_zapret_active():
        print("   [Zapret] ✅ Zapret уже запущен!")
        return True

    if sys.platform != "win32":
        print("   [Zapret] ⚠️ Zapret доступен только на Windows.")
        return False

    zapret_dir = _find_zapret_dir()
    if not zapret_dir:
        print("   [Zapret] ⚠️ Папка Zapret не найдена на диске.")
        print("   [Zapret]   Искали в: Desktop/zapret, C:/zapret, D:/zapret")
        print("   [Zapret]   Скачайте Zapret и положите в одну из этих папок.")
        return False

    print(f"   [Zapret] 📂 Найден: {zapret_dir}")

    import subprocess as sp
    DETACHED = sp.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS

    # Способ 1: service_install (стандартный Zapret — устанавливает как службу Windows)
    service_install = os.path.join(zapret_dir, "service_install")
    if os.path.exists(service_install):
        try:
            print("   [Zapret] 🚀 Запускаем через service_install (служба Windows)...")
            sp.Popen(
                ["cmd", "/c", service_install],
                cwd=zapret_dir,
                creationflags=DETACHED,
                stdout=sp.DEVNULL, stderr=sp.DEVNULL
            )
            time.sleep(5)
            if is_zapret_active():
                print("   [Zapret] ✅ Zapret запущен как служба!")
                return True
            else:
                print("   [Zapret] ⚠️ service_install не помог. Пробуем дальше...")
        except Exception as e:
            print(f"   [Zapret] ⚠️ service_install ошибка: {e}")

    # Способ 2: config_by_user.bat (модифицированный Zapret)
    config_bat = os.path.join(zapret_dir, "config_by_user.bat")
    if os.path.exists(config_bat):
        try:
            print("   [Zapret] 🚀 Запускаем через config_by_user.bat...")
            sp.Popen(
                ["cmd", "/c", config_bat],
                cwd=zapret_dir,
                creationflags=DETACHED,
                stdout=sp.DEVNULL, stderr=sp.DEVNULL
            )
            time.sleep(3)
            if is_zapret_active():
                print("   [Zapret] ✅ Zapret запущен через config_by_user.bat!")
                return True
        except:
            pass

    # Способ 3: winws.exe напрямую (нужен профиль — берём general)
    winws = os.path.join(zapret_dir, "bin", "winws.exe")
    if not os.path.exists(winws):
        winws = os.path.join(zapret_dir, "winws.exe")
    general = os.path.join(zapret_dir, "general")
    if os.path.exists(winws) and os.path.exists(general):
        try:
            print("   [Zapret] 🚀 Запускаем winws.exe --profile general...")
            sp.Popen(
                [winws, "--profile", general],
                cwd=zapret_dir,
                creationflags=DETACHED,
                stdout=sp.DEVNULL, stderr=sp.DEVNULL
            )
            time.sleep(3)
            if is_zapret_active():
                print("   [Zapret] ✅ Zapret запущен через winws.exe!")
                return True
        except:
            pass

    # Способ 4: zapret.bat (запасной)
    zapret_bat = os.path.join(zapret_dir, "zapret.bat")
    if os.path.exists(zapret_bat):
        try:
            print("   [Zapret] 🚀 Запускаем через zapret.bat...")
            sp.Popen(
                ["cmd", "/c", zapret_bat],
                cwd=zapret_dir,
                creationflags=DETACHED,
                stdout=sp.DEVNULL, stderr=sp.DEVNULL
            )
            time.sleep(3)
            if is_zapret_active():
                print("   [Zapret] ✅ Zapret запущен через zapret.bat!")
                return True
        except:
            pass

    print("   [Zapret] ❌ Не удалось запустить Zapret автоматически.")
    print("   [Zapret]   Нужны права АДМИНИСТРАТОРА! Запустите бота от имени админа.")
    print("   [Zapret]   Или запустите Zapret вручную: service_install или config_by_user.bat")
    return False


# ═══════════════════════════════════════════════════════════════════════
# 0. Определение и управление WARP
# ═══════════════════════════════════════════════════════════════════════

def is_warp_active():
    """Проверяет, запущен ли процесс Cloudflare WARP.

    Проверяем каждый раз, чтобы после отключения WARP не оставалось старого
    значения из кэша.
    """
    if sys.platform == "win32":
        for process_name in ("WarpClient.exe", "Cloudflare WARP.exe"):
            try:
                result = _run(
                    ["tasklist", "/FI", f"IMAGENAME eq {process_name}"],
                    timeout=5,
                )
                if process_name.lower() in result.stdout.lower():
                    return True
            except (OSError, subprocess.SubprocessError):
                continue
    else:
        try:
            result = _run(["pgrep", "-f", "warp-svc"], timeout=5)
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass

    return False


def warp_cli_status():
    """Проверяет статус WARP через warp-cli.

    Нельзя просто проверять ``"connected" in output``: слово
    ``disconnected`` тоже содержит эту последовательность.
    """
    if sys.platform != "win32":
        return False
    try:
        result = _run(["warp-cli", "status"], timeout=10)
        output = result.stdout.lower()
        status_lines = [line for line in output.splitlines() if "status" in line]
        status = " ".join(status_lines) if status_lines else output
        return bool(re.search(r"\bconnected\b", status)) and not bool(
            re.search(r"\bdisconnected\b", status)
        )
    except (OSError, subprocess.SubprocessError):
        return False


def warp_cli_reconnect():
    """Перезапускает WARP: disconnect → connect. Возвращает True если удалось."""
    if sys.platform != "win32":
        return False
    
    print("   [WARP] 🔄 Перезапуск WARP...")
    
    try:
        # Шаг 1: Отключаем
        _run(["warp-cli", "disconnect"], capture_output=True, text=True, timeout=10)
        time.sleep(3)
        
        # Шаг 2: Подключаем
        _run(["warp-cli", "connect"], capture_output=True, text=True, timeout=15)
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
        _run(["warp-cli", "registration", "delete"], capture_output=True, text=True, timeout=10)
        time.sleep(2)
        _run(["warp-cli", "registration", "new"], capture_output=True, text=True, timeout=10)
        time.sleep(2)
        _run(["warp-cli", "tunnel", "protocol", "set", "MASQUE"], capture_output=True, text=True, timeout=10)
        time.sleep(1)
        _run(["warp-cli", "tunnel", "masque-options", "set", "h2-only"], capture_output=True, text=True, timeout=10)
        time.sleep(2)
        _run(["warp-cli", "connect"], capture_output=True, text=True, timeout=15)
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
            capture_output=True, text=True, timeout=10
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
                capture_output=True, text=True, timeout=10
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
            capture_output=True, text=True, timeout=10
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
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0 or "уже существует" in result.stdout or "already exists" in result.stdout.lower():
                    _added_routes.append(fp_ip)
                    added += 1
            else:
                cmd = ["ip", "route", "add", f"{fp_ip}/32", "via", gw]
                if iface:
                    cmd += ["dev", iface]
                result = _run(
                    cmd, capture_output=True, text=True, timeout=10
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
            capture_output=True, text=True, timeout=10
        )
        _run(
            ["ip", "route", "add", "default", "via", gateway, "dev", interface, "table", table_id],
            capture_output=True, text=True, timeout=10
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
                    capture_output=True, text=True, timeout=10
                )
            else:
                _run(
                    ["ip", "route", "del", f"{fp_ip}/32"],
                    capture_output=True, text=True, timeout=10
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
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.splitlines():
                # Ищем строки вида: 5.254.85.152  255.255.255.255  192.168.0.1  ...
                for fp_ip in funpay_ips:
                    if fp_ip in line:
                        try:
                            _run(
                                ["route", "delete", fp_ip],
                                capture_output=True, text=True, timeout=10
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
                    capture_output=True, text=True, timeout=10
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
            print("   [Сеть] ⚠️ WinError 10013 — WinDivert/Zapret конфликтует с DirectSession")
            print("   [Сеть]   Это нормально при работающем Zapret. Пробуем через обычную сессию...")
            # Fallback: обычная сессия без source_address (работает через WARP+Zapret)
            try:
                fallback = requests.Session()
                resp = fallback.get("https://funpay.com/", timeout=15)
                if resp.status_code == 200 and "funpay" in resp.text.lower():
                    print("   [Сеть] ✅ FunPay доступен через fallback-сессию!")
                    return True
            except:
                pass
        elif "timed out" in err_str.lower() or "ConnectTimeout" in err_str:
            print("   [Сеть] ❌ FunPay таймаут — WARP/маршруты не работают")
        elif "UNEXPECTED_EOF" in err_str:
            print("   [Сеть] ❌ SSL EOF — Cloudflare заблокировал IP")
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
    
    # Шаг 0: Убедимся что Zapret работает (нужен для WARP)
    if not is_zapret_active():
        print("   [Сеть] ⚠️ Zapret не запущен! Пробуем запустить...")
        start_zapret()
    
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
                _run(["warp-cli", "connect"], capture_output=True, text=True, timeout=15)
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
    print("   [Сеть]   1. Убедитесь что Zapret ЗАПУЩЕН (нужен для WARP!)")
    print("   [Сеть]   2. Откройте WARP → нажмите «Отключить» → «Подключить»")
    print("   [Сеть]   3. В командной строке (админ): route delete 5.254.85.152")
    print("   [Сеть]   4. В командной строке (админ): route delete 185.104.210.59")
    return new_session, False


def setup_network():
    """
    Полная настройка сети для работы с FunPay.
    Вызывать один раз при старте бота.
    Возвращает (session, success).
    """
    print("   [Сеть] Настройка маршрутизации FunPay...")

    # Проверяем WARP первым делом
    warp_on = is_warp_active()
    if warp_on:
        print("   [Сеть] ☁️ Обнаружен Cloudflare WARP!")
        
        # Zapret нужен для WARP — пробуем запустить если не работает
        if not is_zapret_active():
            print("   [Сеть] ⚠️ Zapret не запущен! Пробуем запустить автоматически...")
            start_zapret()
        else:
            print("   [Сеть] ✅ Zapret запущен — WARP будет стабильно работать!")
        
        # Удаляем старые маршруты от предыдущих запусков (они конфликтуют с WARP!)
        cleanup_all_funpay_routes()
        
        # Проверяем что WARP реально подключён
        if not warp_cli_status():
            print("   [Сеть] ⚠️ WARP запущен но НЕ подключён. Переподключаем...")
            warp_cli_reconnect()
        
        print("   [Сеть] ⚠️ ВАЖНО: Zapret нужен для стабильной работы WARP!")
        print("   [Сеть]   (Без Zapret WARP не может перезапуститься — РКН блокирует)")
        print("   [Сеть]   Бот попытался запустить Zapret автоматически.")
        
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
