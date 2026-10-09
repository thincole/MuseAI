#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
               MUSE.AI COOKIE HARVESTER PRO - GIAO DIỆN ĐỒ HỌA (GUI)
==============================================================================
Phần mềm Desktop Đa Luồng (Multi-threading) tự động hóa:
1. Đọc danh sách tài khoản Hotmail/Outlook từ file .txt.
2. Quản lý số luồng song song (1 - 10 luồng) độc lập, tối ưu tốc độ thu hoạch cookie.
3. Tự động gửi OTP từ Muse.ai -> Đọc hòm thư DongVanFB -> Lấy mã -> Xác thực.
4. Bóc tách toàn bộ Cookie phiên (hatch_sess, hatch_gw, hatch_vml, hatch_native_auth_device...).
5. Đồng bộ trực tiếp vào MuseAI Studio (data/accounts.json) và xuất file text dự phòng.
==============================================================================
"""

from __future__ import annotations

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

# Đảm bảo UTF-8 cho Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Lấy mã OTP trên DongVanFB: số lần đọc hòm thư và số giây chờ giữa 2 lần (trang đôi khi phải đọc lại mới có thư)
OTP_READ_ATTEMPTS = 10
OTP_READ_INTERVAL = 5

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from config import CFG
    from store import Store
except ImportError:
    CFG = None
    Store = None

from DrissionPage import ChromiumPage, ChromiumOptions

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QColor, QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


def find_chrome_path() -> str:
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


def format_cookie_header(cookies: dict[str, str]) -> str:
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


# ============================================================================
# LỚP WORKER THREAD CHẠY TỪNG TÀI KHOẢN TRONG POOL
# ============================================================================
class LoginWorker(threading.Thread):
    def __init__(
        self,
        worker_id: int,
        task_queue: queue.Queue,
        controller: WorkerController,
        headless: bool,
        chrome_path: str,
        delay_between: int,
    ):
        super().__init__()
        self.worker_id = worker_id
        self.task_queue = task_queue
        self.controller = controller
        self.headless = headless
        self.chrome_path = chrome_path
        self.delay_between = delay_between
        self.profile_dir = os.path.join(tempfile.gettempdir(), f"muse_gui_worker_{worker_id}")
        self.port = 9310 + worker_id * 5
        self.daemon = True

    def run(self):
        co = ChromiumOptions()
        co.set_browser_path(self.chrome_path)
        co.set_user_data_path(self.profile_dir)
        co.set_local_port(self.port)
        co.set_argument("--no-first-run")
        co.set_argument("--no-default-browser-check")
        co.set_argument("--disable-blink-features=AutomationControlled")
        if self.headless:
            co.headless(True)
        else:
            co.set_argument("--window-size=1200,800")

        self.controller.log(f"[Luồng {self.worker_id}] Đang mở trình duyệt Chrome (Port {self.port})...")
        try:
            page = ChromiumPage(co)
            page.get("https://muse.ai")
            time.sleep(2)
            dongvan_tab = page.new_tab("https://dongvanfb.net/read_mail_box/")
            time.sleep(2)
            page.activate_tab(0)
        except Exception as e:
            self.controller.log(f"[Luồng {self.worker_id}] LỖI khởi động trình duyệt: {e}")
            return

        try:
            while self.controller.is_running:
                try:
                    item = self.task_queue.get(timeout=1.0)
                except queue.Empty:
                    break

                row_idx, acc_line = item
                email = acc_line.split("|")[0].strip()

                try:
                    self.process_single_account(page, dongvan_tab, row_idx, acc_line, email)
                except Exception as e:
                    self.controller.log(f"[Luồng {self.worker_id}] Lỗi xử lý {email}: {e}")
                    self.controller.update_status(row_idx, f"Lỗi: {e}", "#ef4444")
                    self.controller.record_result(False)
                finally:
                    self.task_queue.task_done()

                self.controller.log(f"[Luồng {self.worker_id}] Đã hoàn thành tác vụ cho {email}, sẵn sàng nhận tài khoản mới...")
                if self.delay_between > 0 and self.controller.is_running:
                    time.sleep(self.delay_between)

        finally:
            try:
                page.quit()
            except Exception:
                pass
            self.controller.log(f"[Luồng {self.worker_id}] Đã đóng trình duyệt.")

    @staticmethod
    def _find_dongvan_otp(dongvan_tab, email: str) -> str | None:
        """Dò bảng hòm thư DongVanFB: trả về mã OTP 6 số của thư mới (hôm nay) gửi cho đúng email, không có thì None."""
        rows = dongvan_tab.run_js("""
            let trs = Array.from(document.querySelectorAll('table tr'));
            return trs.map(r => Array.from(r.querySelectorAll('th, td')).map(c => c.innerText.trim()));
        """)
        if not rows or len(rows) <= 1:
            return None
        for row in rows[1:]:
            # Bỏ qua dòng đang tải dữ liệu
            if len(row) > 2 and row[2] == '...':
                continue
            # Bỏ qua nếu dòng này không thuộc về tài khoản hiện tại
            if len(row) > 0 and email.lower() not in row[0].lower():
                continue

            row_text = " ".join(row)
            time_col = row[3] if len(row) > 3 else ""
            # Kiểm tra xem thư này có phải thư mới nhận hôm nay hay không
            if not is_recent_dongvan_time(time_col):
                continue

            m = re.search(r'(?:use\s+code|login\s+code|mã\s+xác\s+nhận|code)\s*[:\-]?\s*(\d{6})', row_text, re.IGNORECASE)
            if not m:
                if len(row) >= 6 and re.search(r'^\d{6}', row[5]):
                    m = re.search(r'(\d{6})', row[5])
                elif re.search(r'\b\d{6}\b', row_text):
                    m = re.search(r'\b\d{6}\b', row_text)
            if m:
                return m.group(1)
        return None

    def process_single_account(self, page: ChromiumPage, dongvan_tab, row_idx: int, acc_line: str, email: str):
        if not self.controller.is_running:
            return

        self.controller.update_status(row_idx, "Đang vào Muse.ai...", "#38bdf8")
        self.controller.log(f"[Luồng {self.worker_id}] [{email}] Mở trang Muse.ai...")

        # 1. Reset Session trên Muse
        page.activate_tab(0)
        try:
            page.run_cdp("Network.clearBrowserCookies")
            page.run_cdp("Network.clearBrowserCache")
            page.run_cdp("Storage.clearDataForOrigin", origin="https://muse.ai", storageTypes="all")
        except Exception:
            pass
        try:
            page.run_js("""
                try {
                    localStorage.clear();
                    sessionStorage.clear();
                } catch(e) {}
            """)
        except Exception:
            pass

        page.get("https://muse.ai")
        time.sleep(3)

        email_inp = page.ele("@placeholder:Mobile number or email") or page.ele("tag:input[inputmode='email']")
        if not email_inp:
            login_link = page.ele("text:Log in") or page.ele("text:Sign in") or page.ele("tag:a@text():Log in")
            if login_link:
                try:
                    login_link.click()
                except Exception:
                    pass
                time.sleep(2)
                email_inp = page.ele("@placeholder:Mobile number or email")

        if not email_inp:
            self.controller.update_status(row_idx, "Không thấy ô Email", "#ef4444")
            self.controller.record_result(False)
            return

        email_inp.clear()
        email_inp.input(email)
        time.sleep(1)

        btn_continue = page.ele("text:Continue") or page.ele("tag:button@text():Continue")
        if not btn_continue:
            self.controller.update_status(row_idx, "Không thấy nút Continue", "#ef4444")
            self.controller.record_result(False)
            return

        btn_continue.click()
        self.controller.update_status(row_idx, "Đã gửi yêu cầu OTP...", "#f59e0b")
        # Đợi 4 giây cho Muse gửi email
        time.sleep(4)

        is_otp = page.run_js("""
            return !!(document.querySelector('input[autocomplete="one-time-code"]') || 
                      document.querySelector('input[aria-label="6-digit security code"]') ||
                      document.querySelector('input.sr-only'));
        """)
        if not is_otp:
            self.controller.update_status(row_idx, "Không hiện ô OTP", "#ef4444")
            self.controller.record_result(False)
            return

        # 2. Đọc OTP trên DongVanFB (chỉ chấp nhận thư mới gửi)
        self.controller.update_status(row_idx, "Đang lấy OTP DongVanFB...", "#fbbf24")
        self.controller.log(f"[Luồng {self.worker_id}] [{email}] Chuyển tab DongVanFB đọc hòm thư...")

        page.activate_tab(dongvan_tab)
        time.sleep(1)

        dongvan_tab.run_js("""
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

        # DongVanFB đôi khi phải đọc lại vài lần mới thấy thư OTP -> thử tối đa OTP_READ_ATTEMPTS lần,
        # mỗi lần bấm "Đọc hòm thư" rồi chờ OTP_READ_INTERVAL giây (vẫn dò bảng mỗi giây, thấy mã là dừng ngay).
        otp_code = None
        for attempt in range(1, OTP_READ_ATTEMPTS + 1):
            if not self.controller.is_running:
                break
            if attempt > 1:
                dongvan_tab.run_js("""
                    let btn = document.querySelector('.btn-buy-home') || Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Đọc hòm thư'));
                    if (btn) btn.click();
                """)
            self.controller.update_status(row_idx, f"Đang lấy OTP DongVanFB (lần {attempt}/{OTP_READ_ATTEMPTS})...", "#fbbf24")
            deadline = time.time() + OTP_READ_INTERVAL
            while time.time() < deadline and self.controller.is_running:
                time.sleep(1)
                otp_code = self._find_dongvan_otp(dongvan_tab, email)
                if otp_code:
                    break
            if otp_code:
                break
            self.controller.log(f"[Luồng {self.worker_id}] [{email}] Chưa thấy mã OTP (lần {attempt}/{OTP_READ_ATTEMPTS}), đọc lại hòm thư...")

        if not otp_code:
            self.controller.update_status(row_idx, f"Không lấy được OTP sau {OTP_READ_ATTEMPTS} lần", "#ef4444")
            self.controller.record_result(False)
            return

        self.controller.set_otp(row_idx, otp_code)
        self.controller.update_status(row_idx, f"Đang điền mã {otp_code}...", "#818cf8")
        self.controller.log(f"[Luồng {self.worker_id}] [{email}] Đã nhận mã OTP mới: {otp_code}. Điền vào Muse.ai...")

        # 3. Điền OTP vào Muse
        page.activate_tab(0)
        time.sleep(1)

        page.run_js("""
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
        next_btn = page.ele("text:Next")
        if next_btn and "disabled" not in str(next_btn.attr("class")).lower() and next_btn.attr("disabled") is None:
            try:
                next_btn.click()
            except Exception:
                pass

        # 4. Chờ đăng nhập và lấy cookie
        self.controller.update_status(row_idx, "Chờ xác nhận Muse...", "#a78bfa")
        login_ok = False
        all_cookies = {}
        cookies_exp = {}

        for _ in range(16):
            if not self.controller.is_running:
                return
            time.sleep(2)
            try:
                cdp = page.run_cdp("Storage.getCookies")
                clist = cdp.get("cookies", [])
                m_cookies = {c["name"]: c["value"] for c in clist if "muse.ai" in c.get("domain", "")}
                if "hatch_sess" in m_cookies:
                    login_ok = True
                    all_cookies = m_cookies
                    cookies_exp = {
                        c["name"]: int(float(c.get("expires", -1)))
                        for c in clist
                        if "muse.ai" in c.get("domain", "") and c.get("expires", 0) > 0
                    }
                    break
            except Exception:
                pass

        if not login_ok:
            self.controller.update_status(row_idx, "Xác nhận thất bại", "#ef4444")
            self.controller.record_result(False)
            return

        # 5. Chuyển vào trang làm việc để Muse kích hoạt hatch_gw (Gateway Token)
        self.controller.update_status(row_idx, "Đang nạp hatch_gw...", "#a78bfa")
        try:
            page.get("https://muse.ai/thread/new")
            for _ in range(6):
                time.sleep(1)
                cdp = page.run_cdp("Storage.getCookies")
                clist = cdp.get("cookies", [])
                for c in clist:
                    if "muse.ai" in c.get("domain", ""):
                        all_cookies[c["name"]] = c["value"]
                        if c.get("expires", 0) > 0:
                            cookies_exp[c["name"]] = int(float(c.get("expires")))
                if "hatch_gw" in all_cookies:
                    self.controller.log(f"[Luồng {self.worker_id}] [{email}] Đã nhận đầy đủ 4 cookie cốt lõi (kể cả hatch_gw).")
                    break
        except Exception:
            pass

        # 5. Tự động kích hoạt session để lấy hatch_vml và đánh thức VM
        try:
            from engine import MuseEngine
            if CFG:
                eng = MuseEngine(CFG)
                ren = eng.renew_session_http(all_cookies, cookies_exp, wake_vm=True)
                if ren and ren.get("cookies"):
                    all_cookies = ren["cookies"]
                    cookies_exp = ren.get("cookies_exp", cookies_exp)
                    self.controller.log(f"[Luồng {self.worker_id}] [{email}] Kích hoạt session cốt lõi hatch_vml thành công (VM: {ren.get('vm_state')}).")
        except Exception as e_ren:
            self.controller.log(f"[Luồng {self.worker_id}] [{email}] Ghi chú kích hoạt: {e_ren}")

        # 6. Lưu kết quả
        self.controller.save_account_success(row_idx, email, all_cookies, cookies_exp)


# ============================================================================
# BỘ ĐIỀU KHIỂN QUẢN LÝ TIẾN TRÌNH & THREAD-SAFE GIAO DIỆN
# ============================================================================
class WorkerController(QThread):
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(int, str, str)  # row, text, color
    otp_signal = pyqtSignal(int, str)          # row, otp
    cookie_signal = pyqtSignal(int, str)       # row, cookie info
    progress_signal = pyqtSignal(int, int, int, int)  # completed, total, success, failed
    finished_signal = pyqtSignal()

    def __init__(
        self,
        accounts: list[str],
        num_threads: int,
        headless: bool,
        save_to_store: bool,
        delay_between: int,
        tasks: list[tuple[int, str]] | None = None,
    ):
        super().__init__()
        self.accounts = accounts
        self.tasks = tasks if tasks is not None else [(idx, acc) for idx, acc in enumerate(accounts)]
        self.num_threads = num_threads
        self.headless = headless
        self.save_to_store = save_to_store
        self.delay_between = delay_between
        self.chrome_path = find_chrome_path()
        self.is_running = True
        self.lock = threading.RLock()

        self.total = len(self.tasks)
        self.completed = 0
        self.success = 0
        self.failed = 0

        self.store = Store(CFG) if (Store and CFG) else None
        self.out_dir = os.path.join(BASE_DIR, "data")
        os.makedirs(self.out_dir, exist_ok=True)
        self.export_txt_path = os.path.join(self.out_dir, "auto_login_success_cookies.txt")

    def run(self):
        self.log(f"Bắt đầu xử lý {self.total} tài khoản với {self.num_threads} luồng song song...")
        q = queue.Queue()
        for idx, acc in self.tasks:
            q.put((idx, acc))

        workers = []
        actual_threads = min(self.num_threads, self.total)
        for i in range(actual_threads):
            w = LoginWorker(
                worker_id=i + 1,
                task_queue=q,
                controller=self,
                headless=self.headless,
                chrome_path=self.chrome_path,
                delay_between=self.delay_between,
            )
            workers.append(w)
            w.start()

        # Đợi các luồng hoàn thành
        for w in workers:
            w.join()

        self.log("Hoàn thành toàn bộ danh sách tài khoản!")
        self.finished_signal.emit()

    def stop(self):
        self.is_running = False
        self.log("Đã gửi tín hiệu dừng tới tất cả các luồng...")

    def log(self, msg: str):
        now = datetime.now().strftime("%H:%M:%S")
        self.log_signal.emit(f"[{now}] {msg}")

    def update_status(self, row_idx: int, text: str, color: str):
        self.status_signal.emit(row_idx, text, color)

    def set_otp(self, row_idx: int, otp: str):
        self.otp_signal.emit(row_idx, otp)

    def record_result(self, is_success: bool):
        with self.lock:
            self.completed += 1
            if is_success:
                self.success += 1
            else:
                self.failed += 1
            self.progress_signal.emit(self.completed, self.total, self.success, self.failed)

    def _update_export_txt(self, email: str, cstr: str):
        """Cập nhật cookie vào file txt, nếu đã có email thì ghi đè chứ không thêm dòng mới."""
        lines = []
        found = False
        if os.path.isfile(self.export_txt_path):
            with open(self.export_txt_path, "r", encoding="utf-8", errors="ignore") as f:
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
        with open(self.export_txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def save_account_success(self, row_idx: int, email: str, cookies: dict, cookies_exp: dict):
        cstr = format_cookie_header(cookies)
        synced_studio = False

        # 1. Đồng bộ trực tiếp vào máy chủ MuseAI Studio đang chạy (Live API)
        if self.save_to_store and CFG:
            try:
                import requests
                url = f"http://127.0.0.1:{CFG.port}/admin/accounts"
                headers = {"Authorization": f"Bearer {CFG.api_key}"} if CFG.api_key else {}

                payload = {
                    "label": email,
                    "cookies": cookies,
                    "cookie_header": cstr,
                    "expires": {k: int(v) for k, v in cookies_exp.items() if v > 0}
                }
                r = requests.post(url, json=payload, headers=headers, timeout=5)
                if r.status_code == 200:
                    synced_studio = True
                    self.log(f"[✓] [{email}] Đã ghi đè / đồng bộ thành công vào MuseAI Studio Dashboard đang chạy!")
            except Exception:
                pass

        with self.lock:
            # 2. Luôn ghi đè cập nhật vào data/accounts.json
            if self.save_to_store and self.store:
                acc = self.store.add_account(cookies, label=email, cookies_exp=cookies_exp)
                if not synced_studio:
                    self.log(f"[✓] [{email}] Đã ghi đè trực tiếp vào data/accounts.json (ID: {acc['id']}).")

            # 3. Ghi đè vào file text dự phòng
            try:
                self._update_export_txt(email, cstr)
            except Exception:
                pass

            self.cookie_signal.emit(row_idx, f"{len(cookies)} cookies")
            self.update_status(row_idx, "✓ Thành công (Ghi đè)", "#10b981")

        # 4. Ghi nhận kết quả ra ngoài lock
        self.record_result(True)


# ============================================================================
# GIAO DIỆN CHÍNH (PYQT6 WINDOW)
# ============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MuseAI Cookie Harvester Pro - Tự Động Đăng Nhập Đa Luồng")
        self.resize(1150, 780)
        self.setMinimumSize(950, 650)
        self.controller: WorkerController | None = None
        self.accounts: list[str] = []

        self.apply_dark_theme()
        self.init_ui()
        self.load_default_file()

    def apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #0f172a;
            }
            QWidget {
                color: #e2e8f0;
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                font-size: 13px;
            }
            QGroupBox {
                border: 1px solid #334155;
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 15px;
                font-weight: bold;
                color: #38bdf8;
                background-color: #1e293b;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 5px;
            }
            QLineEdit, QSpinBox {
                background-color: #0f172a;
                border: 1px solid #475569;
                border-radius: 6px;
                padding: 6px 10px;
                color: #f8fafc;
            }
            QLineEdit:focus, QSpinBox:focus {
                border: 1px solid #38bdf8;
            }
            QPushButton {
                background-color: #334155;
                border: none;
                border-radius: 6px;
                padding: 7px 15px;
                font-weight: bold;
                color: #f8fafc;
            }
            QPushButton:hover {
                background-color: #475569;
            }
            QPushButton#btnStart {
                background-color: #10b981;
                color: #ffffff;
                font-size: 14px;
                padding: 8px 20px;
            }
            QPushButton#btnStart:hover {
                background-color: #059669;
            }
            QPushButton#btnStart:disabled {
                background-color: #064e3b;
                color: #64748b;
            }
            QPushButton#btnStop {
                background-color: #ef4444;
                color: #ffffff;
                font-size: 14px;
                padding: 8px 20px;
            }
            QPushButton#btnStop:hover {
                background-color: #dc2626;
            }
            QPushButton#btnStop:disabled {
                background-color: #450a0a;
                color: #64748b;
            }
            QPushButton#btnRetry {
                background-color: #f59e0b;
                color: #ffffff;
                font-size: 14px;
                padding: 8px 20px;
            }
            QPushButton#btnRetry:hover {
                background-color: #d97706;
            }
            QPushButton#btnRetry:disabled {
                background-color: #78350f;
                color: #64748b;
            }
            QTableWidget {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 8px;
                gridline-color: #334155;
                selection-background-color: #0284c7;
            }
            QHeaderView::section {
                background-color: #0f172a;
                color: #94a3b8;
                padding: 8px;
                font-weight: bold;
                border: none;
                border-right: 1px solid #334155;
                border-bottom: 1px solid #334155;
            }
            QProgressBar {
                border: 1px solid #334155;
                border-radius: 6px;
                text-align: center;
                background-color: #0f172a;
                color: #f8fafc;
                font-weight: bold;
                height: 22px;
            }
            QProgressBar::chunk {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0284c7, stop:1 #10b981);
                border-radius: 5px;
            }
            QTextEdit {
                background-color: #020617;
                border: 1px solid #334155;
                border-radius: 8px;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 12px;
                color: #38bdf8;
            }
            QCheckBox {
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 1px solid #475569;
                background-color: #0f172a;
            }
            QCheckBox::indicator:checked {
                background-color: #10b981;
                border-color: #10b981;
            }
        """)

    def init_ui(self):
        main_widget = QWidget()
        layout = QVBoxLayout(main_widget)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)

        # 1. BẢNG ĐIỀU KHIỂN CẤU HÌNH (CONTROL PANEL)
        ctrl_box = QGroupBox("⚙️ Cấu Hình Thu Hoạch Cookie Đa Luồng")
        ctrl_layout = QVBoxLayout(ctrl_box)

        # Hàng 1: Chọn file
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("File danh sách tài khoản:"))
        self.txt_file = QLineEdit()
        self.txt_file.setPlaceholderText("Chọn file .txt (mỗi tài khoản 1 dòng: email|pass|token...)")
        row1.addWidget(self.txt_file)

        btn_browse = QPushButton("📁 Chọn File...")
        btn_browse.clicked.connect(self.browse_file)
        row1.addWidget(btn_browse)
        ctrl_layout.addLayout(row1)

        # Hàng 2: Cài đặt luồng & tùy chọn
        row2 = QHBoxLayout()

        row2.addWidget(QLabel("Số luồng chạy song song:"))
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 10)
        self.spin_threads.setValue(3)
        self.spin_threads.setToolTip("Số trình duyệt chạy cùng lúc để tăng tốc độ lấy cookie")
        row2.addWidget(self.spin_threads)

        row2.addSpacing(15)
        row2.addWidget(QLabel("Nghỉ giữa tài khoản (giây):"))
        self.spin_delay = QSpinBox()
        self.spin_delay.setRange(0, 30)
        self.spin_delay.setValue(2)
        row2.addWidget(self.spin_delay)

        row2.addSpacing(15)
        self.chk_headless = QCheckBox("Chạy ngầm (Ẩn cửa sổ Chrome)")
        self.chk_headless.setChecked(False)
        self.chk_headless.setToolTip("Tích vào để không làm phiền màn hình làm việc")
        row2.addWidget(self.chk_headless)

        self.chk_save_store = QCheckBox("Tự động nạp vào MuseAI Studio")
        self.chk_save_store.setChecked(True)
        self.chk_save_store.setToolTip("Cập nhật trực tiếp vào data/accounts.json")
        row2.addWidget(self.chk_save_store)

        row2.addStretch()
        ctrl_layout.addLayout(row2)

        # Hàng 3: Các nút hành động
        row3 = QHBoxLayout()
        self.btn_start = QPushButton("▶ BẮT ĐẦU CHẠY")
        self.btn_start.setObjectName("btnStart")
        self.btn_start.clicked.connect(self.start_process)
        row3.addWidget(self.btn_start)

        self.btn_stop = QPushButton("⏹ DỪNG LẠI")
        self.btn_stop.setObjectName("btnStop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_process)
        row3.addWidget(self.btn_stop)

        self.btn_retry = QPushButton("🔁 RETRY LỖI")
        self.btn_retry.setObjectName("btnRetry")
        self.btn_retry.setToolTip("Quét và chạy lại các tài khoản bị lỗi hoặc chưa thành công")
        self.btn_retry.clicked.connect(self.retry_failed)
        row3.addWidget(self.btn_retry)

        row3.addSpacing(15)
        btn_open_folder = QPushButton("📂 Mở Thư Mục Cookie")
        btn_open_folder.clicked.connect(self.open_cookie_folder)
        row3.addWidget(btn_open_folder)

        btn_reload = QPushButton("🔄 Tải Lại File")
        btn_reload.clicked.connect(self.reload_accounts)
        row3.addWidget(btn_reload)

        row3.addStretch()
        ctrl_layout.addLayout(row3)
        layout.addWidget(ctrl_box)

        # 2. KHU VỰC THỐNG KÊ (STATS & PROGRESS)
        stats_box = QHBoxLayout()
        self.lbl_stats = QLabel("Tổng: 0 | Hoàn thành: 0 | Thành công: 0 | Thất bại: 0")
        self.lbl_stats.setStyleSheet("font-weight: bold; color: #f8fafc; font-size: 13.5px;")
        stats_box.addWidget(self.lbl_stats)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        stats_box.addWidget(self.progress_bar)
        layout.addLayout(stats_box)

        # 3. BẢNG TRẠNG THÁI TÀI KHOẢN (TABLE)
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels([
            "STT", "Email Tài Khoản", "Trạng Thái Xử Lý", "Mã OTP", "Cookies", "Thời Gian"
        ])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet("alternate-background-color: #162032;")
        layout.addWidget(self.table, stretch=3)

        # 4. NHẬT KÝ CHI TIẾT (LIVE LOG)
        log_box = QGroupBox("📋 Nhật Ký Hoạt Động Thời Gian Thực")
        log_layout = QVBoxLayout(log_box)
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        log_layout.addWidget(self.txt_log)
        layout.addWidget(log_box, stretch=2)

        self.setCentralWidget(main_widget)

    def load_default_file(self):
        default_p = os.path.join(BASE_DIR, "24-MUSE.txt")
        if os.path.isfile(default_p):
            self.txt_file.setText(default_p)
            self.reload_accounts()

    def browse_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Chọn file danh sách tài khoản", BASE_DIR, "Text files (*.txt);;All files (*.*)")
        if path:
            self.txt_file.setText(path)
            self.reload_accounts()

    def reload_accounts(self):
        file_path = self.txt_file.text().strip()
        if not file_path or not os.path.isfile(file_path):
            return

        self.accounts.clear()
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    self.accounts.append(line)

        self.table.setRowCount(len(self.accounts))
        for row, acc in enumerate(self.accounts):
            email = acc.split("|")[0].strip()
            item_stt = QTableWidgetItem(str(row + 1))
            item_stt.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_email = QTableWidgetItem(email)
            item_status = QTableWidgetItem("Sẵn sàng")
            item_status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_status.setForeground(QColor("#94a3b8"))
            item_otp = QTableWidgetItem("—")
            item_otp.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_cookies = QTableWidgetItem("—")
            item_cookies.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_time = QTableWidgetItem("—")
            item_time.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            self.table.setItem(row, 0, item_stt)
            self.table.setItem(row, 1, item_email)
            self.table.setItem(row, 2, item_status)
            self.table.setItem(row, 3, item_otp)
            self.table.setItem(row, 4, item_cookies)
            self.table.setItem(row, 5, item_time)

        self.lbl_stats.setText(f"Tổng: {len(self.accounts)} | Hoàn thành: 0 | Thành công: 0 | Thất bại: 0")
        self.progress_bar.setValue(0)
        self.log(f"Đã tải {len(self.accounts)} tài khoản từ: {os.path.basename(file_path)}")

    def log(self, msg: str):
        self.txt_log.append(msg)
        self.txt_log.moveCursor(self.txt_log.textCursor().MoveOperation.End)

    def start_process(self):
        if not self.accounts:
            QMessageBox.warning(self, "Cảnh báo", "Vui lòng chọn file chứa danh sách tài khoản hợp lệ!")
            return

        self.btn_start.setEnabled(False)
        self.btn_retry.setEnabled(False)
        self.btn_stop.setEnabled(True)
        num_threads = self.spin_threads.value()
        delay = self.spin_delay.value()
        headless = self.chk_headless.isChecked()
        save_store = self.chk_save_store.isChecked()

        self.controller = WorkerController(
            accounts=self.accounts,
            num_threads=num_threads,
            headless=headless,
            save_to_store=save_store,
            delay_between=delay,
        )
        self.controller.log_signal.connect(self.log)
        self.controller.status_signal.connect(self.on_status_updated)
        self.controller.otp_signal.connect(self.on_otp_received)
        self.controller.cookie_signal.connect(self.on_cookies_saved)
        self.controller.progress_signal.connect(self.on_progress_updated)
        self.controller.finished_signal.connect(self.on_finished)
        self.controller.start()

    def get_failed_rows(self) -> list[int]:
        """Tìm các dòng bị lỗi hoặc chưa thành công để chạy lại."""
        failed = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 2)
            st = item.text().strip() if item else ""
            # Dòng đã thành công thì bỏ qua
            if "thành công" in st.lower() or st.startswith("✓"):
                continue
            # Dòng chưa chạy bao giờ thì bỏ qua (nếu đang tìm lỗi cụ thể)
            if st == "Sẵn sàng":
                continue
            failed.append(row)
        return failed

    def retry_failed(self):
        """Chạy lại các dòng bị lỗi hoặc chưa thành công."""
        if not self.accounts:
            QMessageBox.warning(self, "Cảnh báo", "Vui lòng chọn file chứa danh sách tài khoản hợp lệ!")
            return

        failed_rows = self.get_failed_rows()
        if not failed_rows:
            # Kiểm tra xem có dòng nào đang ở trạng thái 'Sẵn sàng' (chưa chạy) hay không
            ready_rows = [
                r for r in range(self.table.rowCount())
                if self.table.item(r, 2) and self.table.item(r, 2).text().strip() == "Sẵn sàng"
            ]
            if ready_rows:
                reply = QMessageBox.question(
                    self,
                    "Thông báo",
                    f"Hiện không có dòng nào báo lỗi, nhưng còn {len(ready_rows)} tài khoản đang ở trạng thái 'Sẵn sàng'.\n"
                    "Bạn có muốn tiếp tục chạy các tài khoản này không?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    failed_rows = ready_rows
                else:
                    return
            else:
                QMessageBox.information(
                    self,
                    "Thông báo",
                    "Tuyệt vời! Tất cả tài khoản trong danh sách đều đã lấy cookie thành công, không có dòng lỗi nào!",
                )
                return

        reply = QMessageBox.question(
            self,
            "Xác nhận Retry Lỗi",
            f"Tìm thấy {len(failed_rows)} tài khoản báo lỗi / cần chạy lại.\n"
            "Bạn có muốn bắt đầu chạy lại các tài khoản này ngay bây giờ?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.btn_start.setEnabled(False)
        self.btn_retry.setEnabled(False)
        self.btn_stop.setEnabled(True)

        num_threads = self.spin_threads.value()
        delay = self.spin_delay.value()
        headless = self.chk_headless.isChecked()
        save_store = self.chk_save_store.isChecked()

        # Đặt lại trạng thái hiển thị cho các dòng được chọn chạy lại
        for r in failed_rows:
            item_st = self.table.item(r, 2)
            if item_st:
                item_st.setText("Chờ thử lại...")
                item_st.setForeground(QColor("#fbbf24"))

        tasks = [(r, self.accounts[r]) for r in failed_rows]
        self.lbl_stats.setText(f"Retry: {len(tasks)} lỗi | Hoàn thành: 0 | Thành công: 0 | Thất bại: 0")
        self.progress_bar.setValue(0)
        self.log(f"[*] Bắt đầu Retry lại {len(tasks)} tài khoản bị lỗi với {num_threads} luồng song song...")

        self.controller = WorkerController(
            accounts=self.accounts,
            num_threads=num_threads,
            headless=headless,
            save_to_store=save_store,
            delay_between=delay,
            tasks=tasks,
        )
        self.controller.log_signal.connect(self.log)
        self.controller.status_signal.connect(self.on_status_updated)
        self.controller.otp_signal.connect(self.on_otp_received)
        self.controller.cookie_signal.connect(self.on_cookies_saved)
        self.controller.progress_signal.connect(self.on_progress_updated)
        self.controller.finished_signal.connect(self.on_finished)
        self.controller.start()

    def stop_process(self):
        if self.controller and self.controller.isRunning():
            self.controller.stop()
            self.btn_stop.setEnabled(False)
            self.log("[!] Đang dừng các luồng... vui lòng đợi trong giây lát.")

    def on_status_updated(self, row: int, text: str, color_hex: str):
        item = self.table.item(row, 2)
        if item:
            item.setText(text)
            item.setForeground(QColor(color_hex))
            self.table.scrollToItem(item)

    def on_otp_received(self, row: int, otp: str):
        item = self.table.item(row, 3)
        if item:
            item.setText(otp)
            item.setForeground(QColor("#38bdf8"))
            font = item.font()
            font.setBold(True)
            item.setFont(font)

    def on_cookies_saved(self, row: int, info: str):
        item_c = self.table.item(row, 4)
        if item_c:
            item_c.setText(info)
            item_c.setForeground(QColor("#10b981"))

        item_t = self.table.item(row, 5)
        if item_t:
            item_t.setText(datetime.now().strftime("%H:%M:%S"))

    def on_progress_updated(self, completed: int, total: int, success: int, failed: int):
        pct = int((completed / total) * 100) if total > 0 else 0
        self.progress_bar.setValue(pct)
        self.lbl_stats.setText(f"Tổng: {total} | Hoàn thành: {completed} | Thành công: {success} | Thất bại: {failed}")

    def on_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_retry.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.log(">>> TIẾN TRÌNH ĐÃ HOÀN TẤT <<<")
        QMessageBox.information(
            self,
            "Hoàn tất",
            f"Đã xử lý xong toàn bộ danh sách!\nThành công: {self.controller.success}/{self.controller.total}\n"
            f"Cookies đã được cập nhật vào data/accounts.json và xuất file data/auto_login_success_cookies.txt",
        )

    def open_cookie_folder(self):
        folder = os.path.join(BASE_DIR, "data")
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
