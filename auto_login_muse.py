#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
               MUSE.AI AUTO-LOGIN & COOKIE IMPORTER TOOL
==============================================================================
Tự động đăng nhập hàng loạt tài khoản Muse.ai từ file .txt (email|pass|token...):
1. Đọc danh sách tài khoản từ file (mặc định: 24-MUSE.txt).
2. Tự động truy cập https://muse.ai/ và nhập email để nhận mã OTP xác thực.
3. Chuyển sang https://dongvanfb.net/read_mail_box/, chọn OAuth2, đọc hòm thư.
4. Tự động nhận diện thư từ Meta/Muse, bóc tách mã xác thực 6 số (OTP).
5. Quay lại trang muse.ai, điền mã OTP và hoàn tất đăng nhập.
6. Tự động trích xuất toàn bộ Cookies (kể cả httpOnly: hatch_sess, hatch_gw, hatch_vml, hatch_native_auth_device).
7. Tự động lưu thẳng vào data/accounts.json của MuseAI Video Studio Pro và xuất file dự phòng.
==============================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime

# Đảm bảo UTF-8 cho Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Thư mục gốc dự án
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from config import CFG
    from store import Store
except ImportError:
    CFG = None
    Store = None

try:
    from DrissionPage import ChromiumPage, ChromiumOptions
except ImportError:
    print("[LỖI] Chưa cài đặt DrissionPage! Vui lòng chạy: pip install DrissionPage")
    sys.exit(1)


# Màu sắc hiển thị Console
class Colors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"


def log_info(msg: str):
    print(f"{Colors.OKCYAN}[*]{Colors.ENDC} {msg}", flush=True)


def log_success(msg: str):
    print(f"{Colors.OKGREEN}[✓]{Colors.ENDC} {msg}", flush=True)


def log_warn(msg: str):
    print(f"{Colors.WARNING}[!]{Colors.ENDC} {msg}", flush=True)


def log_error(msg: str):
    print(f"{Colors.FAIL}[✗]{Colors.ENDC} {msg}", flush=True)


def find_chrome_path() -> str:
    """Tìm đường dẫn Chrome hoặc Edge trên máy tính."""
    cands = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for p in cands:
        if os.path.isfile(p):
            return p
    w = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("msedge")
    if w:
        return w
    return r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def load_accounts_from_file(file_path: str) -> list[str]:
    """Đọc danh sách các dòng tài khoản từ file .txt."""
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Không tìm thấy file: {file_path}")
    accounts = []
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            accounts.append(line)
    return accounts


def format_cookie_header(cookies: dict[str, str]) -> str:
    """Chuyển đổi dict cookies thành chuỗi header tiêu chuẩn k1=v1; k2=v2."""
    return "; ".join([f"{k}={v}" for k, v in cookies.items()])


def is_recent_dongvan_time(time_str: str, max_age_seconds: int = 3600) -> bool:
    """Kiểm tra xem email trên DongVanFB có phải là email nhận gần đây hay không."""
    try:
        m = re.search(r'(\d{1,2}):(\d{2})\s*-\s*(\d{1,2})/(\d{1,2})/(\d{4})', time_str)
        if not m:
            return True
        h, mi, d, mo, y = map(int, m.groups())
        now = datetime.now()
        # Nếu khác ngày (ví dụ thư từ những ngày trước) -> bỏ qua
        if (d, mo, y) != (now.day, now.month, now.year):
            return False
        email_dt = datetime(y, mo, d, h, mi)
        diff = (now - email_dt).total_seconds()
        # Cho phép nhận trong vòng 1 tiếng, dung sai lệch đồng hồ tối đa 15 phút (-900s)
        return -900 <= diff <= max_age_seconds
    except Exception:
        return True


