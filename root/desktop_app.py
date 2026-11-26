"""MuseAI Video Studio - Phần Mềm Desktop Tạo Video AI Chuyên Nghiệp từ Muse.ai

Chạy ứng dụng desktop native trên Windows sử dụng Microsoft Edge WebView2 (pywebview)
tích hợp trực tiếp máy chủ FastAPI & Browser Automation Engine.
"""
from __future__ import annotations

import base64
import ctypes
import mimetypes
import os
import shutil
import sys
import threading
import time
import urllib.request
import webbrowser

import webview

# Đảm bảo đường dẫn import
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import logger_setup
logger_setup.setup_logging()

from config import CFG
import shopee_engine
import store


# ==================== ĐIỀU KHIỂN CỬA SỔ CMD (WINDOWS) ====================
_SW_HIDE = 0
_SW_SHOW = 5


def _get_console_hwnd() -> int:
    if os.name != "nt":
        return 0
    try:
        return int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
    except Exception:
        return 0


def _disable_console_quick_edit() -> None:
    """Tắt QuickEdit Mode của CMD để click nhầm vào cửa sổ đen không làm treo toàn bộ tiến trình Python."""
    if os.name != "nt":
        return
    try:
        k32 = ctypes.windll.kernel32
        h_in = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h_in, ctypes.byref(mode)):
            # Bỏ ENABLE_QUICK_EDIT_MODE (0x0040), bật ENABLE_EXTENDED_FLAGS (0x0080)
            new_mode = (mode.value & ~0x0040) | 0x0080
            k32.SetConsoleMode(h_in, new_mode)
    except Exception:
        pass


def _is_console_visible() -> bool:
    hwnd = _get_console_hwnd()
    if not hwnd:
        return False
    try:
        return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    except Exception:
        return False


def _set_console_visible(visible: bool) -> bool:
    hwnd = _get_console_hwnd()
    if not hwnd:
        return False
    try:
        ctypes.windll.user32.ShowWindow(hwnd, _SW_SHOW if visible else _SW_HIDE)
        return _is_console_visible()
    except Exception:
        return False


def _should_auto_hide_console() -> bool:
    """Chỉ tự ẩn CMD khi mở qua trình chạy riêng (Chay_MESUAI.bat) hoặc đặt MUSE2API_HIDE_CONSOLE=1.
    Đặt MUSE2API_SHOW_CONSOLE=1 để luôn giữ cửa sổ CMD khi debug."""
    if os.name != "nt":
        return False
    if os.environ.get("MUSE2API_SHOW_CONSOLE", "").strip() == "1":
        return False
    hwnd = _get_console_hwnd()
    if not hwnd:
        return False
    if os.environ.get("MUSE2API_HIDE_CONSOLE", "").strip() == "1":
        return True
    try:
        buf = ctypes.create_unicode_buffer(512)
        if ctypes.windll.kernel32.GetConsoleTitleW(buf, len(buf)):
            if "museai" in (buf.value or "").lower():
                return True
    except Exception:
        pass
    try:
        pids = (ctypes.c_uint32 * 8)()
        cnt = ctypes.windll.kernel32.GetConsoleProcessList(pids, 8)
        # <= 2 tiến trình gắn console (chỉ cmd.exe + python.exe do click đúp .bat) -> an toàn để tự ẩn
        return 0 < cnt <= 2
    except Exception:
        return False


