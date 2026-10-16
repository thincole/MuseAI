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
from datetime import datetime, timedelta

# Đảm bảo UTF-8 cho Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Lấy mã OTP trên DongVanFB: số lần đọc hòm thư và số giây chờ giữa 2 lần (trang đôi khi phải đọc lại mới có thư)
DEAD_STATUS = "ACC CHẾT (Muse is disconnected)"
CHECKPOINT_STATUS = "TK LỖI (Muse chuyển sang Instagram)"
OTP_READ_ATTEMPTS = 10
OTP_READ_INTERVAL = 5
MANUAL_CAPTCHA_WAIT_SEC = 600  # Dừng tối đa 10 phút (600 giây) chờ người dùng nhập Captcha / xác minh bằng tay

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
    QComboBox,
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


def parse_dongvan_time(time_str: str):
    """Cột thời gian DongVanFB 'HH:MM - D/M/YYYY' -> datetime (None nếu không đọc được)."""
    try:
        m = re.search(r'(\d{1,2}):(\d{2})\s*-\s*(\d{1,2})/(\d{1,2})/(\d{4})', time_str or "")
        if not m:
            return None
        h, mi, d, mo, y = map(int, m.groups())
        return datetime(y, mo, d, h, mi)
    except Exception:  # noqa: BLE001
        return None


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
# PROXY CỐ ĐỊNH CHO TỪNG LUỒNG (HomeProxy): mỗi luồng đăng nhập 1 IP riêng, không dùng IP thật của máy
# ============================================================================
PROXY_SRC_APP = "app"            # theo "Nguồn Proxy" trong cài đặt MuseAI Studio
PROXY_SRC_HOMEPROXY = "homeproxy"
PROXY_SRC_WEBSHARE = "webshare"


def _proxy_pool_for(pm, source: str, webshare_key: str, log=None) -> list[str]:
    """Danh sách proxy theo nguồn đã chọn trong công cụ. Nguồn trùng với nguồn đang chạy của MuseAI Studio thì dùng chung
    danh sách (có cache); nguồn khác thì chỉ lấy để dùng, KHÔNG ghi đè cache/cài đặt của MuseAI Studio."""
    app_src = pm.get_provider()
    src = app_src if source in ("", PROXY_SRC_APP) else source
    if src == app_src:
        return pm.get_cached_proxy_pool() or pm.load_proxies_from_thinaptm()
    if src == PROXY_SRC_WEBSHARE:
        key = (webshare_key or pm.get_webshare_key()).strip()
        if not key:
            if log:
                log("Chưa có API key Webshare -> không có proxy.")
            return []
        lst = pm.fetch_proxies_from_webshare(key)
        if log:
            log(f"Webshare: lấy được {len(lst)} proxy.")
        return lst
    lst = pm.fetch_proxies_from_homeproxy_api(pm.HOMEPROXY_TOKEN or pm.DEFAULT_HOMEPROXY_TOKEN)
    if log:
        log(f"HomeProxy: lấy được {len(lst)} proxy.")
    return lst


def pick_worker_proxies(n: int, log=None, source: str = PROXY_SRC_APP, webshare_key: str = "") -> list[str | None]:
    """Chọn n proxy KHÁC NHAU còn sống từ nguồn proxy đã chọn (HomeProxy / Webshare / theo cài đặt MuseAI; ưu tiên proxy ít TK nhất).
    Trả về list độ dài n; luồng nào không kiếm được proxy thì None (chạy IP thật)."""
    out: list[str | None] = [None] * n
    try:
        import proxy_manager as pm
        if not pm.is_proxy_enabled():
            if log:
                log("Proxy đang TẮT trong cài đặt phần mềm (Không fake IP) -> các luồng chạy IP thật.")
            return out
        pool = _proxy_pool_for(pm, source, webshare_key, log)
        if not pool:
            if log:
                log("Không có proxy nào trong danh sách proxy đã chọn -> các luồng chạy IP thật.")
            return out
        try:
            order = pm._least_loaded_first(pool, pm._proxy_loads(pool))
        except Exception:  # noqa: BLE001
            order = list(pool)
        k = 0
        for cand in order:
            if k >= n:
                break
            ok, ip = pm.test_proxy(cand)
            if ok:
                out[k] = cand
                if log:
                    log(f"[Luồng {k + 1}] dùng proxy cố định IP {ip}")
                k += 1
        if k < n and log:
            log(f"Chỉ có {k}/{n} proxy còn sống: các luồng còn lại chạy IP thật.")
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"Không chọn được proxy ({type(exc).__name__}: {exc}) -> chạy IP thật.")
    return out


# ============================================================================
# NGUỒN ĐỌC OTP THỨ 2: hòm thư tạm https://mail.nguyenmaucole.io.vn/ (cho email @nguyenmaucole.io.vn)
# Trang này chỉ là giao diện; dữ liệu lấy qua API JSON (không cần mở trình duyệt, không phụ thuộc DongVanFB):
#   GET https://tempmail-backend.thincole.workers.dev/api/emails?address=<email>  ->  [{id, sender, subject, body_html, received_at}, ...]
# ============================================================================
MAILAPI_DOMAIN = "@nguyenmaucole.io.vn"
MAILAPI_URL = "https://tempmail-backend.thincole.workers.dev/api/emails?address="
MAILAPI_ATTEMPTS = 12      # số lần hỏi API
MAILAPI_INTERVAL = 5       # giây giữa 2 lần hỏi


def is_mailapi_email(email: str) -> bool:
    return str(email or "").strip().lower().endswith(MAILAPI_DOMAIN)