def _update_export_txt(file_path: str, email: str, cstr: str):
    """Cập nhật cookie vào file txt, nếu đã có email thì ghi đè chứ không thêm dòng mới."""
    lines = []
    found = False
    if os.path.isfile(file_path):
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_s = line.strip()
                if not line_s:
                    continue
                if line_s.startswith(f"{email}|") or line_s.split("|")[0].strip().lower() == email.lower():
                    lines.append(f"{email}|{cstr}")
                    found = True
                else:
                    lines.append(line_s)
    if not found:
        lines.append(f"{email}|{cstr}")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class MuseAutoLogin:
    def __init__(self, headless: bool = False, chrome_path: str = ""):
        self.headless = headless
        self.chrome_path = chrome_path or find_chrome_path()
        self.profile_dir = os.path.join(tempfile.gettempdir(), "muse_autologin_isolated_profile")
        self.page: ChromiumPage | None = None
        self.dongvan_tab = None
        self.store = Store(CFG) if (Store and CFG) else None

    def start_browser(self):
        """Khởi động trình duyệt độc lập với Profile riêng để tránh xung đột với trình duyệt đang mở."""
        log_info(f"Đang khởi động trình duyệt Chrome ({'Ẩn danh / Headless' if self.headless else 'Hiển thị giao diện'})...")
        co = ChromiumOptions()
        co.set_browser_path(self.chrome_path)
        co.set_user_data_path(self.profile_dir)
        co.set_argument("--no-first-run")
        co.set_argument("--no-default-browser-check")
        co.set_argument("--disable-blink-features=AutomationControlled")
        if self.headless:
            co.headless(True)
        else:
            co.set_argument("--window-size=1366,850")

        self.page = ChromiumPage(co)
        # Mở tab 1 cho Muse.ai
        self.page.get("https://muse.ai")
        time.sleep(2)
        # Mở tab 2 cho DongVanFB
        self.dongvan_tab = self.page.new_tab("https://dongvanfb.net/read_mail_box/")
        time.sleep(2)
        # Quay về tab Muse
        self.page.activate_tab(0)
        log_success("Trình duyệt đã sẵn sàng với 2 tab: Muse.ai và DongVanFB.net")

    def stop_browser(self):
        """Đóng trình duyệt an toàn."""
        if self.page:
            try:
                self.page.quit()
            except Exception:
                pass
            self.page = None

    def clear_muse_session(self):
        """Xóa sạch Session, Cookies và Local Storage trên Muse.ai để chuẩn bị cho tài khoản mới."""
        try:
            self.page.run_cdp("Network.clearBrowserCookies")
            self.page.run_cdp("Network.clearBrowserCache")
            self.page.run_cdp("Storage.clearDataForOrigin", origin="https://muse.ai", storageTypes="all")
        except Exception:
            pass

    def fetch_otp_from_dongvan(self, acc_line: str, email: str, max_wait_sec: int = 50) -> str | None:
        """Thực hiện đọc hòm thư trên DongVanFB và trích xuất mã OTP."""
        log_info(f"[{email}] Chuyển sang DongVanFB, dán thông tin và bấm 'Đọc hòm thư'...")
        self.page.activate_tab(self.dongvan_tab)
        time.sleep(1)

        # 1. Chọn OAuth2 và điền vào textarea
        self.dongvan_tab.run_js("""
            let r = document.querySelector('input[value="oauth2"]');
            if (r) {
                r.checked = true;
                r.click();
                r.dispatchEvent(new Event('change', { bubbles: true }));
            }
            let ta = document.getElementById('list_email') || document.querySelector('textarea');
            if (ta) {
                ta.value = arguments[0];
                ta.dispatchEvent(new Event('input', { bubbles: true }));
                ta.dispatchEvent(new Event('change', { bubbles: true }));
            }
            let btn = document.querySelector('.btn-buy-home') || Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Đọc hòm thư'));
            if (btn) btn.click();
        """, acc_line)

        log_info(f"[{email}] Đang đợi thư mã đăng nhập gửi về hòm thư DongVanFB (tối đa {max_wait_sec}s)...")
        start_time = time.time()
        last_click = time.time()

        while time.time() - start_time < max_wait_sec:
            time.sleep(2)
            # Lấy toàn bộ hàng trong bảng
            rows = self.dongvan_tab.run_js("""
                let trs = Array.from(document.querySelectorAll('table tr'));
                return trs.map(r => Array.from(r.querySelectorAll('th, td')).map(c => c.innerText.trim()));
            """)

            if rows and len(rows) > 1:
                # Quét các hàng thư (bỏ qua hàng tiêu đề)
                for row in rows[1:]:
                    time_col = row[3] if len(row) > 3 else ""
                    if not is_recent_dongvan_time(time_col):
                        continue

                    row_text = " ".join(row)
                    # Tìm mã 6 số dạng: "Use code 123456 to log in" hoặc "Login code 123456" hoặc cột Code
                    m = re.search(r'(?:use\s+code|login\s+code|mã\s+xác\s+nhận|code)\s*[:\-]?\s*(\d{6})', row_text, re.IGNORECASE)
                    if not m:
                        # Kiểm tra xem có cột riêng chứa 6 số không
                        if len(row) >= 6 and re.search(r'^\d{6}', row[5]):
                            m = re.search(r'(\d{6})', row[5])
                        elif re.search(r'\b\d{6}\b', row_text):
                            m = re.search(r'\b\d{6}\b', row_text)

                    if m:
                        code = m.group(1)
                        log_success(f"[{email}] Đã tìm thấy mã OTP: {Colors.BOLD}{code}{Colors.ENDC}")
                        return code

            # Nếu sau 7s chưa thấy thư mới, bấm lại nút Đọc hòm thư để làm mới
            if time.time() - last_click > 7:
                last_click = time.time()
                self.dongvan_tab.run_js("""
                    let btn = document.querySelector('.btn-buy-home') || Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Đọc hòm thư'));
                    if (btn) btn.click();
                """)

        log_warn(f"[{email}] Quá thời gian {max_wait_sec}s không nhận được mã OTP trên DongVanFB.")
        return None

    def login_account(self, acc_line: str) -> dict | None:
        """Quy trình đăng nhập 1 tài khoản hoàn chỉnh."""
        email = acc_line.split("|")[0].strip()
        log_info(f"{Colors.HEADER}=== BẮT ĐẦU ĐĂNG NHẬP: {email} ==={Colors.ENDC}")

        # Bước 1: Chuẩn bị trang Muse.ai sạch sẽ
        self.page.activate_tab(0)
        self.clear_muse_session()
        self.page.get("https://muse.ai")
        time.sleep(3)

        # Kiểm tra ô nhập email
        email_inp = self.page.ele("@placeholder:Mobile number or email") or self.page.ele("tag:input[inputmode='email']")
        if not email_inp:
            # Thử tìm nút Log in nếu đang ở trang giới thiệu
            login_link = self.page.ele("text:Log in")
            if login_link:
                login_link.click()
                time.sleep(2)
                email_inp = self.page.ele("@placeholder:Mobile number or email")

        if not email_inp:
            log_error(f"[{email}] Không tìm thấy ô nhập email trên trang Muse.ai!")
            return None

        # Nhập email
        email_inp.clear()
        email_inp.input(email)
        time.sleep(1)

        # Bấm Continue
        btn_continue = self.page.ele("text:Continue") or self.page.ele("tag:button@text():Continue")
        if not btn_continue:
            log_error(f"[{email}] Không tìm thấy nút Continue!")
            return None

        btn_continue.click()
        log_info(f"[{email}] Đã bấm Continue, chờ màn hình yêu cầu OTP...")
        time.sleep(4)

        # Kiểm tra xem có sang màn hình OTP không
        is_otp_screen = self.page.run_js("""
            return !!(document.querySelector('input[autocomplete="one-time-code"]') || 
                      document.querySelector('input[aria-label="6-digit security code"]') ||
                      document.querySelector('input.sr-only'));
        """)
        if not is_otp_screen:
            # Kiểm tra xem có thông báo lỗi gì không
            body_txt = self.page.run_js("return document.body.innerText;")
            log_error(f"[{email}] Không xuất hiện ô nhập mã OTP! Phản hồi từ trang: {body_txt[:150]}...")
            return None

        # Bước 2: Lấy mã OTP từ DongVanFB
        otp_code = self.fetch_otp_from_dongvan(acc_line, email)
        if not otp_code:
            return None

        # Bước 3: Quay lại Muse.ai và điền mã OTP
        self.page.activate_tab(0)
        time.sleep(1)
        log_info(f"[{email}] Đang nhập mã OTP {otp_code} vào trang Muse.ai...")

        fill_ok = self.page.run_js("""
            let input = document.querySelector('input[autocomplete="one-time-code"]') || 
                        document.querySelector('input[aria-label="6-digit security code"]') ||
                        document.querySelector('input.sr-only');
            if (!input) return false;
            input.focus();
            let nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
            nativeSetter.call(input, arguments[0]);
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        """, otp_code)

        if not fill_ok:
            log_error(f"[{email}] Điền mã OTP vào React input thất bại!")
            return None

        time.sleep(2)
        # Nếu có nút Next/Confirm thì click
        next_btn = self.page.ele("text:Next")
        if next_btn and "disabled" not in str(next_btn.attr("class")).lower() and next_btn.attr("disabled") is None:
            try:
                next_btn.click()
            except Exception:
                pass

        # Bước 4: Chờ đăng nhập thành công và trích xuất cookie
        log_info(f"[{email}] Đang chờ hệ thống Muse.ai xác nhận đăng nhập...")
        login_success = False
        all_cookies = {}
        cookies_exp = {}

        for _ in range(15):
            time.sleep(2)
            try:
                cdp_res = self.page.run_cdp("Storage.getCookies")
                cookies_list = cdp_res.get("cookies", [])
                m_cookies = {c["name"]: c["value"] for c in cookies_list if "muse.ai" in c.get("domain", "")}
                
                # Kiểm tra cookie phiên đăng nhập chính
                if "hatch_sess" in m_cookies:
                    login_success = True
                    all_cookies = m_cookies
                    cookies_exp = {
                        c["name"]: int(float(c.get("expires", -1)))
                        for c in cookies_list
                        if "muse.ai" in c.get("domain", "") and c.get("expires", 0) > 0
                    }
                    break
            except Exception:
                pass

        if not login_success:
            log_error(f"[{email}] Đăng nhập thất bại hoặc hết thời gian chờ xác nhận!")
            return None

        # Chuyển vào trang làm việc để Muse kích hoạt hatch_gw (Gateway Token)
        try:
            self.page.get("https://muse.ai/thread/new")
            for _ in range(6):
                time.sleep(1)
                cdp_res = self.page.run_cdp("Storage.getCookies")
                for c in cdp_res.get("cookies", []):
                    if "muse.ai" in c.get("domain", ""):
                        all_cookies[c["name"]] = c["value"]
                        if c.get("expires", 0) > 0:
                            cookies_exp[c["name"]] = int(float(c.get("expires")))
                if "hatch_gw" in all_cookies:
                    log_info(f"[{email}] Đã nhận đầy đủ 4 cookie cốt lõi (kể cả hatch_gw).")
                    break
        except Exception:
            pass

        log_success(f"[{email}] {Colors.BOLD}ĐĂNG NHẬP THÀNH CÔNG!{Colors.ENDC} Thu thập được {len(all_cookies)} Cookies.")
        return {
            "email": email,
            "acc_line": acc_line,
            "cookies": all_cookies,
            "cookies_exp": cookies_exp,
            "login_time": int(time.time()),
        }

    def process_all_accounts(self, file_path: str, save_to_store: bool = True, delay_between: int = 3, num_threads: int = 1):
        """Xử lý toàn bộ danh sách tài khoản trong file (Hỗ trợ đơn luồng và đa luồng)."""
        accounts = load_accounts_from_file(file_path)
        total = len(accounts)
        log_info(f"Đã tải {Colors.BOLD}{total}{Colors.ENDC} tài khoản từ file: {file_path}")

        # Thư mục lưu kết quả xuất
        out_dir = os.path.join(BASE_DIR, "data")
        os.makedirs(out_dir, exist_ok=True)
        export_txt_path = os.path.join(out_dir, "auto_login_success_cookies.txt")
        export_json_path = os.path.join(out_dir, "auto_login_results.json")

        success_count = 0
        failed_count = 0
        results = []
        lock = threading.Lock()

        # NẾU CHẠY ĐA LUỒNG (num_threads > 1)
        if num_threads > 1:
            log_info(f"Khởi chạy chế độ {Colors.BOLD}ĐA LUỒNG: {num_threads} luồng song song{Colors.ENDC}...")
            q = queue.Queue()
            for idx, acc in enumerate(accounts, start=1):
                q.put((idx, acc))

            def worker_loop(wid: int):
                nonlocal success_count, failed_count
                port = 9320 + wid * 5
                pdir = os.path.join(tempfile.gettempdir(), f"muse_cli_worker_{wid}")
                co = ChromiumOptions()
                co.set_browser_path(self.chrome_path)
                co.set_user_data_path(pdir)
                co.set_local_port(port)
                co.set_argument("--no-first-run")
                co.set_argument("--no-default-browser-check")
                co.set_argument("--disable-blink-features=AutomationControlled")
                if self.headless:
                    co.headless(True)
                else:
                    co.set_argument("--window-size=1200,800")

                try:
                    wpage = ChromiumPage(co)
                    wpage.get("https://muse.ai")
                    time.sleep(2)
                    wdongvan = wpage.new_tab("https://dongvanfb.net/read_mail_box/")
                    time.sleep(2)
                    wpage.activate_tab(0)
                except Exception as e:
                    log_error(f"[Luồng {wid}] Không thể khởi động Chrome: {e}")
                    return

                try:
                    while True:
                        try:
                            idx, acc_line = q.get_nowait()
                        except queue.Empty:
                            break

                        email = acc_line.split("|")[0].strip()
                        log_info(f"[Luồng {wid}] [{idx}/{total}] Đang xử lý: {Colors.BOLD}{email}{Colors.ENDC}")

                        # Thực hiện đăng nhập trên luồng này
                        try:
                            # 1. Reset Muse session
                            wpage.activate_tab(0)
                            try:
                                wpage.run_cdp("Network.clearBrowserCookies")
                                wpage.run_cdp("Network.clearBrowserCache")
                                wpage.run_cdp("Storage.clearDataForOrigin", origin="https://muse.ai", storageTypes="all")
                            except Exception:
                                pass
                            wpage.get("https://muse.ai")
                            time.sleep(3)

                            e_inp = wpage.ele("@placeholder:Mobile number or email") or wpage.ele("tag:input[inputmode='email']")
                            if not e_inp:
                                l_link = wpage.ele("text:Log in")
                                if l_link:
                                    l_link.click()
                                    time.sleep(2)
                                    e_inp = wpage.ele("@placeholder:Mobile number or email")

                            if not e_inp:
                                log_error(f"[Luồng {wid}] [{email}] Không thấy ô nhập email!")
                                with lock:
                                    failed_count += 1
                                q.task_done()
                                continue

                            e_inp.clear()
                            e_inp.input(email)
                            time.sleep(1)

                            b_cont = wpage.ele("text:Continue") or wpage.ele("tag:button@text():Continue")
                            if not b_cont:
                                log_error(f"[Luồng {wid}] [{email}] Không thấy nút Continue!")
                                with lock:
                                    failed_count += 1
                                q.task_done()
                                continue

                            b_cont.click()
                            time.sleep(4)

                            # 2. Đọc OTP DongVanFB
                            wpage.activate_tab(wdongvan)
                            time.sleep(1)
                            wdongvan.run_js("""
                                let r = document.querySelector('input[value="oauth2"]');
                                if (r) {
                                    r.checked = true;
                                    r.click();
                                    r.dispatchEvent(new Event('change', { bubbles: true }));
                                }
                                let ta = document.getElementById('list_email') || document.querySelector('textarea');
                                if (ta) {
                                    ta.value = arguments[0];
                                    ta.dispatchEvent(new Event('input', { bubbles: true }));
                                    ta.dispatchEvent(new Event('change', { bubbles: true }));
                                }
                                let btn = document.querySelector('.btn-buy-home') || Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Đọc hòm thư'));
                                if (btn) btn.click();
                            """, acc_line)

                            otp_code = None
                            st_time = time.time()
                            lr_time = time.time()
                            while time.time() - st_time < 50:
                                time.sleep(1)
                                rows = wdongvan.run_js("""
                                    let trs = Array.from(document.querySelectorAll('table tr'));
                                    return trs.map(r => Array.from(r.querySelectorAll('th, td')).map(c => c.innerText.trim()));
                                """)
                                if rows and len(rows) > 1:
                                    for r in rows[1:]:
                                        if len(r) > 2 and r[2] == '...':
                                            continue
                                        if len(r) > 0 and email.lower() not in r[0].lower():
                                            continue
                                        r_txt = " ".join(r)
                                        time_col = r[3] if len(r) > 3 else ""
                                        if not is_recent_dongvan_time(time_col):
                                            continue

                                        m = re.search(r'(?:use\s+code|login\s+code|mã\s+xác\s+nhận|code)\s*[:\-]?\s*(\d{6})', r_txt, re.IGNORECASE)
                                        if not m:
                                            if len(r) >= 6 and re.search(r'^\d{6}', r[5]):
                                                m = re.search(r'(\d{6})', r[5])
                                            elif re.search(r'\b\d{6}\b', r_txt):
                                                m = re.search(r'\b\d{6}\b', r_txt)
                                        if m:
                                            otp_code = m.group(1)
                                            break
                                if otp_code:
                                    break
                                if time.time() - lr_time > 10:
                                    lr_time = time.time()
                                    wdongvan.run_js("""
                                        let btn = document.querySelector('.btn-buy-home') || Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Đọc hòm thư'));
                                        if (btn) btn.click();
                                    """)

                            if not otp_code:
                                log_warn(f"[Luồng {wid}] [{email}] Hết giờ chờ OTP!")
                                with lock:
                                    failed_count += 1
                                q.task_done()
                                continue

                            # 3. Điền OTP vào Muse
                            log_success(f"[Luồng {wid}] [{email}] Nhận mã OTP: {Colors.BOLD}{otp_code}{Colors.ENDC}. Đang xác nhận...")
                            wpage.activate_tab(0)
                            time.sleep(1)
                            wpage.run_js("""
                                let input = document.querySelector('input[autocomplete="one-time-code"]') || 
                                            document.querySelector('input[aria-label="6-digit security code"]') ||
                                            document.querySelector('input.sr-only');
                                if (input) {
                                    input.focus();
                                    let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                                    setter.call(input, arguments[0]);
                                    input.dispatchEvent(new Event('input', { bubbles: true }));
                                    input.dispatchEvent(new Event('change', { bubbles: true }));
                                }
                            """, otp_code)
                            time.sleep(2)
                            n_btn = wpage.ele("text:Next")
                            if n_btn and "disabled" not in str(n_btn.attr("class")).lower() and n_btn.attr("disabled") is None:
                                try:
                                    n_btn.click()
                                except Exception:
                                    pass

                            # 4. Chờ đăng nhập và lấy cookie
                            l_ok = False
                            all_c = {}
                            c_exp = {}
                            for _ in range(15):
                                time.sleep(2)
                                try:
                                    cdp = wpage.run_cdp("Storage.getCookies")
                                    cl = cdp.get("cookies", [])
                                    mc = {c["name"]: c["value"] for c in cl if "muse.ai" in c.get("domain", "")}
                                    if "hatch_sess" in mc:
                                        l_ok = True
                                        all_c = mc
                                        c_exp = {
                                            c["name"]: int(float(c.get("expires", -1)))
                                            for c in cl
                                            if "muse.ai" in c.get("domain", "") and c.get("expires", 0) > 0
                                        }
                                        break
                                except Exception:
                                    pass

                            if l_ok:
                                # Chuyển vào trang làm việc để Muse kích hoạt hatch_gw (Gateway Token)
                                try:
                                    wpage.get("https://muse.ai/thread/new")
                                    for _ in range(6):
                                        time.sleep(1)
                                        cdp = wpage.run_cdp("Storage.getCookies")
                                        for c in cdp.get("cookies", []):
                                            if "muse.ai" in c.get("domain", ""):
                                                all_c[c["name"]] = c["value"]
                                                if c.get("expires", 0) > 0:
                                                    c_exp[c["name"]] = int(float(c.get("expires")))
                                        if "hatch_gw" in all_c:
                                            log_info(f"[Luồng {wid}] [{email}] Đã nhận đầy đủ 4 cookie cốt lõi (kể cả hatch_gw).")
                                            break
                                except Exception:
                                    pass

                                # Kích hoạt session cốt lõi hatch_vml và wake VM
                                try:
                                    from engine import MuseEngine
                                    if CFG:
                                        eng = MuseEngine(CFG)
                                        ren = eng.renew_session_http(all_c, c_exp, wake_vm=True)
                                        if ren and ren.get("cookies"):
                                            all_c = ren["cookies"]
                                            c_exp = ren.get("cookies_exp", c_exp)
                                            log_info(f"[Luồng {wid}] [{email}] Kích hoạt hatch_vml thành công (VM: {ren.get('vm_state')}).")
                                except Exception:
                                    pass

                                cstr = format_cookie_header(all_c)
                                synced_live = False

                                # Đồng bộ Live API máy chủ MuseAI Studio
                                if save_to_store and CFG:
                                    try:
                                        import requests
                                        url = f"http://127.0.0.1:{CFG.port}/admin/accounts"
                                        headers = {"Authorization": f"Bearer {CFG.api_key}"} if CFG.api_key else {}
                                        try:
                                            rl = requests.get(url, headers=headers, timeout=3)
                                            if rl.status_code == 200:
                                                for old_a in rl.json().get("accounts", []):
                                                    if old_a.get("label") == email:
                                                        requests.delete(f"{url}/{old_a['id']}", headers=headers, timeout=3)
                                        except Exception:
                                            pass
                                        payload = {
                                            "label": email,
                                            "cookies": all_c,
                                            "cookie_header": cstr,
                                            "expires": {k: int(v) for k, v in c_exp.items() if v > 0}
                                        }
                                        r = requests.post(url, json=payload, headers=headers, timeout=5)
                                        if r.status_code == 200:
                                            synced_live = True
                                            log_success(f"[Luồng {wid}] [{email}] {Colors.BOLD}Đã nạp thành công vào MuseAI Studio qua Live API!{Colors.ENDC}")
                                    except Exception:
                                        pass

                                with lock:
                                    success_count += 1
                                    results.append({
                                        "email": email,
                                        "cookies": all_c,
                                        "cookies_exp": c_exp,
                                        "time": int(time.time()),
                                    })
                                    if save_to_store and not synced_live and self.store:
                                        acc = self.store.add_account(all_c, label=email, cookies_exp=c_exp)
                                        log_success(f"[Luồng {wid}] [{email}] {Colors.BOLD}Đã lưu/ghi đè vào data/accounts.json (ID: {acc['id']}){Colors.ENDC}")

                                    _update_export_txt(export_txt_path, email, cstr)
                            else:
                                with lock:
                                    failed_count += 1
                                log_error(f"[Luồng {wid}] [{email}] Xác nhận đăng nhập thất bại!")

                        except Exception as ex:
                            with lock:
                                failed_count += 1
                            log_error(f"[Luồng {wid}] [{email}] Lỗi: {ex}")
                        finally:
                            q.task_done()

                        if delay_between > 0:
                            time.sleep(delay_between)

                finally:
                    try:
                        wpage.quit()
                    except Exception:
                        pass

            threads = []
            actual_w = min(num_threads, total)
            for wid in range(1, actual_w + 1):
                t = threading.Thread(target=worker_loop, args=(wid,))
                t.daemon = True
                threads.append(t)
                t.start()

            for t in threads:
                t.join()

        # NẾU CHẠY ĐƠN LUỒNG (num_threads == 1)
        else:
            self.start_browser()
            try:
                for idx, acc_line in enumerate(accounts, start=1):
                    email = acc_line.split("|")[0].strip()
                    print("\n" + "=" * 70)
                    log_info(f"[{idx}/{total}] Đang xử lý tài khoản: {Colors.BOLD}{email}{Colors.ENDC}")
                    print("=" * 70)

                    try:
                        res = self.login_account(acc_line)
                        if res:
                            success_count += 1
                            results.append(res)

                            # Kích hoạt hatch_vml và wake VM
                            try:
                                from engine import MuseEngine
                                if CFG:
                                    eng = MuseEngine(CFG)
                                    ren = eng.renew_session_http(res["cookies"], res["cookies_exp"], wake_vm=True)
                                    if ren and ren.get("cookies"):
                                        res["cookies"] = ren["cookies"]
                                        res["cookies_exp"] = ren.get("cookies_exp", res["cookies_exp"])
                                        log_info(f"[{email}] Kích hoạt hatch_vml thành công (VM: {ren.get('vm_state')}).")
                            except Exception:
                                pass

                            cookie_str = format_cookie_header(res["cookies"])
                            synced_live = False

                            # Live API
                            if save_to_store and CFG:
                                try:
                                    import requests
                                    url = f"http://127.0.0.1:{CFG.port}/admin/accounts"
                                    headers = {"Authorization": f"Bearer {CFG.api_key}"} if CFG.api_key else {}
                                    try:
                                        rl = requests.get(url, headers=headers, timeout=3)
                                        if rl.status_code == 200:
                                            for old_a in rl.json().get("accounts", []):
                                                if old_a.get("label") == email:
                                                    requests.delete(f"{url}/{old_a['id']}", headers=headers, timeout=3)
                                    except Exception:
                                        pass
                                    payload = {
                                        "label": email,
                                        "cookies": res["cookies"],
                                        "cookie_header": cookie_str,
                                        "expires": {k: int(v) for k, v in res["cookies_exp"].items() if v > 0}
                                    }
                                    r = requests.post(url, json=payload, headers=headers, timeout=5)
                                    if r.status_code == 200:
                                        synced_live = True
                                        log_success(f"[{email}] Đã nạp thành công vào MuseAI Studio qua Live API!")
                                except Exception:
                                    pass

                            if save_to_store and not synced_live and self.store:
                                acc = self.store.add_account(res["cookies"], label=email, cookies_exp=res["cookies_exp"])
                                log_success(f"[{email}] Đã lưu/ghi đè vào data/accounts.json (ID: {acc['id']}).")

                            _update_export_txt(export_txt_path, email, cookie_str)
                            log_success(f"[{email}] Đã lưu/ghi đè dự phòng vào {export_txt_path}")
                        else:
                            failed_count += 1
                    except Exception as e:
                        failed_count += 1
                        log_error(f"[{email}] Lỗi trong quá trình xử lý: {e}")

                    if idx < total and delay_between > 0:
                        log_info(f"Nghỉ {delay_between} giây trước khi chuyển sang tài khoản tiếp theo...")
                        time.sleep(delay_between)
            finally:
                self.stop_browser()

        # Lưu kết quả JSON
        try:
            with open(export_json_path, "w", encoding="utf-8") as jf:
                json.dump(results, jf, ensure_ascii=False, indent=2)
        except Exception:
            pass

        print("\n" + "=" * 70)
        print(f"               KẾT QUẢ QUÁ TRÌNH TỰ ĐỘNG ĐĂNG NHẬP")
        print("=" * 70)
        log_success(f"Tổng số tài khoản: {total}")
        log_success(f"Đăng nhập thành công: {success_count}")
        if failed_count > 0:
            log_warn(f"Thất bại: {failed_count}")
        log_info(f"File Cookies dự phòng: {export_txt_path}")
        log_info(f"File kết quả JSON: {export_json_path}")
        if save_to_store and self.store:
            log_success(f"Đã cập nhật tài khoản vào hệ thống: data/accounts.json")
        print("=" * 70 + "\n")