class DesktopAPI:
    """Cầu nối giao tiếp 2 chiều giữa giao diện JavaScript và hệ điều hành Windows."""

    def __init__(self):
        self._window = None
        self._allow_close = False

    def set_window(self, win):
        self._window = win

    def get_desktop_config(self) -> dict:
        """Trả về cấu hình hiện tại cho giao diện người dùng."""
        return {
            "api_key": CFG.api_key,
            "host": CFG.host,
            "port": CFG.port,
            "media_dir": CFG.media_dir,
            "chromium": CFG.chromium,
            "is_desktop": True,
            "has_console": bool(_get_console_hwnd()),
            "console_visible": _is_console_visible(),
            "close_to_taskbar": os.environ.get("MUSE2API_CLOSE_TO_TASKBAR", "1").strip() != "0",
        }

    def get_console_state(self) -> dict:
        return {
            "has_console": bool(_get_console_hwnd()),
            "visible": _is_console_visible(),
        }

    def toggle_console(self) -> dict:
        """Bật/tắt hiển thị cửa sổ CMD chạy ngầm kèm phần mềm."""
        if not _get_console_hwnd():
            return {"success": False, "has_console": False, "visible": False}
        want = not _is_console_visible()
        now_vis = _set_console_visible(want)
        return {"success": True, "has_console": True, "visible": now_vis}

    def exit_app(self) -> dict:
        """Thoát hẳn phần mềm (bỏ qua cơ chế thu nhỏ xuống Taskbar khi bấm X) và dọn sạch Chrome/proxy."""
        self._allow_close = True
        if self._window:
            def _do_destroy():
                try:
                    self._window.destroy()
                except Exception:
                    os._exit(0)
            threading.Timer(0.05, _do_destroy).start()
        return {"success": True}

    def choose_image(self) -> dict:
        """Mở hộp thoại chọn file ảnh chuẩn của Windows (Native OpenFileDialog)."""
        if not self._window:
            return {"success": False, "error": "Cửa sổ chưa sẵn sàng"}
        try:
            res = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=(
                    "Hình ảnh (*.png;*.jpg;*.jpeg;*.webp)",
                    "Tất cả tệp (*.*)",
                ),
            )
            if res and len(res) > 0:
                file_path = res[0]
                mime, _ = mimetypes.guess_type(file_path)
                if not mime:
                    mime = "image/png"
                with open(file_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                data_url = f"data:{mime};base64,{b64}"
                return {
                    "success": True,
                    "path": file_path,
                    "name": os.path.basename(file_path),
                    "data_url": data_url,
                }
        except Exception as e:
            return {"success": False, "error": str(e)}
        return {"success": False}

    def save_video_dialog(self, filename: str) -> dict:
        """Mở hộp thoại Lưu Video chuẩn của Windows (Native SaveFileDialog)."""
        if not self._window:
            return {"success": False, "error": "Cửa sổ chưa sẵn sàng"}
        src = os.path.join(CFG.media_dir, filename)
        if not os.path.isfile(src):
            return {"success": False, "error": f"Tệp không tồn tại: {filename}"}

        try:
            res = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=filename,
                file_types=("Video MP4 (*.mp4)", "Tất cả tệp (*.*)"),
            )
            if res:
                dst = res if isinstance(res, str) else res[0]
                shutil.copy2(src, dst)
                return {"success": True, "saved_path": dst}
        except Exception as e:
            return {"success": False, "error": str(e)}
        return {"success": False}

    def open_video_folder(self) -> dict:
        """Mở trực tiếp thư mục chứa video trên Windows File Explorer."""
        try:
            os.makedirs(CFG.media_dir, exist_ok=True)
            os.startfile(CFG.media_dir)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def pick_folder_dialog(self, default_path: str = "") -> dict:
        """Mở hộp thoại chọn thư mục chuẩn của Windows (Native FolderDialog)."""
        if not self._window:
            return {"success": False, "error": "Cửa sổ chưa sẵn sàng"}
        try:
            initial_dir = shopee_engine.resolve_out_dir(default_path or shopee_engine.DEFAULT_OUT_DIR)
            if not os.path.isdir(initial_dir):
                parent_dir = os.path.dirname(initial_dir)
                initial_dir = parent_dir if os.path.isdir(parent_dir) else CFG.base_dir
            res = self._window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=initial_dir,
            )
            if res and len(res) > 0:
                selected_dir = res[0]
                return {"success": True, "folder_path": selected_dir}
        except Exception as e:
            return {"success": False, "error": str(e)}
        return {"success": False}

    def pick_text_file_dialog(self, title: str = "Chọn file TXT chứa link Shopee") -> dict:
        """Mở hộp thoại chọn file text chuẩn Windows (Native OpenFileDialog)."""
        file_path = None
        if self._window:
            try:
                res = self._window.create_file_dialog(
                    webview.OPEN_DIALOG,
                    allow_multiple=False,
                    file_types=(
                        "Text Files (*.txt)",
                        "All Files (*.*)",
                    ),
                )
                if res and len(res) > 0:
                    file_path = res[0]
            except Exception as e:
                print(f"[DesktopAPI] webview create_file_dialog error: {e}")

        # Fallback bằng tkinter nếu webview không mở được hộp thoại
        if not file_path:
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                file_path = filedialog.askopenfilename(
                    title=title,
                    filetypes=[("Text Files (*.txt)", "*.txt"), ("All Files (*.*)", "*.*")]
                )
                root.destroy()
            except Exception as e:
                print(f"[DesktopAPI] tkinter filedialog fallback error: {e}")

        if file_path and os.path.isfile(file_path):
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                return {
                    "success": True,
                    "file_path": file_path,
                    "name": os.path.basename(file_path),
                    "content": content
                }
            except Exception as e:
                return {"success": False, "error": f"Không thể đọc file: {e}"}
        return {"success": False, "cancelled": True}

    def open_folder_path(self, folder_path: str) -> dict:
        """Mở thư mục bất kỳ trên Windows Explorer."""
        try:
            _, resolved_folder = shopee_engine.ensure_out_dir(folder_path)
            os.startfile(resolved_folder)
            return {"success": True, "path": resolved_folder}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def open_url(self, url: str) -> dict:
        """Mở liên kết web bằng trình duyệt mặc định của hệ thống."""
        try:
            webbrowser.open(url)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}


