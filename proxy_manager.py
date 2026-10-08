"""Quản lý Proxy cho MuseAI Studio — Tự động đồng bộ HomeProxy từ E:\\ThinAptm0707,
gán mỗi tài khoản 1 proxy cố định (Sticky Proxy), tự động kiểm tra và chỉ xoay proxy khi proxy die.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import socket
import threading
import time
import urllib.request
import requests

from config import CFG
from store import Store
store = Store(CFG)

log = logging.getLogger("proxy_manager")

THINAPTM_DIR = os.environ.get("THINAPTM_DIR", "").strip() or r"E:\ThinAptm0707"
PROXY_CACHE_FILE = os.path.join(CFG.data_dir, "proxy_pool.json")

_forwarders: dict[str, int] = {}  # proxy_str -> local_port
_forwarder_servers: list[socket.socket] = []
_forwarder_lock = threading.Lock()
_pool_lock = threading.Lock()


def parse_proxy_str(proxy_str: str) -> dict | None:
    """Tách chuỗi proxy (host:port:user:pass hoặc host:port) thành dict."""
    if not proxy_str:
        return None
    s = str(proxy_str).strip()
    if s.startswith("#") or len(s) < 4:
        return None
    # Xóa nhãn tag #VN, #US nếu có
    s = re.sub(r"#[A-Za-z0-9_-]+\s*$", "", s).strip()
    if s.startswith("http://") or s.startswith("https://"):
        s = s.split("://", 1)[1]
    if "@" in s:
        auth_part, host_part = s.split("@", 1)
        u_p = auth_part.split(":", 1)
        h_p = host_part.split(":", 1)
        return {
            "host": h_p[0],
            "port": int(h_p[1]) if len(h_p) > 1 else 80,
            "user": u_p[0] if len(u_p) > 0 else "",
            "pass": u_p[1] if len(u_p) > 1 else "",
            "raw": proxy_str
        }
    parts = s.split(":")
    if len(parts) >= 4:
        return {
            "host": parts[0],
            "port": int(parts[1]),
            "user": parts[2],
            "pass": ":".join(parts[3:]),
            "raw": proxy_str
        }
    if len(parts) >= 2:
        return {
            "host": parts[0],
            "port": int(parts[1]),
            "user": "",
            "pass": "",
            "raw": proxy_str
        }
    return None


def test_proxy(proxy_str: str, timeout: float = 6.0) -> tuple[bool, str]:
    """Kiểm tra proxy có hoạt động không bằng request ipify. Trả về (is_alive, ip_hoặc_lỗi)."""
    p = parse_proxy_str(proxy_str)
    if not p:
        return False, "Định dạng proxy không hợp lệ"
    if p["user"] and p["pass"]:
        proxy_url = f"http://{p['user']}:{p['pass']}@{p['host']}:{p['port']}"
    else:
        proxy_url = f"http://{p['host']}:{p['port']}"
    try:
        r = requests.get(
            "https://api.ipify.org?format=json",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=timeout
        )
        if r.status_code == 200:
            ip = r.json().get("ip", "")
            return True, ip or "Alive"
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)[:60]


def start_local_forwarder(proxy_str: str) -> int:
    """Khởi chạy một forwarder TCP siêu nhẹ trên 127.0.0.1 để nạp Basic Auth tự động cho Chrome CDP."""
    p = parse_proxy_str(proxy_str)
    if not p:
        raise ValueError(f"Proxy không hợp lệ: {proxy_str}")

    clean_key = f"{p['host']}:{p['port']}:{p['user']}:{p['pass']}"
    with _forwarder_lock:
        if clean_key in _forwarders:
            return _forwarders[clean_key]

        remote_host = p["host"]
        remote_port = p["port"]
        username = p["user"]
        password = p["pass"]
        auth_b64 = base64.b64encode(f"{username}:{password}".encode()).decode() if (username and password) else ""

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(100)
        local_port = server.getsockname()[1]

        def handle_client(client_sock):
            try:
                req = client_sock.recv(4096)
                if not req:
                    client_sock.close()
                    return
                first_line = req.split(b"\r\n")[0]
                words = first_line.split()
                if len(words) < 2:
                    client_sock.close()
                    return
                method = words[0].upper()
                target = words[1].decode("latin1")

                remote_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                remote_sock.settimeout(20)
                remote_sock.connect((remote_host, remote_port))

                if method == b"CONNECT":
                    connect_lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
                    if auth_b64:
                        connect_lines.append(f"Proxy-Authorization: Basic {auth_b64}")
                    connect_lines.append("Proxy-Connection: Keep-Alive")
                    raw_req = "\r\n".join(connect_lines) + "\r\n\r\n"
                    remote_sock.sendall(raw_req.encode("latin1"))

                    resp = remote_sock.recv(4096)
                    if b"200" not in resp.split(b"\r\n")[0]:
                        client_sock.close()
                        remote_sock.close()
                        return
                    client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                else:
                    headers_end = req.find(b"\r\n\r\n")
                    if headers_end != -1 and auth_b64:
                        auth_header = f"Proxy-Authorization: Basic {auth_b64}\r\n".encode("latin1")
                        new_req = req[:headers_end + 2] + auth_header + req[headers_end + 2:]
                        remote_sock.sendall(new_req)
                    else:
                        remote_sock.sendall(req)

                # QUAN TRỌNG: Gỡ bỏ timeout sau khi bắt tay thành công để không làm đứt kết nối streaming/video
                try:
                    client_sock.settimeout(None)
                    remote_sock.settimeout(None)
                    client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    remote_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except Exception:
                    pass

                def pipe(src, dst):
                    try:
                        while True:
                            data = src.recv(16384)
                            if not data:
                                break
                            dst.sendall(data)
                    except Exception:
                        pass
                    finally:
                        try: src.close()
                        except: pass
                        try: dst.close()
                        except: pass

                threading.Thread(target=pipe, args=(client_sock, remote_sock), daemon=True).start()
                threading.Thread(target=pipe, args=(remote_sock, client_sock), daemon=True).start()
            except Exception:
                try: client_sock.close()
                except: pass

        def listen_loop():
            while True:
                try:
                    sock, _ = server.accept()
                    threading.Thread(target=handle_client, args=(sock,), daemon=True).start()
                except Exception:
                    break

        threading.Thread(target=listen_loop, daemon=True).start()
        _forwarders[clean_key] = local_port
        _forwarder_servers.append(server)
        log.info("🔌 Khởi động Local Proxy Forwarder 127.0.0.1:%d -> %s:%d", local_port, remote_host, remote_port)
        return local_port


def stop_all_forwarders():
    """Đóng sạch sẽ toàn bộ local proxy forwarder sockets."""
    with _forwarder_lock:
        for s in _forwarder_servers:
            try:
                s.close()
            except Exception:
                pass
        _forwarder_servers.clear()
        _forwarders.clear()
        log.info("🔌 Đã đóng toàn bộ Local Proxy Forwarders.")


def load_proxies_from_thinaptm() -> list[str]:
    """Đọc danh sách proxy từ ThinAptm0707 (trên mọi ổ đĩa) hoặc file proxy.txt trong thư mục MuseAI."""
    proxies: list[str] = []
    token = ""

    # 1. Quét danh sách các thư mục ứng viên ThinAptm
    candidate_dirs = [THINAPTM_DIR]
    for d in ["E", "D", "C", "F"]:
        d_path = f"{d}:\\ThinAptm0707"
        if d_path not in candidate_dirs:
            candidate_dirs.append(d_path)

    for c_dir in candidate_dirs:
        if not os.path.isdir(c_dir):
            continue
        settings_file = os.path.join(c_dir, "settings.json")
        if os.path.isfile(settings_file):
            try:
                with open(settings_file, "r", encoding="utf-8") as f:
                    s = json.load(f)
                if not token:
                    token = s.get("homeproxy_token", "").strip()
                for p in s.get("proxy_list", []):
                    p_clean = str(p).strip()
                    if p_clean and p_clean not in proxies and not p_clean.startswith("#"):
                        proxies.append(p_clean)
            except Exception as e:
                log.warning("Lỗi đọc settings.json từ %s: %s", c_dir, e)

        # Đọc thêm từ các file proxy .txt trong thư mục ThinAptm
        for txt_name in ["proxy_us_tot_8.txt", "proxy.txt", "proxies.txt"]:
            us_file = os.path.join(c_dir, txt_name)
            if os.path.isfile(us_file):
                try:
                    with open(us_file, "r", encoding="utf-8") as f:
                        for line in f:
                            p = line.strip()
                            if p and not p.startswith("#") and p not in proxies:
                                proxies.append(p)
                except Exception:
                    pass

    # 2. Đọc thêm từ file proxy.txt hoặc proxies.txt ngay trong thư mục MuseAI (cực kỳ tiện khi mang sang máy khác)
    local_proxy_files = [
        os.path.join(CFG.base_dir, "proxy.txt"),
        os.path.join(CFG.base_dir, "proxies.txt"),
        os.path.join(CFG.base_dir, "proxy_pool.txt"),
        os.path.join(CFG.data_dir, "proxy.txt"),
        os.path.join(CFG.data_dir, "proxies.txt"),
    ]
    for lpf in local_proxy_files:
        if os.path.isfile(lpf):
            try:
                with open(lpf, "r", encoding="utf-8") as f:
                    for line in f:
                        p = line.strip()
                        if p and not p.startswith("#") and p not in proxies:
                            proxies.append(p)
                log.info("📄 Đã nạp proxy từ file cục bộ: %s (Hiện có %d proxy)", lpf, len(proxies))
            except Exception as e:
                log.warning("Lỗi đọc file proxy cục bộ %s: %s", lpf, e)

    # 3. Lấy token HomeProxy (từ .env hoặc từ settings.json của ThinAptm)
    if not token:
        token = os.environ.get("HOMEPROXY_TOKEN", "").strip()

    if token:
        hp_list = fetch_proxies_from_homeproxy_api(token)
        for p in hp_list:
            if p and p not in proxies:
                proxies.append(p)
        log.info("🌐 Tổng số proxy thu thập được sau khi gọi HomeProxy API: %d proxy", len(proxies))

    # Lưu cache vào data/proxy_pool.json
    save_proxy_pool(proxies)
    return proxies


def fetch_proxies_from_homeproxy_api(token: str) -> list[str]:
    """Gọi HomeProxy.vn API chuẩn theo thuật toán của ThinAptm (hỗ trợ x-merchant-id và fallback orders)."""
    if not token:
        return []
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    proxy_lines: list[str] = []

    # Bước 1: Lấy x-merchant-id từ /orders (HomeProxy yêu cầu header này để trả về danh sách proxy chính xác)
    merchant_id = ""
    for base in ["https://api.homeproxy.vn/api/v1", "https://app.homeproxy.vn/api/v2"]:
        try:
            r0 = requests.get(f"{base}/orders?page=1&limit=1", headers=headers, timeout=8)
            if r0.status_code == 200:
                orders_data = r0.json().get("data", [])
                if orders_data:
                    merchant_id = str(orders_data[0].get("user", {}).get("merchant", {}).get("id", ""))
                    if merchant_id:
                        break
        except Exception:
            pass

    if merchant_id:
        headers["x-merchant-id"] = merchant_id
        log.info("🌐 [HomeProxy] Đã xác thực merchant_id: %s", merchant_id)

    # Bước 2: Lấy danh sách proxy thực sự từ /users/proxies
    now_ms = int(time.time() * 1000)
    for base in ["https://api.homeproxy.vn/api/v1", "https://app.homeproxy.vn/api/v2"]:
        try:
            r = requests.get(f"{base}/users/proxies?page=1&limit=500", headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                for item in items:
                    status_name = item.get("status", {}).get("name", "")
                    expired_at = item.get("expiredAt", 0)
                    if status_name != "Completed":
                        continue
                    if expired_at and expired_at < now_ms:
                        continue
                    px = item.get("proxy", {})
                    ip_data = px.get("ipaddress", {}) if isinstance(px.get("ipaddress"), dict) else {}
                    host = ip_data.get("domain") or ip_data.get("ip") or px.get("domain") or px.get("ip") or px.get("host")
                    port = px.get("port")
                    user = px.get("username")
                    pwd = str(px.get("password") or "").strip()
                    if host and port:
                        try:
                            port_int = int(str(port).strip())
                            if 1 <= port_int <= 65535:
                                line = f"{str(host).strip()}:{port_int}"
                                if user:
                                    line += f":{str(user).strip()}:{pwd}"
                                if line not in proxy_lines:
                                    proxy_lines.append(line)
                        except Exception:
                            pass
                if proxy_lines:
                    log.info("🌐 [HomeProxy] Lấy thành công từ %s/users/proxies: %d proxy", base, len(proxy_lines))
                    break
        except Exception as e:
            log.warning("[HomeProxy] Lỗi gọi %s/users/proxies: %s", base, e)

    # Bước 3: Fallback lấy từ orders nếu users/proxies bị lỗi hoặc rỗng
    if not proxy_lines:
        for base in ["https://api.homeproxy.vn/api/v1", "https://app.homeproxy.vn/api/v2"]:
            try:
                r2 = requests.get(f"{base}/orders?page=1&limit=100", headers=headers, timeout=10)
                if r2.status_code == 200:
                    orders = r2.json().get("data", [])
                    for order in orders:
                        if order.get("status", {}).get("name") != "Completed":
                            continue
                        for prod in order.get("products", []):
                            host = prod.get("domain") or prod.get("ip") or ""
                            port = prod.get("port")
                            user = prod.get("user", "")
                            pwd = str(prod.get("password") or "").strip()
                            if host and port:
                                line = f"{host}:{port}:{user}:{pwd}" if user else f"{host}:{port}"
                                if line not in proxy_lines:
                                    proxy_lines.append(line)
                    if proxy_lines:
                        log.info("🌐 [HomeProxy] Fallback lấy thành công từ %s/orders: %d proxy", base, len(proxy_lines))
                        break
            except Exception as e:
                log.warning("[HomeProxy] Lỗi gọi %s/orders: %s", base, e)

    return proxy_lines


def get_cached_proxy_pool() -> list[str]:
    """Lấy danh sách proxy đã lưu trong cache."""
    if os.path.isfile(PROXY_CACHE_FILE):
        try:
            with open(PROXY_CACHE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d.get("proxies", [])
        except Exception:
            pass
    return []


def save_proxy_pool(proxies: list[str]):
    """Lưu danh sách proxy vào cache file."""
    os.makedirs(os.path.dirname(PROXY_CACHE_FILE), exist_ok=True)
    try:
        with open(PROXY_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"proxies": proxies, "updated_at": int(time.time())}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_account_proxy(acc_id: str) -> str | None:
    """Lấy proxy hiện tại của tài khoản."""
    acc = store.get_account(acc_id)
    if not acc:
        return None
    return acc.get("proxy")


def assign_sticky_proxy(acc_id: str, proxy_str: str, ip: str = ""):
    """Gán proxy cố định cho tài khoản."""
    store.update_account(
        acc_id,
        proxy=proxy_str,
        proxy_ip=ip,
        proxy_status="alive",
        proxy_assigned_at=int(time.time())
    )
    log.info("📌 Đã gán proxy cố định cho TK %s: %s (IP: %s)", acc_id, proxy_str, ip or "Chưa kiểm tra")


def ensure_alive_proxy_for_account(acc_id: str, force_check: bool = False) -> str | None:
    """Kiểm tra proxy của tài khoản. Nếu proxy còn sống, giữ nguyên.
    Nếu chưa có proxy hoặc proxy cũ đã chết (die), tự động chọn proxy mới còn sống từ Pool."""
    acc = store.get_account(acc_id)
    if not acc:
        return None

    cur_proxy = acc.get("proxy")
    if cur_proxy and not force_check:
        return cur_proxy

    if cur_proxy:
        ok, ip_or_err = test_proxy(cur_proxy)
        if ok:
            store.update_account(acc_id, proxy_status="alive", proxy_ip=ip_or_err)
            return cur_proxy
        else:
            log.warning("❌ Proxy của TK %s đã DIE (%s) -> Tiến hành đổi sang proxy mới!", acc.get("label", acc_id), ip_or_err)
            store.update_account(acc_id, proxy_status="dead", proxy_error=ip_or_err)

    # Cần cấp proxy mới từ Pool
    pool = get_cached_proxy_pool()
    if not pool:
        pool = load_proxies_from_thinaptm()

    if not pool:
        log.warning("Không có proxy nào khả dụng trong HomeProxy Pool!")
        return None

    # Tìm các proxy chưa được gán cho tài khoản nào khác
    assigned_proxies = {a.get("proxy") for a in store.list_accounts() if a.get("proxy") and a.get("id") != acc_id}
    available = [p for p in pool if p not in assigned_proxies and p != cur_proxy]
    if not available:
        available = [p for p in pool if p != cur_proxy] or pool

    # Xáo trộn và kiểm tra proxy sống
    random.shuffle(available)
    for candidate in available:
        ok, ip_or_err = test_proxy(candidate)
        if ok:
            assign_sticky_proxy(acc_id, candidate, ip=ip_or_err)
            return candidate

    log.error("Tất cả proxy trong Pool đều không phản hồi khi cấp cho TK %s!", acc_id)
    return None


def sync_all_accounts_with_homeproxy() -> dict:
    """Đồng bộ toàn bộ proxy từ E:\\ThinAptm0707 và gán sticky proxy cho tất cả tài khoản chưa có."""
    import concurrent.futures
    proxies = load_proxies_from_thinaptm()
    accounts = store.list_accounts()
    assigned_count = 0

    if not proxies:
        return {
            "success": False,
            "pool_size": 0,
            "total_proxies": 0,
            "alive_proxies": 0,
            "total_accounts": len(accounts),
            "assigned_count": 0,
            "newly_assigned": 0,
            "accounts": accounts,
            "message": "Không tìm thấy proxy nào trong ThinAptm0707 hoặc file proxy.txt trong thư mục MuseAI!"
        }

    # 1. Kiểm tra nhanh các proxy khả dụng bằng đa luồng (3.5s timeout)
    alive_proxies: list[tuple[str, str]] = []
    if proxies:
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            future_to_p = {executor.submit(test_proxy, p, 3.5): p for p in proxies}
            for future in concurrent.futures.as_completed(future_to_p):
                p = future_to_p[future]
                try:
                    ok, ip = future.result()
                    if ok:
                        alive_proxies.append((p, ip))
                except Exception:
                    pass

    # Nếu không test được mạng ngoài, dùng toàn bộ danh sách proxy làm fallback
    if not alive_proxies and proxies:
        alive_proxies = [(p, "") for p in proxies]

    assigned_proxies = {a.get("proxy") for a in accounts if a.get("proxy")}
    free_alive = [item for item in alive_proxies if item[0] not in assigned_proxies]
    if not free_alive:
        free_alive = list(alive_proxies)

    for i, a in enumerate(accounts):
        aid = a["id"]
        cur_p = a.get("proxy")
        if not cur_p:
            if free_alive:
                chosen, ip = free_alive[i % len(free_alive)]
                assign_sticky_proxy(aid, chosen, ip)
                assigned_count += 1

    return {
        "success": True,
        "pool_size": len(proxies),
        "total_proxies": len(proxies),
        "alive_proxies": len(alive_proxies),
        "total_accounts": len(accounts),
        "assigned_count": assigned_count,
        "newly_assigned": assigned_count,
        "accounts": store.list_accounts()
    }


def get_forwarder_url_for_account(acc_id: str) -> str | None:
    """Trả về URL http://127.0.0.1:<port> của local forwarder cho tài khoản, hoặc None."""
    try:
        p_str = ensure_alive_proxy_for_account(acc_id)
        if p_str:
            port = start_local_forwarder(p_str)
            return f"http://127.0.0.1:{port}"
    except Exception as e:
        log.warning("Không thể lấy forwarder url cho TK %s: %s", acc_id, e)
    return None