def main():
    # Chế độ tương tác trực tiếp khi người dùng nhấp đúp file .bat (không có tham số)
    if len(sys.argv) == 1:
        print("=" * 70)
        print("       MUSE.AI AUTO-LOGIN & DONGVANFB OTP MATCHER TOOL")
        print("       Phần Mềm Tự Động Đăng Nhập Hàng Loạt Tài Khoản Muse")
        print("=" * 70)

        # 1. Chọn file tài khoản
        default_file = "24-MUSE.txt" if os.path.isfile(os.path.join(BASE_DIR, "24-MUSE.txt")) else ""
        print(f"\n[1] File danh sách tài khoản (Mặc định: {default_file or 'Nhập tên file'}):")
        inp_file = input(f"    Ấn Enter để dùng '{default_file}', hoặc gõ tên file: ").strip()
        file_name = inp_file if inp_file else (default_file or "24-MUSE.txt")
        file_path = file_name if os.path.isabs(file_name) else os.path.join(BASE_DIR, file_name)

        if not os.path.isfile(file_path):
            print(f"[LỖI] Không tìm thấy file: {file_path}")
            txt_files = [f for f in os.listdir(BASE_DIR) if f.endswith(".txt") and "req" not in f.lower()]
            if txt_files:
                print("Các file .txt hiện có:")
                for f in txt_files:
                    print(f"  - {f}")
            input("\nNhấn Enter để thoát...")
            sys.exit(1)

        # 2. Chọn số luồng
        print("\n[2] Số luồng chạy song song để tăng tốc (1 - 10, Khuyên dùng: 3 - 5):")
        thr_inp = input("    Nhập số luồng (Ấn Enter để chọn 3 luồng): ").strip()
        try:
            num_threads = int(thr_inp) if thr_inp else 3
            num_threads = max(1, min(10, num_threads))
        except ValueError:
            num_threads = 3

        # 3. Chọn chế độ hiển thị
        print("\n[3] Chế độ hiển thị trình duyệt Chrome:")
        print("    1. Hiện cửa sổ Chrome (Trực quan, dễ theo dõi - Khuyên dùng)")
        print("    2. Chạy ngầm (Headless - Ẩn cửa sổ)")
        mode_inp = input("    Lựa chọn (1 hoặc 2, ấn Enter để chọn 1): ").strip()
        headless = (mode_inp == "2")

        print("\n" + "-" * 70)
        log_info(f"File tài khoản: {os.path.basename(file_path)}")
        log_info(f"Số luồng chạy song song: {num_threads} luồng")
        log_info(f"Chế độ: {'Chạy ngầm (Headless)' if headless else 'Hiển thị Chrome'}")
        log_info(f"Tự động lưu vào data/accounts.json: Có")
        print("-" * 70 + "\n")

        app = MuseAutoLogin(headless=headless)
        app.process_all_accounts(file_path, save_to_store=True, delay_between=2, num_threads=num_threads)
        return

    # Chế độ nhận tham số dòng lệnh
    parser = argparse.ArgumentParser(description="Tool tự động đăng nhập Muse.ai qua hòm thư DongVanFB")
    parser.add_argument("file", nargs="?", default="24-MUSE.txt", help="Đường dẫn file .txt chứa tài khoản (mặc định: 24-MUSE.txt)")
    parser.add_argument("--threads", "-t", type=int, default=3, help="Số luồng chạy song song (1 - 10, mặc định: 3)")
    parser.add_argument("--headless", action="store_true", help="Chạy ở chế độ ẩn trình duyệt (không hiện cửa sổ Chrome)")
    parser.add_argument("--no-store", action="store_true", help="Không tự động lưu vào accounts.json của MuseAI")
    parser.add_argument("--delay", type=int, default=2, help="Thời gian nghỉ giữa 2 tài khoản (giây, mặc định: 2)")
    args = parser.parse_args()

    file_path = args.file
    if not os.path.isabs(file_path):
        file_path = os.path.join(BASE_DIR, file_path)

    if not os.path.isfile(file_path):
        print(f"[LỖI] Không tìm thấy file danh sách tài khoản: {file_path}")
        txt_files = [f for f in os.listdir(BASE_DIR) if f.endswith(".txt") and "req" not in f.lower()]
        if txt_files:
            print("Các file .txt hiện có trong thư mục:")
            for f in txt_files:
                print(f"  - {f}")
        sys.exit(1)

    app = MuseAutoLogin(headless=args.headless)
    app.process_all_accounts(file_path, save_to_store=not args.no_store, delay_between=args.delay, num_threads=args.threads)


if __name__ == "__main__":
    main()


