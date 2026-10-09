"""Quản lý Proxy cho MuseAI Studio — Tự động đồng bộ HomeProxy từ E:\\ThinAptm0707,
gán mỗi tài khoản 1 proxy cố định (Sticky Proxy), tự động kiểm tra và chỉ xoay proxy khi proxy die.
"""
from __future__ import annotations

import atexit
import base64
import json
import logging
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import requests

from config import CFG
from store import Store
store = Store(CFG)

log = logging.getLogger("proxy_manager")

THINAPTM_DIR = os.environ.get("THINAPTM_DIR", "").strip() or r"E:\ThinAptm0707"
# Token HomeProxy.vn cố định cho hệ thống nội bộ
DEFAULT_HOMEPROXY_TOKEN = "homepx42152_e21c0b975d1093642c0a6e95bd760a49a4f7bf5a2e67ba971535c18940288b1e"
HOMEPROXY_TOKEN = os.environ.get("HOMEPROXY_TOKEN", "").strip() or DEFAULT_HOMEPROXY_TOKEN
PROXY_CACHE_FILE = os.path.join(CFG.data_dir, "proxy_pool.json")
PROXY_SETTINGS_FILE = os.path.join(CFG.data_dir, "proxy_settings.json")

_forwarders: dict[str, int] = {}  # proxy_str -> local_port
_forwarder_servers: list[socket.socket] = []
_forwarder_lock = threading.Lock()
_pool_lock = threading.Lock()
_proxy_enabled_mem: bool | None = None


def is_proxy_enabled() -> bool:
    """Kiểm tra xem hệ thống có đang bật fake IP / proxy hay không.
    Mặc định True, nhưng người dùng có thể tắt hoàn toàn để chạy mạng trực tiếp (Không fake IP)."""
    global _proxy_enabled_mem
    if _proxy_enabled_mem is not None:
        return _proxy_enabled_mem
    if os.path.isfile(PROXY_SETTINGS_FILE):
        try:
            with open(PROXY_SETTINGS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                _proxy_enabled_mem = bool(d.get("enabled", True))
                return _proxy_enabled_mem
        except Exception:
            pass
    _proxy_enabled_mem = True
    return _proxy_enabled_mem


def set_proxy_enabled(enabled: bool) -> bool:
    """Bật hoặc tắt toàn bộ việc sử dụng Proxy trên toàn hệ thống."""
    global _proxy_enabled_mem
    _proxy_enabled_mem = bool(enabled)
    os.makedirs(os.path.dirname(PROXY_SETTINGS_FILE), exist_ok=True)
    try:
        with open(PROXY_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"enabled": bool(enabled), "updated_at": int(time.time())}, f, indent=2)
        log.info("🌐 [Proxy Manager] Chế độ Proxy đã chuyển sang: %s", "BẬT (Fake IP)" if enabled else "TẮT (Chạy Trực Tiếp / Không Fake IP)")
        return True
    except Exception as e:
        log.warning("Lỗi lưu proxy settings: %s", e)
        return False


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


FORWARDER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy_forwarder.py")
_forwarder_procs: dict[str, subprocess.Popen] = {}


def start_local_forwarder(proxy_str: str) -> int:
    """Forwarder 127.0.0.1 tự chèn Basic Auth cho Chrome (Chrome không tự đăng nhập proxy user/pass).

    Mỗi proxy chạy 1 tiến trình asyncio riêng (proxy_forwarder.py): toàn bộ lưu lượng của các trình duyệt
    không còn đi qua luồng Python trong app (trước đây ~2 luồng/kết nối giành GIL làm cả app chậm).
    Không chạy được tiến trình riêng -> dùng forwarder luồng trong app như cũ."""
    p = parse_proxy_str(proxy_str)
    if not p:
        raise ValueError(f"Proxy không hợp lệ: {proxy_str}")

    clean_key = f"{p['host']}:{p['port']}:{p['user']}:{p['pass']}"
    with _forwarder_lock:
        proc = _forwarder_procs.get(clean_key)
        if clean_key in _forwarders and (proc is None or proc.poll() is None):
            return _forwarders[clean_key]
        if proc is not None:
            log.warning("🔌 Forwarder tiến trình của %s:%s đã dừng (mã %s) -> khởi động lại",
                        p["host"], p["port"], proc.poll())
            _forwarder_procs.pop(clean_key, None)
            _forwarders.pop(clean_key, None)
        try:
            local_port = _spawn_forwarder_process(clean_key, p)
            log.info("🔌 Khởi động Proxy Forwarder (tiến trình riêng) 127.0.0.1:%d -> %s:%d",
                     local_port, p["host"], p["port"])
            return local_port
        except Exception as exc:  # noqa: BLE001
            log.warning("Không chạy được forwarder tiến trình riêng (%s) -> dùng forwarder trong app", exc)
            return _start_inprocess_forwarder(clean_key, p)


