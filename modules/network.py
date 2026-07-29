"""
modules/network.py — Маршрутизация FunPay-трафика в обход VPN.

Проблема: глобальный VPN перехватывает весь трафик, но Cloudflare на FunPay
обрывает VPN-соединения (SSL: UNEXPECTED_EOF_WHILE_READING, ConnectTimeout).

Решение (автоматическое, по порядку):
  1. DirectSession — HTTPAdapter с source_address привязкой к локальному IP
     (ядро маршрутизирует пакеты с этого IP через реальный интерфейс).
  2. Системные маршруты — ip-route / route для IP FunPay через шлюз
     локального провайдера (требует root/admin, но 100% надёжно).
  3. Пользователь настраивает split-tunneling VPN вручную (см. README).
"""

import socket
import subprocess
import sys
import re
import requests

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
        result = subprocess.run(
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
            result = subprocess.run(
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
        result = subprocess.run(
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
    Требует root/admin привилегий.
    Возвращает True, если хотя бы один маршрут добавлен.
    """
    global _added_routes

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
                result = subprocess.run(
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
                result = subprocess.run(
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
        subprocess.run(
            ["ip", "rule", "add", "from", source_ip, "table", table_id],
            capture_output=True, text=True, timeout=10
        )
        subprocess.run(
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
                subprocess.run(
                    ["route", "delete", fp_ip],
                    capture_output=True, text=True, timeout=10
                )
            else:
                subprocess.run(
                    ["ip", "route", "del", f"{fp_ip}/32"],
                    capture_output=True, text=True, timeout=10
                )
        except Exception:
            pass
    print(f"   [Сеть] 🧹 Удалено {len(_added_routes)} маршрутов FunPay.")
    _added_routes.clear()


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
        print(f"   [Сеть] ❌ FunPay недоступен: {e}")
        return False


def setup_network():
    """
    Полная настройка сети для работы с FunPay.
    Вызывать один раз при старте бота.
    Возвращает (session, success).
    """
    print("   [Сеть] Настройка маршрутизации FunPay...")

    session = get_direct_session()

    if test_funpay_connectivity(session):
        return session, True

    print("   [Сеть] Попытка добавить системные маршруты...")
    if add_funpay_routes():
        if test_funpay_connectivity(session):
            return session, True

    print("   [Сеть] ❌ Не удалось обеспечить прямой доступ к FunPay.")
    print("   [Сеть]   Решения:")
    print("   [Сеть]   1. Настройте split-tunneling VPN (исключите funpay.com)")
    print("   [Сеть]   2. Запустите бота от имени root/admin для маршрутов")
    return session, False