_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_server_running(url: str) -> bool:
    """Kiểm tra máy chủ FastAPI có đang phản hồi không (đi thẳng loopback, bỏ qua proxy Windows)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MuseStudioHealthCheck"})
        with _LOOPBACK_OPENER.open(req, timeout=1.5) as resp:
            return resp.status in (200, 401, 404)
    except Exception:
        return False


def _start_background_server(err_box: dict | None = None):
    """Khởi chạy máy chủ FastAPI ngầm trong luồng daemon."""
    try:
        import uvicorn
        from app import app

        uvicorn.run(
            app,
            host=CFG.host or "127.0.0.1",
            port=CFG.port or 18610,
            log_level="warning",
            access_log=False,
        )
    except BaseException as exc:  # noqa: BLE001
        if err_box is not None:
            err_box["error"] = f"{type(exc).__name__}: {exc}"


def main():
    _disable_console_quick_edit()
    CFG.ensure_dirs()
    port = CFG.port or 18610
    server_url = f"http://127.0.0.1:{port}"
    probe_url = f"{server_url}/api/studio/config"

    print(f"[*] Khởi động MuseAI Video Studio Pro...")
    print(f"[*] Thư mục lưu trữ: {CFG.media_dir}")
    print(f"[*] Trình duyệt: {CFG.chromium}")

    # Kiểm tra máy chủ đã chạy chưa
    if not _is_server_running(probe_url):
        print(f"[*] Đang khởi chạy máy chủ backend trên cổng {port}...")
        err_box: dict = {}
        t = threading.Thread(target=_start_background_server, args=(err_box,), daemon=True)
        t.start()

        # Đợi máy chủ thực sự lắng nghe trên loopback (tối đa ~30 giây), dừng sớm nếu thread backend thoát lỗi
        ready = False
        for _ in range(150):
            time.sleep(0.2)
            if _is_server_running(probe_url):
                ready = True
                break
            if not t.is_alive():
                break

        if not ready:
            detail = err_box.get("error") or (
                "Tiến trình máy chủ backend đã tự thoát trước khi mở cổng"
                if not t.is_alive()
                else f"Quá thời gian chờ cổng {port} phản hồi"
            )
            print(f"[!] KHÔNG THỂ KHỞI ĐỘNG MÁY CHỦ TRÊN {server_url} ({detail}).")
            print(f"[!] Gợi ý kiểm tra:")
            print(f"    1. Đóng tiến trình python/uvicorn cũ đang chiếm cổng {port} (hoặc đổi MUSE2API_PORT).")
            print(f"    2. Kiểm tra file .env: đảm bảo MUSE2API_HOST=0.0.0.0 (hoặc 127.0.0.1), hiện đang là '{CFG.host}'.")
            print(f"    3. Xem chi tiết lỗi tại: {os.path.join(BASE_DIR, 'log.txt')}")
            sys.exit(1)

        print(f"[+] Máy chủ đã hoạt động: {server_url}")
    else:
        print(f"[+] Máy chủ đã sẵn sàng từ trước: {server_url}")

    # Chống Edge WebView2 tự đóng băng/bóp nhịp JS setTimeout khi người dùng thu nhỏ hoặc che cửa sổ app
    # (nếu không tắt, 70 luồng JS poll /v1/videos sẽ ngủ đông nhiều phút cho tới khi bấm mở lại cửa sổ!)
    _wv2_flags = (
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-ipc-flooding-protection",
        "--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion",
    )
    _cur_wv2 = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "").strip()
    _missing = [f for f in _wv2_flags if f not in _cur_wv2]
    if _missing:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (_cur_wv2 + " " + " ".join(_missing)).strip()

    # Khởi tạo giao diện Desktop
    api = DesktopAPI()
    window = webview.create_window(
        title="MuseAI Video Studio Pro - Phần Mềm Tạo Video AI Chuyên Nghiệp",
        url=f"{server_url}/",
        js_api=api,
        width=1320,
        height=860,
        min_size=(1060, 680),
        background_color="#070a12",
        text_select=True,
    )
    api.set_window(window)

    # ==================== KHAY HỆ THỐNG (SYSTEM TRAY GÓC PHẢI) ====================
    # Dùng trực tiếp System.Windows.Forms.NotifyIcon của WinForms (có sẵn cùng pywebview, không cần cài thư viện ngoài).
    class _SystemTrayController:
        def __init__(self, win, api_obj):
            self._win = win
            self._api = api_obj
            self.ready = False
            self._balloon_shown = False
            self._form = None
            self._notify_icon = None
            self._menu = None
            self._icon = None
            self._bmp = None
            self._callbacks = []  # giữ tham chiếu mạnh tránh Python GC thu hồi delegate WinForms

        def _load_icon(self, Drawing, Drawing2D):
            ico_path = os.path.join(BASE_DIR, "muse-logo.ico")
            if os.path.isfile(ico_path):
                try:
                    return Drawing.Icon(ico_path), None
                except Exception:
                    pass
            # Fallback: tự vẽ biểu tượng chữ M màu xanh-tím sắc nét 32x32 nếu thiếu muse-logo.ico
            bmp = Drawing.Bitmap(32, 32)
            g = Drawing.Graphics.FromImage(bmp)
            g.SmoothingMode = Drawing2D.SmoothingMode.AntiAlias
            brush = Drawing2D.LinearGradientBrush(
                Drawing.Rectangle(0, 0, 32, 32),
                Drawing.Color.FromArgb(255, 79, 70, 229),
                Drawing.Color.FromArgb(255, 14, 165, 233),
                45.0,
            )
            g.FillRectangle(brush, 2, 2, 28, 28)
            font = Drawing.Font("Segoe UI", 16.0, Drawing.FontStyle.Bold)
            sf = Drawing.StringFormat()
            sf.Alignment = Drawing.StringAlignment.Center
            sf.LineAlignment = Drawing.StringAlignment.Center
            g.DrawString("M", font, Drawing.SolidBrush(Drawing.Color.White), Drawing.RectangleF(0.0, 1.0, 32.0, 32.0), sf)
            g.Dispose()
            return Drawing.Icon.FromHandle(bmp.GetHicon()), bmp

        def setup(self) -> bool:
            if os.name != "nt" or self.ready:
                return self.ready
            try:
                import clr
                clr.AddReference("System.Windows.Forms")
                clr.AddReference("System.Drawing")
                from System import Action
                import System.Windows.Forms as WinForms
                import System.Drawing as Drawing
                import System.Drawing.Drawing2D as Drawing2D
                import webview.platforms.winforms as wf

                form = wf.BrowserView.instances.get(self._win.uid)
                if form is None:
                    return False
                self._form = form

                def _create_on_ui_thread():
                    if self.ready:
                        return
                    ico, bmp = self._load_icon(Drawing, Drawing2D)
                    self._icon = ico
                    self._bmp = bmp
                    try:
                        form.Icon = ico
                    except Exception:
                        pass

                    ni = WinForms.NotifyIcon()
                    ni.Icon = ico
                    ni.Text = "MuseAI Video Studio Pro (Đang chạy)"
                    ni.Visible = True

                    menu = WinForms.ContextMenuStrip()
                    mi_open = WinForms.ToolStripMenuItem("Mở MuseAI Video Studio")
                    mi_cmd = WinForms.ToolStripMenuItem("Hiện / Ẩn cửa sổ CMD")
                    mi_exit = WinForms.ToolStripMenuItem("Thoát hẳn MuseAI (Dừng toàn bộ luồng)")

                    def _restore(_s=None, _e=None):
                        try:
                            form.Show()
                            if form.WindowState == WinForms.FormWindowState.Minimized:
                                form.WindowState = WinForms.FormWindowState.Normal
                            form.Activate()
                            form.TopMost = True
                            form.TopMost = False
                        except Exception:
                            try:
                                self._win.show()
                            except Exception:
                                pass

                    def _on_mouse_click(_s, e):
                        try:
                            if e.Button == WinForms.MouseButtons.Left:
                                _restore()
                        except Exception:
                            _restore()

                    def _on_toggle_cmd(_s, _e):
                        try:
                            self._api.toggle_console()
                        except Exception:
                            pass

                    def _on_exit(_s, _e):
                        self.dispose()
                        self._api.exit_app()

                    mi_open.Click += _restore
                    mi_cmd.Click += _on_toggle_cmd
                    mi_exit.Click += _on_exit
                    ni.MouseClick += _on_mouse_click
                    ni.DoubleClick += _restore
                    ni.BalloonTipClicked += _restore

                    menu.Items.Add(mi_open)
                    if _get_console_hwnd():
                        menu.Items.Add(mi_cmd)
                    menu.Items.Add(WinForms.ToolStripSeparator())
                    menu.Items.Add(mi_exit)
                    ni.ContextMenuStrip = menu

                    self._notify_icon = ni
                    self._menu = menu
                    self._callbacks = [_restore, _on_mouse_click, _on_toggle_cmd, _on_exit]
                    self.ready = True

                if form.InvokeRequired:
                    form.Invoke(Action(_create_on_ui_thread))
                else:
                    _create_on_ui_thread()
                return self.ready
            except Exception as exc:
                print(f"[!] Không khởi tạo được biểu tượng khay hệ thống (System Tray): {exc}")
                return False

        def hide_to_tray(self) -> bool:
            if not self.ready:
                self.setup()
            if not self.ready or self._form is None:
                return False
            try:
                from System import Action
                import System.Windows.Forms as WinForms

                def _do():
                    if self._notify_icon is not None:
                        self._notify_icon.Visible = True
                    self._form.Hide()
                    if not self._balloon_shown and self._notify_icon is not None:
                        self._balloon_shown = True
                        try:
                            self._notify_icon.ShowBalloonTip(
                                2500,
                                "MuseAI Video Studio vẫn đang chạy ngầm",
                                "Phần mềm đã ẩn ở góc phải (khay hệ thống). Bấm biểu tượng để mở lại hoặc chuột phải để Thoát hẳn.",
                                WinForms.ToolTipIcon.Info,
                            )
                        except Exception:
                            pass

                if self._form.InvokeRequired:
                    self._form.Invoke(Action(_do))
                else:
                    _do()
                return True
            except Exception:
                return False

        def dispose(self):
            ni = self._notify_icon
            self._notify_icon = None
            form = self._form
            if ni is None:
                return

            def _do_dispose():
                try:
                    ni.Visible = False
                    ni.Dispose()
                except Exception:
                    pass
                try:
                    if self._menu is not None:
                        self._menu.Dispose()
                        self._menu = None
                except Exception:
                    pass
                try:
                    if self._icon is not None:
                        self._icon.Dispose()
                        self._icon = None
                except Exception:
                    pass
                try:
                    if self._bmp is not None:
                        self._bmp.Dispose()
                        self._bmp = None
                except Exception:
                    pass

            try:
                if form is not None and not form.IsDisposed and form.InvokeRequired:
                    from System import Action
                    form.Invoke(Action(_do_dispose))
                else:
                    _do_dispose()
            except Exception:
                _do_dispose()

    tray = _SystemTrayController(window, api)

    # Ấn nút X (Close) -> ẩn vào khay hệ thống góc dưới bên phải (System Tray) thay vì đóng app
    # (trừ khi bấm nút "Thoát" trong giao diện / chuột phải biểu tượng khay -> Thoát hẳn, hoặc MUSE2API_CLOSE_TO_TASKBAR=0).
    close_to_tray = (
        os.environ.get("MUSE2API_CLOSE_TO_TRAY", "1").strip() != "0"
        and os.environ.get("MUSE2API_CLOSE_TO_TASKBAR", "1").strip() != "0"
    )

    def _on_closing():
        if api._allow_close or not close_to_tray:
            tray.dispose()
            return True

        def _do_hide():
            try:
                if not tray.hide_to_tray():
                    # Dự phòng an toàn: nếu không tạo được icon khay hệ thống thì thu nhỏ xuống Taskbar để không mất dấu app
                    window.minimize()
            except Exception:
                try:
                    window.minimize()
                except Exception:
                    pass

        # Gọi ẩn sau 50ms để WinForms hoàn tất hủy sự kiện FormClosing trước khi Hide()
        threading.Timer(0.05, _do_hide).start()
        return False

    def _on_shown():
        # Tạo sẵn biểu tượng MuseAI ở khay hệ thống góc phải
        threading.Timer(0.15, tray.setup).start()
        # Tự ẩn cửa sổ CMD ngay khi giao diện Desktop đã hiện lên (lỗi lúc khởi động trước đó vẫn thấy rõ trên CMD)
        if _should_auto_hide_console():
            threading.Timer(0.25, lambda: _set_console_visible(False)).start()

    window.events.closing += _on_closing
    window.events.shown += _on_shown

    _cleanup_lock = threading.Lock()
    _cleaned_up = {"done": False}

    def _cleanup():
        with _cleanup_lock:
            if _cleaned_up["done"]:
                return
            _cleaned_up["done"] = True
        try:
            tray.dispose()
        except Exception:
            pass
        print("[*] Đang đóng trình duyệt Chrome và dọn dẹp các tiến trình ngầm...")
        try:
            from app import engine
            engine.stop()
            engine.kill_orphaned_chromes()
        except Exception:
            try:
                from engine import engine
                engine.stop()
                engine.kill_orphaned_chromes()
            except Exception:
                pass
        try:
            import proxy_manager
            proxy_manager.stop_all_forwarders()
        except Exception:
            pass
        # Dọn dẹp cache video tạm trong data/media khi tắt app để tránh đầy ổ cứng
        try:
            m_dir = CFG.media_dir
            if os.path.isdir(m_dir):
                del_cnt = 0
                for f in os.listdir(m_dir):
                    fp = os.path.join(m_dir, f)
                    if os.path.isfile(fp):
                        try:
                            os.remove(fp)
                            del_cnt += 1
                        except Exception:
                            pass
                if del_cnt > 0:
                    print(f"[+] Đã dọn dẹp sạch {del_cnt} file video tạm trong data/media.")
        except Exception:
            pass

        # Dọn dẹp cache ảnh Shopee (shopee_images_cache) khi tắt app để tránh tốn dung lượng
        try:
            shopee_cache = os.path.join(BASE_DIR, "data", "shopee_images_cache")
            if os.path.isdir(shopee_cache):
                del_s = 0
                for f in os.listdir(shopee_cache):
                    fp = os.path.join(shopee_cache, f)
                    try:
                        if os.path.isfile(fp) or os.path.islink(fp):
                            os.remove(fp)
                            del_s += 1
                        elif os.path.isdir(fp):
                            shutil.rmtree(fp, ignore_errors=True)
                            del_s += 1
                    except Exception:
                        pass
                if del_s > 0:
                    print(f"[+] Đã dọn dẹp sạch {del_s} ảnh trong data/shopee_images_cache.")
        except Exception:
            pass

        print("[+] Đã dọn dẹp sạch sẽ tài nguyên và tiến trình.")

    import atexit
    atexit.register(_cleanup)

    # Chạy vòng lặp Desktop Window (sử dụng Edge WebView2)
    print(f"[*] Đang mở cửa sổ ứng dụng Desktop...")
    try:
        webview.start(gui="edgechromium", debug=False)
    except BaseException:
        # Nếu app crash bất thường khi CMD đang ẩn -> hiện lại CMD để người dùng thấy thông báo lỗi / pause không bị treo ẩn
        _set_console_visible(True)
        raise
    finally:
        _cleanup()
        print(f"[*] Đã đóng ứng dụng MuseAI Studio.")


if __name__ == "__main__":
    main()
