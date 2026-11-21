"""MuseAI Video Studio - Phần Mềm Desktop Tạo Video AI Chuyên Nghiệp từ Muse.ai

Chạy ứng dụng desktop native trên Windows sử dụng Microsoft Edge WebView2 (pywebview)
tích hợp trực tiếp máy chủ FastAPI & Browser Automation Engine.
"""
from __future__ import annotations

import base64
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


class DesktopAPI:
    """Cầu nối giao tiếp 2 chiều giữa giao diện JavaScript và hệ điều hành Windows."""

    def __init__(self):
        self._window = None

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
        }

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

    def _cleanup():
        print("[*] Đang đóng trình duyệt Chrome và dọn dẹp các tiến trình ngầm...")
        try:
            from app import engine
            engine.stop()
            engine.kill_orphaned_chromes()
        except Exception as e:
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
    finally:
        _cleanup()
        print(f"[*] Đã đóng ứng dụng MuseAI Studio.")


if __name__ == "__main__":
    main()