def _spawn_forwarder_process(clean_key: str, p: dict) -> int:
    """Gọi trong _forwarder_lock. Trả về cổng local của tiến trình forwarder vừa chạy."""
    if not os.path.isfile(FORWARDER_SCRIPT):
        raise FileNotFoundError(FORWARDER_SCRIPT)
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    proc = subprocess.Popen([sys.executable, "-u", FORWARDER_SCRIPT], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags)
    try:
        cfg = {"host": p["host"], "port": int(p["port"]), "user": p["user"] or "", "pass": p["pass"] or ""}
        proc.stdin.write((json.dumps(cfg) + "\n").encode())
        proc.stdin.flush()
        result: list[bytes] = []
        reader = threading.Thread(target=lambda: result.append(proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(15)
        line = (result[0] if result else b"").decode(errors="replace").strip()
        if not line.startswith("PORT "):
            raise RuntimeError(f"forwarder không báo cổng (nhận: {line!r})")
        local_port = int(line.split()[1])
    except BaseException:
        proc.kill()
        raise
    _forwarder_procs[clean_key] = proc
    _forwarders[clean_key] = local_port
    return local_port


def _start_inprocess_forwarder(clean_key: str, p: dict) -> int:
    """Forwarder dự phòng bằng luồng Python trong app (cách cũ). Gọi trong _forwarder_lock."""
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
        for proc in _forwarder_procs.values():
            try:
                proc.kill()
            except Exception:
                pass
        _forwarder_servers.clear()
        _forwarder_procs.clear()
        _forwarders.clear()
        log.info("🔌 Đã đóng toàn bộ Local Proxy Forwarders.")


def _kill_forwarder_procs():
    for proc in list(_forwarder_procs.values()):
        try:
            proc.kill()
        except Exception:
            pass


# Tiến trình con cũng tự thoát khi app đóng stdin; atexit để chắc chắn khi app thoát bình thường
atexit.register(_kill_forwarder_procs)


POOL_SOURCE_HOMEPROXY = "homeproxy_api"


def load_proxies_from_thinaptm() -> list[str]:
    """Danh sách proxy của hệ thống = đúng các proxy HomeProxy đang thuê (lấy qua HomeProxy API).

    Mọi máy đều nạp cùng danh sách này rồi tự chia đều cho tài khoản của mình. KHÔNG trộn thêm các file proxy khác
    trong ThinAptm (vd proxy_us_tot_8.txt là IP Mỹ của công cụ khác). API lỗi -> dùng danh sách HomeProxy lấy được
    lần trước; chưa từng lấy được -> proxy_list trong settings.json của ThinAptm / file proxy.txt trong thư mục MuseAI."""
    proxies: list[str] = []
    token = ""
    candidate_dirs = [THINAPTM_DIR]
    for d in ["E", "D", "C", "F"]:
        d_path = f"{d}:\\ThinAptm0707"
        if d_path not in candidate_dirs:
            candidate_dirs.append(d_path)
    for c_dir in candidate_dirs:
        settings_file = os.path.join(c_dir, "settings.json")
        if not token and os.path.isfile(settings_file):
            try:
                with open(settings_file, "r", encoding="utf-8") as f:
                    token = (json.load(f).get("homeproxy_token") or "").strip()
            except Exception as e:
                log.warning("Lỗi đọc settings.json từ %s: %s", c_dir, e)
    token = token or HOMEPROXY_TOKEN or DEFAULT_HOMEPROXY_TOKEN

    if token:
        hp_list = list(dict.fromkeys(str(p).strip() for p in fetch_proxies_from_homeproxy_api(token) if p and str(p).strip()))
        if hp_list:
            log.info("🌐 HomeProxy API: %d proxy -> dùng làm danh sách proxy chung", len(hp_list))
            save_proxy_pool(hp_list, source=POOL_SOURCE_HOMEPROXY)
            return hp_list
        cached = get_cached_proxy_pool()
        if cached:
            log.warning("HomeProxy API không trả về proxy -> dùng lại %d proxy HomeProxy lấy được lần trước", len(cached))
            return cached

    log.warning("Không lấy được danh sách HomeProxy -> dùng proxy_list trong settings.json (ThinAptm) / proxy.txt cục bộ")
    return _load_proxies_from_files(candidate_dirs)


def _load_proxies_from_files(candidate_dirs: list[str]) -> list[str]:
    """Dự phòng khi chưa từng lấy được danh sách từ HomeProxy API."""
    proxies: list[str] = []

    for c_dir in candidate_dirs:
        settings_file = os.path.join(c_dir, "settings.json")
        if os.path.isfile(settings_file):
            try:
                with open(settings_file, "r", encoding="utf-8") as f:
                    s = json.load(f)
                for p in s.get("proxy_list", []):
                    p_clean = str(p).strip()
                    if p_clean and p_clean not in proxies and not p_clean.startswith("#"):
                        proxies.append(p_clean)
            except Exception as e:
                log.warning("Lỗi đọc settings.json từ %s: %s", c_dir, e)

    # Đọc thêm từ file proxy.txt hoặc proxies.txt ngay trong thư mục MuseAI (cực kỳ tiện khi mang sang máy khác)
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
    return proxies


def fetch_proxies_from_homeproxy_api(token: str) -> list[str]:
    """Gọi HomeProxy.vn API chuẩn theo thuật toán của ThinAptm (hỗ trợ x-merchant-id và fallback orders)."""
    token = token or DEFAULT_HOMEPROXY_TOKEN
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
    """Danh sách HomeProxy lấy được lần gần nhất (cache cũ không ghi nguồn có thể lẫn proxy khác -> bỏ qua)."""
    if os.path.isfile(PROXY_CACHE_FILE):
        try:
            with open(PROXY_CACHE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("source") == POOL_SOURCE_HOMEPROXY:
                return d.get("proxies", [])
        except Exception:
            pass
    return []


def save_proxy_pool(proxies: list[str], source: str = POOL_SOURCE_HOMEPROXY):
    """Lưu danh sách proxy vào cache file."""
    os.makedirs(os.path.dirname(PROXY_CACHE_FILE), exist_ok=True)
    try:
        with open(PROXY_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"proxies": proxies, "source": source, "updated_at": int(time.time())},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _proxy_loads(proxies: list[str], exclude_acc: str | None = None) -> dict[str, int]:
    """Số tài khoản đang gắn vào từng proxy (để luôn chọn proxy ít tài khoản nhất)."""
    loads = {p: 0 for p in proxies}
    for a in store.list_accounts():
        if a.get("id") == exclude_acc:
            continue
        p = a.get("proxy")
        if p in loads:
            loads[p] += 1
    return loads


def _least_loaded_first(proxies: list[str], loads: dict[str, int]) -> list[str]:
    """Proxy ít tài khoản nhất lên trước; các proxy bằng tải thì xáo ngẫu nhiên."""
    shuffled = list(proxies)
    random.shuffle(shuffled)
    return sorted(shuffled, key=lambda p: loads.get(p, 0))


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
    Nếu chưa có proxy hoặc proxy cũ đã chết (die), tự động chọn proxy mới còn sống từ Pool.
    Nếu người dùng tắt Fake Proxy (is_proxy_enabled=False), trả về None để chạy trực tiếp."""
    if not is_proxy_enabled():
        return None

    acc = store.get_account(acc_id)
    if not acc:
        return None

    cur_proxy = acc.get("proxy")
    # Chỉ đọc cache (không gọi mạng): hàm này chạy mỗi lần mượn/trả phiên trình duyệt
    cached_pool = get_cached_proxy_pool()
    stale = bool(cur_proxy and cached_pool and cur_proxy not in cached_pool)
    if stale:
        log.info("🔀 TK %s đang dùng proxy không còn trong danh sách HomeProxy -> chuyển sang proxy ít TK nhất",
                 acc.get("label", acc_id))
    if cur_proxy and not force_check and not stale:
        return cur_proxy

    if cur_proxy and not stale:
        ok, ip_or_err = test_proxy(cur_proxy)
        if ok:
            store.update_account(acc_id, proxy_status="alive", proxy_ip=ip_or_err)
            return cur_proxy
        else:
            log.warning("❌ Proxy của TK %s đã DIE (%s) -> Tiến hành đổi sang proxy mới!", acc.get("label", acc_id), ip_or_err)
            store.update_account(acc_id, proxy_status="dead", proxy_error=ip_or_err)

    # Cần cấp proxy mới từ danh sách HomeProxy
    pool = cached_pool or load_proxies_from_thinaptm()

    if not pool:
        log.warning("Không có proxy nào khả dụng trong HomeProxy Pool!")
        return None

    # Ưu tiên proxy đang ít tài khoản nhất (trước đây chọn ngẫu nhiên -> có proxy gánh 20+ TK cùng 1 IP)
    loads = _proxy_loads(pool, exclude_acc=acc_id)
    available = _least_loaded_first([p for p in pool if p != cur_proxy] or pool, loads)
    for candidate in available:
        ok, ip_or_err = test_proxy(candidate)
        if ok:
            assign_sticky_proxy(acc_id, candidate, ip=ip_or_err)
            return candidate

    log.error("Tất cả proxy trong Pool đều không phản hồi khi cấp cho TK %s!", acc_id)
    return None


def sync_all_accounts_with_homeproxy(rebalance: bool = False) -> dict:
    """Nạp lại danh sách HomeProxy và gán proxy CỐ ĐỊNH cho tài khoản của máy này.

    Mặc định chỉ gán cho TK chưa có proxy hoặc proxy đã chết / không còn trong danh sách HomeProxy (chọn proxy
    ít TK nhất) — TK đang có proxy sống giữ nguyên IP. rebalance=True (người dùng xác nhận) mới chuyển bớt TK
    khỏi proxy đang gánh quá ceil(số TK / số proxy)."""
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

    # Proxy đang có TK dùng mà test nhanh bị trượt: test lại kỹ trước khi coi là chết (tránh đổi IP oan)
    alive_set = {p for p, _ in alive_proxies}
    in_use = {a.get("proxy") for a in accounts if a.get("proxy")}
    for p in [p for p in proxies if p in in_use and p not in alive_set]:
        ok, ip = test_proxy(p, 10.0)
        if ok:
            alive_proxies.append((p, ip))

    # Nếu không test được mạng ngoài, dùng toàn bộ danh sách proxy làm fallback
    if not alive_proxies and proxies:
        alive_proxies = [(p, "") for p in proxies]

    alive_ip = dict(alive_proxies)
    cap = -(-len(accounts) // len(alive_ip)) if alive_ip else 0
    loads = {p: 0 for p in alive_ip}
    keep, to_assign = [], []
    for a in accounts:
        p = a.get("proxy")
        if p in loads and (not rebalance or loads[p] < cap):
            loads[p] += 1
            keep.append(a)
        else:
            to_assign.append(a)
    # Số TK đang vượt mức chia đều (chỉ báo, không tự chuyển nếu người dùng chưa xác nhận)
    overloaded = sum(max(0, n - cap) for n in loads.values()) if not rebalance else 0
    rebalanced = 0
    for a in to_assign:
        if not loads:
            break
        chosen = _least_loaded_first(list(loads), loads)[0]
        loads[chosen] += 1
        if a.get("proxy"):
            rebalanced += 1
        assign_sticky_proxy(a["id"], chosen, alive_ip.get(chosen, ""))
        assigned_count += 1
    if alive_ip:
        log.info("📌 [Proxy Manager] %d TK / %d proxy sống (của %d HomeProxy): gán mới %d, đổi proxy %d TK "
                 "(proxy chết/không còn trong danh sách%s), mức chia đều %d TK/proxy, đang vượt mức %d TK",
                 len(accounts), len(alive_ip), len(proxies), assigned_count - rebalanced, rebalanced,
                 " + chia lại theo yêu cầu" if rebalance else "", cap, overloaded)

    return {
        "success": True,
        "pool_size": len(proxies),
        "total_proxies": len(proxies),
        "alive_proxies": len(alive_proxies),
        "total_accounts": len(accounts),
        "assigned_count": assigned_count,
        "newly_assigned": assigned_count - rebalanced,
        "rebalanced": rebalanced,
        "max_accounts_per_proxy": cap,
        "overloaded": overloaded,
        "accounts": store.list_accounts()
    }


def get_forwarder_url_for_account(acc_id: str) -> str | None:
    """Trả về URL http://127.0.0.1:<port> của local forwarder cho tài khoản, hoặc None."""
    if not is_proxy_enabled():
        return None
    try:
        p_str = ensure_alive_proxy_for_account(acc_id)
        if p_str:
            port = start_local_forwarder(p_str)
            return f"http://127.0.0.1:{port}"
    except Exception as e:
        log.warning("Không thể lấy forwarder url cho TK %s: %s", acc_id, e)
    return None