def mailapi_fetch(email: str, timeout: float = 15.0) -> list[dict] | None:
    """Danh sách thư của hòm thư (mới nhất trước) hoặc None nếu không gọi được API."""
    import urllib.parse
    import urllib.request
    url = MAILAPI_URL + urllib.parse.quote(email.strip().lower())
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return data if isinstance(data, list) else None
    except Exception:  # noqa: BLE001
        return None


def mailapi_extract_otp(mail: dict) -> str | None:
    """Bóc mã 6 số từ 1 thư (tiêu đề + nội dung), bỏ qua mã màu CSS (#123456) và phần <style>."""
    subject = str(mail.get("subject") or "")
    body = str(mail.get("body_html") or mail.get("body") or "")
    body = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"&nbsp;|&#160;", " ", body)
    text = re.sub(r"#[0-9a-fA-F]{3,8}\b", " ", subject + " " + body)
    m = re.search(r"(?:use\s+code|login\s+code|security\s+code|mã\s+xác\s+nhận|mã|code)\D{0,40}?(\d{6})\b", text, re.IGNORECASE)
    if not m:
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    return m.group(1) if m else None


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
        proxy_str: str | None = None,
        use_fingerprint: bool = True,
        manual_captcha: bool = True,
    ):
        super().__init__()
        self.use_fingerprint = use_fingerprint
        self.manual_captcha = manual_captcha
        self.worker_id = worker_id
        self.task_queue = task_queue
        self.controller = controller
        self.headless = headless
        self.chrome_path = chrome_path
        self.delay_between = delay_between
        self.proxy_str = proxy_str
        self.profile_dir = os.path.join(tempfile.gettempdir(), f"muse_gui_worker_{worker_id}")
        self.port = 9310 + worker_id * 5
        self.daemon = True

    def _build_options(self, prof: dict | None = None) -> "ChromiumOptions":
        co = ChromiumOptions()
        co.set_browser_path(self.chrome_path)
        co.set_user_data_path(self.profile_dir)
        co.set_local_port(self.port)
        co.set_argument("--no-first-run")
        co.set_argument("--no-default-browser-check")
        co.set_argument("--disable-blink-features=AutomationControlled")
        if self.proxy_str:
            try:
                import proxy_manager as pm
                fport = pm.start_local_forwarder(self.proxy_str)
                co.set_argument(f"--proxy-server=http://127.0.0.1:{fport}")
                # Trang đọc hòm thư (DongVanFB) đi thẳng, không qua proxy: nhanh và tránh proxy chặn làm không đọc được OTP
                co.set_argument("--proxy-bypass-list=dongvanfb.net;*.dongvanfb.net;localhost;127.0.0.1")
            except Exception as exc:  # noqa: BLE001
                self.controller.log(f"[Luồng {self.worker_id}] Không bật được proxy ({exc}) -> chạy IP thật")
        if prof:
            import fingerprint as fpm
            for a in fpm.chrome_args(prof):          # cửa sổ / tỉ lệ hiển thị / ngôn ngữ theo hồ sơ ngẫu nhiên
                co.set_argument(a)
        elif not self.headless:
            co.set_argument("--window-size=1200,800")
        if self.headless:
            co.headless(True)
        return co

    def _wipe_profile(self):
        """Xoá sạch hồ sơ Chrome của luồng (cookie, cache, localStorage, lịch sử...) để tài khoản kế tiếp bắt đầu từ trắng."""
        for _ in range(5):
            try:
                shutil.rmtree(self.profile_dir, ignore_errors=True)
                if not os.path.exists(self.profile_dir):
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)

    def _launch_browser(self):
        """Mở Chrome MỚI với hồ sơ trắng (cùng proxy cố định của luồng) + tab Muse + tab DongVanFB."""
        self._wipe_profile()
        prof = None
        if self.use_fingerprint:
            try:
                import fingerprint as fpm
                prof = fpm.make_profile()
            except Exception as exc:  # noqa: BLE001
                self.controller.log(f"[Luồng {self.worker_id}] Không tạo được hồ sơ trình duyệt ({exc}) -> dùng mặc định")
        co = self._build_options(prof)
        page = ChromiumPage(co)
        try:
            if prof:
                import fingerprint as fpm
                fpm.apply_profile(page, prof)
                self.controller.log(
                    f"[Luồng {self.worker_id}] Hồ sơ trình duyệt mới: màn hình {prof['screen'][0]}x{prof['screen'][1]}, "
                    f"cửa sổ {prof['window'][0]}x{prof['window'][1]}, DPR {prof['dpr']}, {prof['cores']} nhân/{prof['memory']}GB, "
                    f"GPU {prof['gpu_renderer'][7:45]}, ngôn ngữ {prof['lang']}")
            page.get("https://muse.ai")
            time.sleep(2)
            dongvan_tab = page.new_tab("https://dongvanfb.net/read_mail_box/")
            time.sleep(2)
            page.activate_tab(0)
        except Exception:
            try:
                page.quit()
            except Exception:  # noqa: BLE001
                pass
            raise
        return page, dongvan_tab

    def _close_browser(self, page):
        """Tắt hẳn Chrome của luồng rồi xoá hồ sơ (xoá hết cookie)."""
        try:
            page.quit()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
        self._wipe_profile()

    def run(self):
        proxy_note = " qua proxy cố định" if self.proxy_str else " (IP thật)"
        try:
            while self.controller.is_running:
                try:
                    item = self.task_queue.get(timeout=1.0)
                except queue.Empty:
                    break

                row_idx, acc_line = item
                email = acc_line.split("|")[0].strip()
                page = None
                try:
                    # MỖI TÀI KHOẢN = 1 TRÌNH DUYỆT MỚI: mở Chrome hồ sơ trắng -> làm xong -> tắt và xoá hết cookie
                    self.controller.log(f"[Luồng {self.worker_id}] [{email}] Mở Chrome mới (hồ sơ trắng, Port {self.port}){proxy_note}...")
                    page, dongvan_tab = self._launch_browser()
                    self.process_single_account(page, dongvan_tab, row_idx, acc_line, email)
                except Exception as e:
                    self.controller.log(f"[Luồng {self.worker_id}] Lỗi xử lý {email}: {e}")
                    self.controller.update_status(row_idx, f"Lỗi: {e}", "#ef4444")
                    self.controller.record_result(False)
                finally:
                    if page is not None:
                        self._close_browser(page)
                        self.controller.log(f"[Luồng {self.worker_id}] [{email}] Đã tắt Chrome và xoá toàn bộ cookie/hồ sơ.")
                    self.task_queue.task_done()

                self.controller.log(f"[Luồng {self.worker_id}] Đã hoàn thành tác vụ cho {email}, sẵn sàng nhận tài khoản mới...")
                if self.delay_between > 0 and self.controller.is_running:
                    time.sleep(self.delay_between)
        finally:
            self._wipe_profile()
            self.controller.log(f"[Luồng {self.worker_id}] Kết thúc luồng.")

    @staticmethod
    def _find_dongvan_otp(dongvan_tab, email: str, not_before=None, exclude=None) -> str | None:
        """Dò bảng hòm thư DongVanFB: trả về mã OTP 6 số của thư MỚI NHẤT gửi cho đúng email.
        - not_before: chỉ nhận thư có giờ >= mốc này (giờ bấm 'Continue' trên Muse trừ ~2 phút dung sai). Trước đây chỉ lọc
          'trong 1 giờ gần đây' nên lấy nhầm mã của lần đăng nhập trước (còn nằm trong bảng) khi thư mới chưa về.
        - exclude: tập mã đã dùng/đã thấy ở lần trước của email này -> không nhận lại."""
        exclude = exclude or set()
        rows = dongvan_tab.run_js("""
            let trs = Array.from(document.querySelectorAll('table tr'));
            return trs.map(r => Array.from(r.querySelectorAll('th, td')).map(c => c.innerText.trim()));
        """)
        if not rows or len(rows) <= 1:
            return None
        best = None   # (thời gian, mã)
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
            mail_dt = parse_dongvan_time(time_col)
            if not_before is not None and mail_dt is not None and mail_dt < not_before:
                continue   # thư cũ (trước lần yêu cầu mã này)

            m = re.search(r'(?:use\s+code|login\s+code|mã\s+xác\s+nhận|code)\s*[:\-]?\s*(\d{6})', row_text, re.IGNORECASE)
            if not m:
                if len(row) >= 6 and re.search(r'^\d{6}', row[5]):
                    m = re.search(r'(\d{6})', row[5])
                elif re.search(r'\b\d{6}\b', row_text):
                    m = re.search(r'\b\d{6}\b', row_text)
            if not m:
                continue
            code = m.group(1)
            if code in exclude:
                continue   # mã đã dùng ở lần trước
            key = mail_dt or datetime.min
            if best is None or key > best[0]:
                best = (key, code)
        return best[1] if best else None

    @staticmethod
    def _meta_checkpoint(page) -> bool:
        """Trang bị Meta chuyển sang đăng nhập Instagram / vòng chuyển hướng xác minh (redirect-cycle) -> True."""
        try:
            url = str(page.url or "").lower()
        except Exception:  # noqa: BLE001
            return False
        return ("instagram.com" in url) or ("redirect-cycle" in url)

    def _fail_checkpoint(self, row_idx: int, email: str):
        """Muse/Meta chuyển sang instagram.com: báo TK LỖI ngay, ghi email vào file riêng, KHÔNG coi là TK chết; luồng tắt Chrome và sang TK kế."""
        self.controller.log(f"[Luồng {self.worker_id}] [{email}] Muse chuyển sang instagram.com -> báo TK LỖI, bỏ qua TK này (sang tài khoản kế tiếp).")
        self.controller.update_status(row_idx, CHECKPOINT_STATUS, "#ef4444")
        self.controller.mark_checkpoint(email)
        self.controller.record_result(False)

    @staticmethod
    def _meta_manual_checkpoint(page) -> bool:
        """Meta/Muse đòi xác minh 'Confirm you're human' hoặc hiện Captcha (auth.meta.com/checkpoints/..., recaptcha/hcaptcha/arkose...)."""
        try:
            url = str(page.url or "").lower()
        except Exception:  # noqa: BLE001
            return False
        if any(k in url for k in ("auth.meta.com/checkpoints", "/checkpoints/", "checkpoint", "challenge", "captcha")):
            return True
        try:
            has_captcha = page.run_js("""
                var t = (document.body && document.body.innerText) || '';
                if (/confirm you(?:'|\\u2019)?re human|confirm you are human|verify you(?:'|\\u2019)?re human|verify you are human|security check|kiểm tra bảo mật|xác minh bạn là con người/i.test(t)) return true;
                if (document.querySelector('iframe[src*="captcha" i], iframe[src*="recaptcha" i], iframe[src*="arkose" i], iframe[src*="hcaptcha" i], iframe[src*="funcaptcha" i], [id*="captcha" i], [class*="captcha" i]')) return true;
                return false;
            """)
            return bool(has_captcha)
        except Exception:  # noqa: BLE001
            return False

    def _fail_verify(self, row_idx: int, email: str):
        """Meta bắt xác minh (Confirm you're human) khi tắt chế độ chờ tay: báo lỗi xác minh, tắt Chrome và sang TK kế."""
        self.controller.log(f"[Luồng {self.worker_id}] [{email}] Meta bắt xác minh (Confirm you're human) -> bỏ qua TK này (không lưu cookie).")
        self.controller.update_status(row_idx, "Cần xác minh Captcha tay", "#ef4444")
        self.controller.record_result(False)

    def _wait_manual_captcha(
        self,
        page,
        row_idx: int,
        email: str,
        wait_sec: int = MANUAL_CAPTCHA_WAIT_SEC,
        wait_for_otp_box: bool = False,
    ) -> tuple[bool, dict, dict]:
        """Tạm dừng tối đa wait_sec giây (mặc định 10 phút = 600s) để người dùng nhập Captcha / xác minh bằng tay trên trình duyệt."""
        try:
            page.activate_tab(0)
            page.run_cdp("Page.bringToFront")
        except Exception:  # noqa: BLE001
            pass

        mins = wait_sec // 60
        self.controller.log(
            f"[Luồng {self.worker_id}] [{email}] 🖐 Cần xác minh Captcha bằng tay! "
            f"Tạm dừng tối đa {mins} phút ({wait_sec}s) để bạn thao tác trên cửa sổ Chrome (Luồng {self.worker_id})..."
        )

        deadline = time.time() + wait_sec
        while time.time() < deadline:
            if not self.controller.is_running:
                return False, {}, {}

            rem = max(0, int(deadline - time.time()))
            self.controller.update_status(
                row_idx,
                f"🖐 Chờ nhập Captcha tay ({rem // 60:02d}:{rem % 60:02d})...",
                "#f59e0b",
            )

            if self._meta_checkpoint(page):
                self._fail_checkpoint(row_idx, email)
                return False, {}, {}

            if wait_for_otp_box:
                try:
                    is_otp = page.run_js("""
                        return !!(document.querySelector('input[autocomplete="one-time-code"]') || 
                                  document.querySelector('input[aria-label="6-digit security code"]') ||
                                  document.querySelector('input.sr-only'));
                    """)
                    if is_otp and not self._meta_manual_checkpoint(page):
                        self.controller.log(f"[Luồng {self.worker_id}] [{email}] ✓ Đã qua bước xác minh tay! Tiếp tục lấy mã OTP...")
                        return True, {}, {}
                except Exception:  # noqa: BLE001
                    pass

            try:
                cdp = page.run_cdp("Storage.getCookies")
                clist = cdp.get("cookies", [])
                m_cookies = {c["name"]: c["value"] for c in clist if "muse.ai" in c.get("domain", "")}
                if "hatch_sess" in m_cookies and not self._meta_manual_checkpoint(page):
                    cookies_exp = {
                        c["name"]: int(float(c.get("expires", -1)))
                        for c in clist
                        if "muse.ai" in c.get("domain", "") and c.get("expires", 0) > 0
                    }
                    self.controller.log(f"[Luồng {self.worker_id}] [{email}] ✓ Người dùng đã xác minh Captcha thành công! Tiếp tục nạp cookie...")
                    return True, m_cookies, cookies_exp
            except Exception:  # noqa: BLE001
                pass

            time.sleep(1)

        self.controller.log(
            f"[Luồng {self.worker_id}] [{email}] ⏰ Quá {mins} phút chờ nhập Captcha bằng tay -> tạm bỏ qua tài khoản này (có thể bấm Retry Lỗi để chạy lại)."
        )
        self.controller.update_status(row_idx, "Quá 10p chờ Captcha tay", "#ef4444")
        self.controller.record_result(False)
        return False, {}, {}

    @staticmethod
    def _muse_disconnected(page, wait_sec: float = 6.0) -> bool:
        """Đợi tối đa wait_sec giây: trang hiện chữ 'Muse is disconnected' -> True; thấy ô soạn tin (TK bình thường) -> False."""
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            try:
                state = page.run_js(
                    "var t=(document.body&&document.body.innerText)||'';"
                    "if(/Muse is disconnected|disconnected Muse to protect/i.test(t))return 'dead';"
                    "if(document.querySelector('textarea'))return 'ok';return '';")
            except Exception:  # noqa: BLE001
                state = ""
            if state == "dead":
                return True
            if state == "ok":
                return False
            time.sleep(0.5)
        return False

    def _otp_from_mailapi(self, row_idx: int, email: str, seen: set[str] | None) -> str | None:
        """Hỏi API hòm thư tạm mỗi MAILAPI_INTERVAL giây (tối đa MAILAPI_ATTEMPTS lần), chỉ nhận thư MỚI có mã 6 số."""
        self.controller.log(f"[Luồng {self.worker_id}] [{email}] Đọc OTP qua mail.nguyenmaucole.io.vn (API)...")
        for attempt in range(1, MAILAPI_ATTEMPTS + 1):
            if not self.controller.is_running:
                return None
            self.controller.update_status(row_idx, f"Đang lấy OTP mail.nguyenmaucole (lần {attempt}/{MAILAPI_ATTEMPTS})...", "#fbbf24")
            mails = mailapi_fetch(email)
            if mails is None:
                self.controller.log(f"[Luồng {self.worker_id}] [{email}] Không gọi được API hòm thư (lần {attempt}), thử lại...")
            else:
                for mail in mails:               # mới nhất trước
                    if seen is not None and str(mail.get("id")) in seen:
                        continue
                    code = mailapi_extract_otp(mail)
                    if code:
                        return code
            time.sleep(MAILAPI_INTERVAL)
        return None

    def _otp_from_dongvan(self, page, dongvan_tab, row_idx: int, acc_line: str, email: str) -> str | None:
        # Đọc OTP trên DongVanFB (chỉ chấp nhận thư mới gửi)
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
                otp_code = self._find_dongvan_otp(dongvan_tab, email, getattr(self, '_otp_not_before', None),
                                                  self.controller.used_otps(email))
                if otp_code:
                    break
            if otp_code:
                break
            self.controller.log(f"[Luồng {self.worker_id}] [{email}] Chưa thấy mã OTP (lần {attempt}/{OTP_READ_ATTEMPTS}), đọc lại hòm thư...")

        return otp_code

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
        if self._meta_checkpoint(page):
            self._fail_checkpoint(row_idx, email)
            return

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

        # Email @nguyenmaucole.io.vn: chụp danh sách thư ĐANG CÓ trước khi bấm Continue, sau đó chỉ nhận thư MỚI (khác id)
        mailapi_seen: set[str] | None = None
        if is_mailapi_email(email):
            before = mailapi_fetch(email)
            mailapi_seen = {str(m.get("id")) for m in before} if before is not None else None

        # Mốc thời gian yêu cầu mã (dung sai 2 phút): chỉ nhận thư gửi SAU mốc này
        self._otp_not_before = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=2)
        btn_continue.click()
        self.controller.update_status(row_idx, "Đã gửi yêu cầu OTP...", "#f59e0b")
        # Đợi 4 giây cho Muse gửi email
        time.sleep(4)
        if self._meta_checkpoint(page):
            self._fail_checkpoint(row_idx, email)
            return

        login_ok = False
        all_cookies = {}
        cookies_exp = {}

        if self._meta_manual_checkpoint(page):
            if self.manual_captcha:
                ok_manual, m_cookies, c_exp = self._wait_manual_captcha(
                    page, row_idx, email, MANUAL_CAPTCHA_WAIT_SEC, wait_for_otp_box=True
                )
                if not ok_manual:
                    return
                if "hatch_sess" in m_cookies:
                    login_ok = True
                    all_cookies = m_cookies
                    cookies_exp = c_exp
                else:
                    self._otp_not_before = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=2)
            else:
                self._fail_verify(row_idx, email)
                return

        if not login_ok:
            is_otp = page.run_js("""
                return !!(document.querySelector('input[autocomplete="one-time-code"]') || 
                          document.querySelector('input[aria-label="6-digit security code"]') ||
                          document.querySelector('input.sr-only'));
            """)
            if not is_otp:
                self.controller.update_status(row_idx, "Không hiện ô OTP", "#ef4444")
                self.controller.record_result(False)
                return

            # 2. Lấy OTP: email @nguyenmaucole.io.vn đọc qua API hòm thư tạm; email khác (Hotmail OAuth2...) đọc qua DongVanFB
            if is_mailapi_email(email):
                otp_code = self._otp_from_mailapi(row_idx, email, mailapi_seen)
            else:
                otp_code = self._otp_from_dongvan(page, dongvan_tab, row_idx, acc_line, email)

            if not otp_code:
                self.controller.update_status(row_idx, f"Không lấy được OTP sau {OTP_READ_ATTEMPTS} lần", "#ef4444")
                self.controller.record_result(False)
                return

            self.controller.remember_otp(email, otp_code)
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

            # 4. Chờ đăng nhập và lấy cookie (nếu bị yêu cầu nhập Captcha tay sau OTP thì dừng 10 phút chờ người dùng)
            self.controller.update_status(row_idx, "Chờ xác nhận Muse...", "#a78bfa")
            need_manual_captcha = False

            cookie_deadline = time.time() + 34
            while time.time() < cookie_deadline:
                if not self.controller.is_running:
                    return
                time.sleep(2)
                if self._meta_checkpoint(page):
                    self._fail_checkpoint(row_idx, email)
                    return
                if self._meta_manual_checkpoint(page):
                    need_manual_captcha = True
                    break
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
                if self.manual_captcha:
                    ok_manual, m_cookies, c_exp = self._wait_manual_captcha(
                        page, row_idx, email, MANUAL_CAPTCHA_WAIT_SEC
                    )
                    if not ok_manual:
                        return
                    login_ok = True
                    all_cookies = m_cookies
                    cookies_exp = c_exp
                else:
                    if need_manual_captcha:
                        self._fail_verify(row_idx, email)
                    else:
                        self.controller.update_status(row_idx, "Xác nhận thất bại", "#ef4444")
                        self.controller.record_result(False)
                    return

        # 5. Chuyển vào trang làm việc để Muse kích hoạt hatch_gw (Gateway Token) và hatch_vml
        self.controller.update_status(row_idx, "Đang nạp hatch_gw...", "#a78bfa")
        try:
            page.get("https://muse.ai/thread/new")
            for _ in range(10):
                time.sleep(1)
                if self._meta_manual_checkpoint(page):
                    break
                cdp = page.run_cdp("Storage.getCookies")
                clist = cdp.get("cookies", [])
                for c in clist:
                    if "muse.ai" in c.get("domain", ""):
                        all_cookies[c["name"]] = c["value"]
                        if c.get("expires", 0) > 0:
                            cookies_exp[c["name"]] = int(float(c.get("expires")))
                if "hatch_gw" in all_cookies and "hatch_vml" in all_cookies:
                    self.controller.log(f"[Luồng {self.worker_id}] [{email}] Đã nhận đầy đủ 4 cookie cốt lõi (kể cả hatch_gw & hatch_vml).")
                    break
        except Exception:
            pass

        if self._meta_manual_checkpoint(page):
            if self.manual_captcha:
                ok_manual, m_cookies, c_exp = self._wait_manual_captcha(
                    page, row_idx, email, MANUAL_CAPTCHA_WAIT_SEC
                )
                if not ok_manual:
                    return
                all_cookies.update(m_cookies)
                cookies_exp.update(c_exp)
                try:
                    page.get("https://muse.ai/thread/new")
                    for _ in range(8):
                        time.sleep(1)
                        cdp = page.run_cdp("Storage.getCookies")
                        for c in cdp.get("cookies", []):
                            if "muse.ai" in c.get("domain", ""):
                                all_cookies[c["name"]] = c["value"]
                                if c.get("expires", 0) > 0:
                                    cookies_exp[c["name"]] = int(float(c["expires"]))
                        if "hatch_gw" in all_cookies:
                            break
                except Exception:
                    pass
            else:
                self._fail_verify(row_idx, email)
                return

        if self._meta_checkpoint(page):
            self._fail_checkpoint(row_idx, email)
            return

        # 5b. Muse khoá TK ("Muse is disconnected"): TK chết -> đánh dấu, KHÔNG nạp cookie vào Studio, loại khỏi danh sách chạy lại
        if self._muse_disconnected(page):
            self.controller.log(f"[Luồng {self.worker_id}] [{email}] Muse báo 'Muse is disconnected' -> TK CHẾT, loại ra (không lưu cookie).")
            self.controller.update_status(row_idx, DEAD_STATUS, "#ef4444")
            self.controller.mark_dead(email)
            self.controller.record_result(False)
            return

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
        use_proxy: bool = True,
        use_fingerprint: bool = True,
        proxy_source: str = PROXY_SRC_APP,
        webshare_key: str = "",
        manual_captcha: bool = True,
    ):
        super().__init__()
        self.proxy_source = proxy_source
        self.webshare_key = webshare_key
        self.use_proxy = use_proxy
        self.use_fingerprint = use_fingerprint
        self.manual_captcha = manual_captcha
        self._used_otps: dict[str, set[str]] = {}
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
        if self.use_proxy:
            self.log(f"Đang chọn {actual_threads} proxy cố định (mỗi luồng 1 IP riêng)...")
            worker_proxies = pick_worker_proxies(actual_threads, self.log, self.proxy_source, self.webshare_key)
        else:
            self.log("Không dùng proxy: các luồng chạy bằng IP thật của máy.")
            worker_proxies = [None] * actual_threads
        for i in range(actual_threads):
            w = LoginWorker(
                worker_id=i + 1,
                task_queue=q,
                controller=self,
                headless=self.headless,
                chrome_path=self.chrome_path,
                delay_between=self.delay_between,
                proxy_str=worker_proxies[i],
                use_fingerprint=self.use_fingerprint,
                manual_captcha=self.manual_captcha,
            )
            workers.append(w)
            w.start()

        # Đợi các luồng hoàn thành
        for w in workers:
            w.join()

        if self.use_proxy:
            try:
                import proxy_manager as pm
                pm.stop_all_forwarders()      # chỉ đóng forwarder do công cụ này mở
            except Exception:  # noqa: BLE001
                pass
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

    def mark_checkpoint(self, email: str):
        """Ghi email cần xác minh tay (Meta chuyển sang Instagram) vào data/auto_login_checkpoint.txt (không ghi trùng)."""
        try:
            path = os.path.join(self.out_dir, "auto_login_checkpoint.txt")
            existing = set()
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    existing = {ln.split("|")[0].strip().lower() for ln in f if ln.strip()}
            if email.strip().lower() not in existing:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"{email}|Meta chuyen sang Instagram|{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}" + chr(10))
        except Exception:  # noqa: BLE001
            pass

    def used_otps(self, email: str) -> set[str]:
        """Các mã OTP đã dùng/đã thấy cho email này trong phiên chạy (để không lấy lại mã cũ)."""
        with self.lock:
            return set(self._used_otps.get(email.strip().lower(), set()))

    def remember_otp(self, email: str, code: str):
        with self.lock:
            self._used_otps.setdefault(email.strip().lower(), set()).add(code)

    def mark_dead(self, email: str, reason: str = "Muse is disconnected"):
        """Ghi TK chết vào data/auto_login_dead.txt và, nếu TK đã có trong Studio, khoá TK đó (không giao việc nữa)."""
        try:
            path = os.path.join(self.out_dir, "auto_login_dead.txt")
            existing = set()
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    existing = {ln.split("|")[0].strip().lower() for ln in f if ln.strip()}
            if email.strip().lower() not in existing:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"{email}|{reason}|{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}" + chr(10))
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.store:
                for a in self.store.list_accounts():
                    if str(a.get("label", "")).strip().lower() == email.strip().lower():
                        self.store.set_locked(a["id"], True, f"TK chết khi lấy cookie: {reason}")
        except Exception:  # noqa: BLE001
            pass

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
            QDialog, QMessageBox {
                background-color: #1e293b;
            }
            QMessageBox QLabel, QDialog QLabel {
                color: #f8fafc;
                background-color: transparent;
            }
            QComboBox {
                background-color: #0f172a;
                border: 1px solid #475569;
                border-radius: 6px;
                padding: 6px 10px;
                color: #f8fafc;
            }
            QComboBox:focus {
                border: 1px solid #38bdf8;
            }
            QComboBox QAbstractItemView {
                background-color: #0f172a;
                color: #f8fafc;
                border: 1px solid #475569;
                selection-background-color: #0284c7;
                selection-color: #ffffff;
                outline: 0;
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

        self.chk_proxy = QCheckBox("Proxy cố định (mỗi luồng 1 IP)")
        self.chk_proxy.setChecked(True)
        self.chk_proxy.setToolTip("Mỗi luồng đăng nhập qua 1 proxy riêng thay vì IP thật của máy (giảm bị Muse chặn)")
        row2.addWidget(self.chk_proxy)

        self.cmb_proxy_src = QComboBox()
        self.cmb_proxy_src.addItem("Nguồn: theo MuseAI Studio", PROXY_SRC_APP)
        self.cmb_proxy_src.addItem("Nguồn: HomeProxy", PROXY_SRC_HOMEPROXY)
        self.cmb_proxy_src.addItem("Nguồn: Webshare", PROXY_SRC_WEBSHARE)
        self.cmb_proxy_src.setToolTip("Chọn nguồn proxy cho các luồng lấy cookie. Chọn nguồn khác MuseAI Studio thì chỉ dùng cho công cụ này, không đổi cài đặt của MuseAI Studio.")
        self.cmb_proxy_src.currentIndexChanged.connect(self._on_proxy_src_changed)
        row2.addWidget(self.cmb_proxy_src)

        self.txt_ws_key = QLineEdit()
        self.txt_ws_key.setPlaceholderText("Webshare API key")
        self.txt_ws_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_ws_key.setFixedWidth(200)
        self.txt_ws_key.setToolTip("API key Webshare (webshare.io -> API). Để trống nếu MuseAI Studio đã lưu key. Nhập ở đây sẽ được lưu vào data/proxy_settings.json (không đưa lên git).")
        self.txt_ws_key.setVisible(False)
        row2.addWidget(self.txt_ws_key)

        self.chk_fp = QCheckBox("Mỗi trình duyệt 1 cấu hình khác nhau")
        self.chk_fp.setChecked(True)
        self.chk_fp.setToolTip("Mỗi lần mở Chrome dùng 1 hồ sơ riêng: màn hình, cửa sổ, số nhân CPU, RAM, card đồ hoạ, nhiễu Canvas/Audio, múi giờ, ngôn ngữ")
        row2.addWidget(self.chk_fp)

        self.chk_save_store = QCheckBox("Tự động nạp vào MuseAI Studio")
        self.chk_save_store.setChecked(True)
        self.chk_save_store.setToolTip("Cập nhật trực tiếp vào data/accounts.json")
        row2.addWidget(self.chk_save_store)

        self.chk_manual_captcha = QCheckBox("Chờ xác minh Captcha tay (10 phút)")
        self.chk_manual_captcha.setChecked(True)
        self.chk_manual_captcha.setToolTip(
            "Khi đăng nhập Muse và điền OTP xong nếu bị yêu cầu nhập Captcha / xác minh con người, "
            "phần mềm sẽ tạm dừng tối đa 10 phút (600s) để bạn thao tác trên trình duyệt Chrome."
        )
        row2.addWidget(self.chk_manual_captcha)

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

        row4 = QHBoxLayout()
        self.btn_save_ok = QPushButton("💾 Lưu TK thành công")
        self.btn_save_ok.setToolTip("Lưu danh sách các tài khoản ĐÃ THÀNH CÔNG (dòng gốc email|mật khẩu|...) ra 1 file .txt")
        self.btn_save_ok.clicked.connect(self.save_success_accounts)
        row4.addWidget(self.btn_save_ok)

        self.btn_del_ok = QPushButton("🗑 Xoá TK thành công")
        self.btn_del_ok.setToolTip("Xoá các tài khoản đã thành công khỏi bảng (KHÔNG sửa file gốc). Nên lưu trước bằng nút bên cạnh.")
        self.btn_del_ok.clicked.connect(self.delete_success_accounts)
        row4.addWidget(self.btn_del_ok)

        self.btn_save_all = QPushButton("💾 Lưu danh sách TK")
        self.btn_save_all.setToolTip("Lưu toàn bộ danh sách tài khoản hiện có trong bảng ra file .txt (ví dụ sau khi đã xoá TK thành công)")
        self.btn_save_all.clicked.connect(self.save_all_accounts)
        row4.addWidget(self.btn_save_all)
        row4.addStretch()
        ctrl_layout.addLayout(row4)
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

    # ------------------------------------------------------------------
    # LƯU / XOÁ TÀI KHOẢN THÀNH CÔNG, LƯU DANH SÁCH
    # ------------------------------------------------------------------
    @staticmethod
    def _status_is_success(st: str) -> bool:
        st = (st or "").strip()
        return st.startswith("✓") or "thành công" in st.lower()

    def _is_running(self) -> bool:
        ctrl = getattr(self, "controller", None)
        try:
            return bool(ctrl and ctrl.isRunning())
        except Exception:  # noqa: BLE001
            return False

    def success_rows(self) -> list[int]:
        rows = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 2)
            if item and self._status_is_success(item.text()):
                rows.append(r)
        return rows

    def _default_save_dir(self) -> str:
        src = self.txt_file.text().strip()
        return os.path.dirname(src) if src and os.path.isdir(os.path.dirname(src)) else BASE_DIR

    @staticmethod
    def _write_lines(path: str, lines: list[str]):
        """Ghi file; nếu file đã có thì chép bản dự phòng .bak_<giờ> trước khi ghi đè."""
        if os.path.isfile(path):
            try:
                shutil.copy2(path, f"{path}.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            except Exception:  # noqa: BLE001
                pass
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))

    def save_success_accounts(self):
        rows = self.success_rows()
        if not rows:
            QMessageBox.information(self, "Lưu TK thành công", "Chưa có tài khoản nào thành công để lưu.")
            return
        default = os.path.join(self._default_save_dir(), f"Acc_thanh_cong_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
        path, _ = QFileDialog.getSaveFileName(self, "Lưu các tài khoản đã thành công", default, "Text files (*.txt);;All files (*.*)")
        if not path:
            return
        self._write_lines(path, [self.accounts[r] for r in rows])
        self.log(f"[*] Đã lưu {len(rows)} tài khoản thành công vào: {path}")
        QMessageBox.information(self, "Lưu TK thành công", f"Đã lưu {len(rows)} tài khoản thành công vào:\n{path}")

    def delete_success_accounts(self):
        if self._is_running():
            QMessageBox.warning(self, "Đang chạy", "Hãy bấm DỪNG LẠI (hoặc chờ chạy xong) rồi mới xoá tài khoản thành công.")
            return
        rows = set(self.success_rows())
        if not rows:
            QMessageBox.information(self, "Xoá TK thành công", "Chưa có tài khoản nào thành công để xoá.")
            return
        ans = QMessageBox.question(
            self, "Xoá TK thành công",
            f"Xoá {len(rows)} tài khoản thành công khỏi bảng?\n\n"
            "Chỉ xoá trong bảng đang hiển thị, KHÔNG sửa file gốc.\n"
            "Nên bấm 'Lưu TK thành công' trước để giữ lại danh sách.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        keep = [r for r in range(self.table.rowCount()) if r not in rows]
        snap = []
        for r in keep:
            cells = []
            for c in range(1, 6):
                it = self.table.item(r, c)
                cells.append((it.text() if it else "", it.foreground() if it else None))
            snap.append(cells)
        self.accounts[:] = [self.accounts[r] for r in keep]
        self.table.setRowCount(len(keep))
        for new_r, cells in enumerate(snap):
            stt = QTableWidgetItem(str(new_r + 1))
            stt.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(new_r, 0, stt)
            for c, (text, brush) in zip(range(1, 6), cells):
                it = QTableWidgetItem(text)
                if c != 1:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if brush is not None:
                    it.setForeground(brush)
                self.table.setItem(new_r, c, it)
        total = len(self.accounts)
        failed = sum(1 for r in range(total) if (self.table.item(r, 2).text().strip() not in ("", "Sẵn sàng")))
        self.lbl_stats.setText(f"Tổng: {total} | Hoàn thành: {failed} | Thành công: 0 | Thất bại: {failed}")
        self.progress_bar.setValue(int(failed * 100 / total) if total else 0)
        self.log(f"[*] Đã xoá {len(rows)} tài khoản thành công khỏi bảng, còn {total} tài khoản.")

    def save_all_accounts(self):
        if not self.accounts:
            QMessageBox.information(self, "Lưu danh sách", "Danh sách tài khoản đang trống.")
            return
        src = self.txt_file.text().strip()
        default = src if src else os.path.join(BASE_DIR, f"Danh_sach_TK_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
        path, _ = QFileDialog.getSaveFileName(self, "Lưu danh sách tài khoản", default, "Text files (*.txt);;All files (*.*)")
        if not path:
            return
        self._write_lines(path, list(self.accounts))
        self.log(f"[*] Đã lưu {len(self.accounts)} tài khoản vào: {path}")
        QMessageBox.information(self, "Lưu danh sách", f"Đã lưu {len(self.accounts)} tài khoản vào:\n{path}")

    def _on_proxy_src_changed(self, _idx: int = 0):
        self.txt_ws_key.setVisible(self.cmb_proxy_src.currentData() == PROXY_SRC_WEBSHARE)

    def _proxy_source_args(self) -> dict:
        """(nguồn proxy, API key Webshare) cho WorkerController. Key nhập tay được lưu lại (file data/proxy_settings.json)."""
        src = self.cmb_proxy_src.currentData() or PROXY_SRC_APP
        key = self.txt_ws_key.text().strip() if src == PROXY_SRC_WEBSHARE else ""
        if key:
            try:
                import proxy_manager as pm
                pm._update_settings(webshare_key=key)
            except Exception:  # noqa: BLE001
                pass
        return {"proxy_source": src, "webshare_key": key}

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
        manual_captcha = self.chk_manual_captcha.isChecked()
        if manual_captcha and self.chk_headless.isChecked():
            self.chk_headless.setChecked(False)
            self.log("[!] Đã tự động bỏ tích 'Chạy ngầm (Ẩn cửa sổ Chrome)' để bạn có thể nhập Captcha bằng tay trên trình duyệt.")
        headless = self.chk_headless.isChecked()
        save_store = self.chk_save_store.isChecked()

        self.controller = WorkerController(
            accounts=self.accounts,
            num_threads=num_threads,
            headless=headless,
            save_to_store=save_store,
            delay_between=delay,
            use_proxy=self.chk_proxy.isChecked(),
            use_fingerprint=self.chk_fp.isChecked(),
            manual_captcha=manual_captcha,
            **self._proxy_source_args(),
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
            # TK đã bị Muse khoá vĩnh viễn (Muse is disconnected): loại ra, không chạy lại
            if st == DEAD_STATUS:
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
        manual_captcha = self.chk_manual_captcha.isChecked()
        if manual_captcha and self.chk_headless.isChecked():
            self.chk_headless.setChecked(False)
            self.log("[!] Đã tự động bỏ tích 'Chạy ngầm (Ẩn cửa sổ Chrome)' để bạn có thể nhập Captcha bằng tay trên trình duyệt.")
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
            use_proxy=self.chk_proxy.isChecked(),
            use_fingerprint=self.chk_fp.isChecked(),
            manual_captcha=manual_captcha,
            tasks=tasks,
            **self._proxy_source_args(),
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
