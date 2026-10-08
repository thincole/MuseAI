"""Quản lý hệ thống ghi log thời gian thực ra file log.txt cho MuseAI Video Studio Pro.
Mỗi khi khởi động lại phần mềm, file log.txt sẽ tự động được xóa trắng (truncate)
và ghi lại toàn bộ hoạt động của hệ thống (Engine, Proxy, Shopee, API, lỗi phát sinh).
"""
import os
import sys
import logging
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "log.txt")

_initialized = False


import io


class _TeeStream:
    """Ghi dữ liệu đồng thời ra stream gốc (console) và file log.txt."""
    def __init__(self, original_stream, log_filepath):
        self.original_stream = original_stream
        self.log_filepath = log_filepath

    def write(self, message):
        try:
            self.original_stream.write(message)
            self.original_stream.flush()
        except Exception:
            pass
        if message:
            try:
                with open(self.log_filepath, "a", encoding="utf-8", errors="replace") as f:
                    f.write(message)
            except Exception:
                pass

    def flush(self):
        try:
            self.original_stream.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            fn = getattr(self.original_stream, "isatty", None)
            return bool(fn()) if fn else False
        except Exception:
            return False

    def fileno(self):
        try:
            return self.original_stream.fileno()
        except Exception:
            raise io.UnsupportedOperation("fileno")

    def __getattr__(self, name):
        return getattr(self.original_stream, name)


def _silence_proactor_winerror10054():
    """Bỏ qua thông báo lỗi ConnectionResetError (WinError 10054) vô hại của Windows asyncio Proactor khi client ngắt kết nối."""
    if sys.platform == "win32":
        try:
            from asyncio.proactor_events import _ProactorBasePipeTransport
            _orig_lost = _ProactorBasePipeTransport._call_connection_lost

            def _clean_lost(self, exc):
                try:
                    _orig_lost(self, exc)
                except (ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            _ProactorBasePipeTransport._call_connection_lost = _clean_lost
        except Exception:
            pass


def setup_logging():
    """Khởi tạo file log.txt, xóa trắng dữ liệu cũ và thiết lập handler ghi log liên tục."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    _silence_proactor_winerror10054()

    # 1. Sao lưu log phiên trước sang log.txt.1 nếu có
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    backup_file = os.path.join(BASE_DIR, "log.txt.1")
    try:
        if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > 0:
            import shutil
            shutil.copy2(LOG_FILE, backup_file)
    except Exception:
        pass

    header = (
        "================================================================================\n"
        f"        MuseAI Video Studio Pro - Nhật Ký Hoạt Động Hệ Thống (log.txt)\n"
        f"        Thời gian khởi động: {now_str}\n"
        "================================================================================\n\n"
    )
    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(header)
    except Exception as e:
        print(f"Lỗi khởi tạo {LOG_FILE}: {e}")

    # 2. Tạo FileHandler cho logging tiêu chuẩn
    file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8", errors="replace")
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Tránh gắn trùng handler
    if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == os.path.abspath(LOG_FILE) for h in root.handlers):
        root.addHandler(file_handler)

    # 3. Chuyển hướng stdout và stderr để gom toàn bộ print() và traceback lỗi vào log.txt
    try:
        if not isinstance(sys.stdout, _TeeStream):
            sys.stdout = _TeeStream(sys.stdout, LOG_FILE)
        if not isinstance(sys.stderr, _TeeStream):
            sys.stderr = _TeeStream(sys.stderr, LOG_FILE)
    except Exception:
        pass

    logging.getLogger("muse2api").info("Hệ thống ghi log vào %s đã sẵn sàng (đã xóa trắng từ phiên trước).", LOG_FILE)
