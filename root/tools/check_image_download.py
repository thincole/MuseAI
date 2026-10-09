"""Chẩn đoán: máy này có tải được ảnh sản phẩm Shopee không (dùng khi Ghép Outro 12s báo "không tải được ảnh sản phẩm").

Chạy (trong thư mục root):  python tools\\check_image_download.py
Kiểm tra lần lượt: DNS của CDN ảnh Shopee, proxy hệ thống Windows, tải bằng đúng hàm phần mềm dùng, tải trực tiếp bỏ qua
proxy hệ thống, và bằng requests / curl_cffi. Dòng nào báo LOI là chỗ hỏng.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TEST_URL = "https://cf.shopee.ph/file/ph-11134207-81zti-mm6ytsbdm7f3dc"  # ảnh SP thật (09/10/2026); 404 thì thay link khác
HOSTS = ["cf.shopee.ph", "down-ph.img.susercontent.com", "shopee.ph"]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"}


def step(name, fn):
    t = time.time()
    try:
        res = fn()
        print(f"[ OK ] {name}: {res} ({time.time() - t:.1f}s)")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[LOI ] {name}: {type(e).__name__}: {e} ({time.time() - t:.1f}s)")
        return False


def dns(host):
    ips = sorted({i[4][0] for i in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    bad = [ip for ip in ips if not ipaddress.ip_address(ip.split("%")[0]).is_global]
    return f"{ips}" + (f"  <-- IP NOI BO/AO {bad}: DNS bi phan mem VPN/proxy (fake-ip) chiem" if bad else "")


def fetch_urllib(use_system_proxy: bool):
    handlers = [] if use_system_proxy else [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    with opener.open(urllib.request.Request(TEST_URL, headers=UA), timeout=15) as r:
        return f"HTTP {r.status}, {len(r.read())} bytes, {r.headers.get('Content-Type')}"


def fetch_requests():
    import requests
    r = requests.get(TEST_URL, headers=UA, timeout=15)
    return f"HTTP {r.status_code}, {len(r.content)} bytes"


def fetch_cffi():
    from curl_cffi import requests as cr
    r = cr.get(TEST_URL, impersonate="chrome124", timeout=15)
    return f"HTTP {r.status_code}, {len(r.content)} bytes"


def fetch_app():
    os.environ.setdefault("MUSE2API_HOME", ROOT)
    import shopee_engine
    data, ctype = shopee_engine._fetch_image_bytes(TEST_URL, timeout=15)
    return f"{len(data)} bytes, {ctype}"


if __name__ == "__main__":
    print("Python", sys.version.split()[0], "|", sys.executable)
    for h in HOSTS:
        step(f"DNS {h}", lambda h=h: dns(h))
    step("Proxy he thong Windows (urllib.getproxies)", lambda: urllib.request.getproxies() or "khong co")
    step("Tai anh bang HAM CUA PHAN MEM (shopee_engine._fetch_image_bytes)", fetch_app)
    step("Tai anh urllib CO proxy he thong", lambda: fetch_urllib(True))
    step("Tai anh urllib BO QUA proxy he thong", lambda: fetch_urllib(False))
    step("Tai anh bang requests", fetch_requests)
    step("Tai anh bang curl_cffi", fetch_cffi)
    print("\nGui toan bo ket qua tren cho nguoi ho tro.")
