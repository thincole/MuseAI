"""Shopee Engine cho MuseAI — Sao chép toàn bộ logic lấy sản phẩm từ Database PostgreSQL
và sinh prompt chuẩn TVC / Video quảng cáo từ Novagate sang MuseAI.
"""
from __future__ import annotations

import atexit
import base64
import collections
import copy
import ipaddress
import json
import logging
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

import ssl_ctx
from config import CFG

log = logging.getLogger("shopee_engine")


def _ffmpeg_concurrency() -> int:
    env_val = os.environ.get("MUSE2API_FFMPEG_CONCURRENCY", "").strip()
    if env_val:
        try:
            return max(1, min(int(env_val), 64))
        except (TypeError, ValueError):
            pass
    # Tự động cân đối số luồng ghép FFmpeg theo số luồng CPU (mỗi FFmpeg dùng -threads 2 + -filter_threads 2
    # nên ~cpu_threads // 3 tiến trình song song tận dụng tối đa CPU mà không nghẽn chuyển ngữ cảnh).
    cpu_threads = os.cpu_count() or 8
    return max(6, min(20, cpu_threads // 3))


# Concurrency Limiter: giới hạn số tiến trình FFmpeg encode/ghép video chạy đồng thời
# (mặc định 4 — đủ cho ~40 luồng render; chỉnh qua biến môi trường MUSE2API_FFMPEG_CONCURRENCY).
# Các luồng worker vượt quá giới hạn sẽ xếp hàng chờ. Tác vụ stream-copy thường chỉ mất <1-2s, nhưng
# re-encode / ghép Outro có thể mất hàng chục giây (timeout tới 240s), nên KHÔNG dùng 1 khóa toàn cục
# (sẽ chặn mọi job khác tới vài phút). Giới hạn nhỏ vẫn giúp tránh nghẽn I/O ổ đĩa và quá tải CPU
# (kết hợp với độ ưu tiên tiến trình thấp + "-threads 2" bên dưới). ffprobe (nhẹ, <1s) chạy ngoài semaphore.
class _DynamicLimiter:
    """Bộ giới hạn số luồng FFmpeg đồng thời hỗ trợ co giãn giới hạn động an toàn
    (không lỗi BoundedSemaphore 'released too many times' khi hạ giới hạn lúc đang có nhiều job chạy)."""

    def __init__(self, limit: int):
        self._cond = threading.Condition(threading.Lock())
        self._limit = max(1, int(limit))
        self._active = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        return self._active

    def set_limit(self, new_limit: int) -> int:
        val = max(1, int(new_limit))
        with self._cond:
            self._limit = val
            self._cond.notify_all()
        return val

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        with self._cond:
            if not blocking:
                if self._active < self._limit:
                    self._active += 1
                    return True
                return False
            deadline = None if timeout is None else (time.monotonic() + timeout)
            while self._active >= self._limit:
                rem = None if deadline is None else (deadline - time.monotonic())
                if rem is not None and rem <= 0:
                    return False
                self._cond.wait(rem)
            self._active += 1
            return True

    def release(self) -> None:
        with self._cond:
            if self._active > 0:
                self._active -= 1
            self._cond.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


FFMPEG_CONCURRENCY = _ffmpeg_concurrency()
FFMPEG_BASE_CONCURRENCY = FFMPEG_CONCURRENCY
FFMPEG_SEMAPHORE = _DynamicLimiter(FFMPEG_CONCURRENCY)
FFMPEG_LOCK = FFMPEG_SEMAPHORE  # Giữ tên cũ để tương thích ngược (nếu module khác import).
_FFMPEG_CONF_LOCK = threading.Lock()
_FFMPEG_ACTIVE_LOCK = threading.Lock()
_FFMPEG_ACTIVE_COUNT = 0


def _apply_ffmpeg_limit_locked(val: int) -> int:
    global FFMPEG_CONCURRENCY
    if val == FFMPEG_CONCURRENCY:
        return FFMPEG_CONCURRENCY
    FFMPEG_CONCURRENCY = FFMPEG_SEMAPHORE.set_limit(val)
    return FFMPEG_CONCURRENCY


def set_ffmpeg_concurrency(n) -> int:
    """Thay đổi động số luồng FFmpeg ghép video chạy đồng thời ngay trong RAM (đánh thức ngay các luồng đang chờ)."""
    global FFMPEG_BASE_CONCURRENCY
    try:
        val = max(1, min(int(n), 64))
    except (TypeError, ValueError):
        val = _ffmpeg_concurrency()
    with _FFMPEG_CONF_LOCK:
        FFMPEG_BASE_CONCURRENCY = val
        _apply_ffmpeg_limit_locked(val)
    log.info("🎞️ [FFmpeg] Đã đặt số luồng ghép video đồng thời: %d luồng", FFMPEG_CONCURRENCY)
    return FFMPEG_CONCURRENCY


def apply_dynamic_ffmpeg_throttle(cpu_pct: float) -> int:
    """Tự động tiết chế số luồng FFmpeg khi CPU cao để nhường CPU cho Chrome WebSocket/render, và mở lại khi CPU dịu."""
    with _FFMPEG_CONF_LOCK:
        base = max(1, int(FFMPEG_BASE_CONCURRENCY or FFMPEG_CONCURRENCY or 4))
        cur = FFMPEG_CONCURRENCY
        if cpu_pct >= 90.0:
            target = max(2, min(base, int(base * 0.4)))
        elif cpu_pct >= 82.0:
            target = max(2, min(base, int(base * 0.7)))
        elif cpu_pct <= 72.0:
            target = base
        else:
            target = cur
        if target != cur:
            _apply_ffmpeg_limit_locked(target)
            log.info("🎞️ [FFmpeg Auto-Throttle] CPU %.0f%% -> điều chỉnh luồng FFmpeg %d -> %d (gốc %d)",
                     cpu_pct, cur, target, base)
        return FFMPEG_CONCURRENCY


def get_ffmpeg_status() -> dict:
    with _FFMPEG_ACTIVE_LOCK:
        active = _FFMPEG_ACTIVE_COUNT
    return {
        "active": active,
        "limit": FFMPEG_CONCURRENCY,
        "base": FFMPEG_BASE_CONCURRENCY,
    }


def _safe_remove(path: str | None) -> None:
    """Xóa file (output dở dang / file tạm) nếu tồn tại, bỏ qua lỗi."""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as e:
        log.debug("Không xóa được file %s: %s", path, e)


def _run_ffmpeg_cmd(cmd: list[str], timeout: int = 180, check: bool = False, text: bool = False) -> subprocess.CompletedProcess:
    """Chạy FFmpeg / FFprobe với độ ưu tiên tiến trình thấp (BELOW_NORMAL_PRIORITY_CLASS trên Windows)
    và ẩn hoàn toàn cửa sổ đen con (CREATE_NO_WINDOW).
    Đảm bảo CPU luôn ưu tiên giao diện Windows, chuột, bàn phím và hệ điều hành, máy tính không bao giờ bị đơ/lag."""
    global _FFMPEG_ACTIVE_COUNT
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "check": check,
        "text": text,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000) |
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    is_encode = bool(cmd) and os.path.basename(str(cmd[0])).lower().startswith("ffmpeg")
    if is_encode:
        with _FFMPEG_ACTIVE_LOCK:
            _FFMPEG_ACTIVE_COUNT += 1
    try:
        return subprocess.run(cmd, **kwargs)
    finally:
        if is_encode:
            with _FFMPEG_ACTIVE_LOCK:
                _FFMPEG_ACTIVE_COUNT = max(0, _FFMPEG_ACTIVE_COUNT - 1)


_NVENC_AVAILABLE: bool | None = None
_NVENC_LOCK = threading.Lock()
# Giới hạn số phiên NVENC chạy đồng thời trên GPU phổ thông (driver NVIDIA thường chặn 3-8 phiên; vượt ngưỡng
# thì chuyển thẳng sang CPU libx264 thay vì 30+ tiến trình cùng đâm vào CUDA rồi chờ/lỗi).
_NVENC_MAX_SLOTS = max(1, min(8, int(os.environ.get("MUSE2API_NVENC_SLOTS", "4") or 4)))
_NVENC_SEMAPHORE = threading.BoundedSemaphore(_NVENC_MAX_SLOTS)


def has_nvenc() -> bool:
    """Kiểm tra máy có GPU NVIDIA hỗ trợ encode phần cứng h264_nvenc thật sự không (chỉ kiểm tra 1 lần rồi lưu cache)."""
    global _NVENC_AVAILABLE
    if os.environ.get("MUSE2API_DISABLE_NVENC", "").strip() == "1":
        return False
    if _NVENC_AVAILABLE is not None:
        return _NVENC_AVAILABLE
    with _NVENC_LOCK:
        if _NVENC_AVAILABLE is not None:
            return _NVENC_AVAILABLE
        try:
            res = _run_ffmpeg_cmd(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
                    "-c:v", "h264_nvenc", "-f", "null", "-",
                ],
                timeout=8,
            )
            _NVENC_AVAILABLE = (res.returncode == 0)
            if _NVENC_AVAILABLE:
                log.info("🚀 [FFmpeg GPU] Đã kích hoạt tăng tốc phần cứng NVIDIA NVENC (h264_nvenc, tối đa %d luồng GPU đồng thời)!", _NVENC_MAX_SLOTS)
        except Exception:
            _NVENC_AVAILABLE = False
        return _NVENC_AVAILABLE


def _ffmpeg_global_thread_args() -> list[str]:
    """Giới hạn số luồng nội bộ cho decoder + filter_complex của mỗi tiến trình FFmpeg để tránh hàng chục
    tiến trình chạy song song mỗi tiến trình tự đẻ hàng chục OS thread gây tranh chấp CPU."""
    return ["-threads", "2", "-filter_threads", "2", "-filter_complex_threads", "2"]


def _video_encode_args(use_nvenc: bool) -> list[str]:
    if use_nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "superfast", "-crf", "22", "-threads", "2", "-pix_fmt", "yuv420p"]

# ==================== CẤU HÌNH DATABASE NOVAGATE (đọc từ .env, không ghi cứng secret) ====================
# Server URL / API key / Client ID để trong .env (file này đã được .gitignore) để không lộ khi commit.
def get_computer_name() -> str:
    """Lấy tên máy tính hiện tại đang chạy."""
    return os.environ.get("COMPUTERNAME", "").strip() or socket.gethostname() or "MuseAI_Client"


# Giá trị mặc định chuẩn:
# - Server URL: http://100.79.170.67:3000
# - API Key: shopee_secret_2026
# - Client ID: Tên máy tính đang chạy (Hostname)
DEFAULT_SERVER_URL = os.environ.get("SHOPEE_SERVER_URL", "").strip() or "http://100.79.170.67:3000"
DEFAULT_API_KEY = os.environ.get("SHOPEE_API_KEY", "").strip() or "shopee_secret_2026"
DEFAULT_CLIENT_ID = os.environ.get("SHOPEE_CLIENT_ID", "").strip() or get_computer_name()
_LOCAL_FALLBACK_OUT_DIR = os.path.join(CFG.base_dir, "output")
DEFAULT_OUT_DIR = os.environ.get("SHOPEE_OUT_DIR", "").strip() or _LOCAL_FALLBACK_OUT_DIR

_OUT_DIR_WARN_LOCK = threading.Lock()
_WARNED_INVALID_OUT_DIRS: set[str] = set()


def _has_drive_root(path: str) -> bool:
    """Kiểm tra nhanh ổ đĩa dạng X: có tồn tại trên Windows hay không (tránh chạm UNC mạng)."""
    drive, _ = os.path.splitdrive(path)
    if len(drive) == 2 and drive[1] == ":":
        return os.path.exists(drive + os.sep)
    return True


def _warn_out_dir_once(bad_path: str, reason: str, fallback: str) -> None:
    with _OUT_DIR_WARN_LOCK:
        if bad_path in _WARNED_INVALID_OUT_DIRS:
            return
        _WARNED_INVALID_OUT_DIRS.add(bad_path)
    log.warning(
        "⚠️ [Thư mục xuất] '%s' không khả dụng trên máy này (%s) -> tự động chuyển sang '%s'.",
        bad_path, reason, fallback,
    )


def resolve_out_dir(raw_dir: str | None = None, fallback_dir: str | None = None) -> str:
    """Chuẩn hóa đường dẫn thư mục xuất video; tự fallback về thư mục cục bộ nếu ổ đĩa không tồn tại."""
    local_fb = os.path.normpath(_LOCAL_FALLBACK_OUT_DIR)
    fb_cand = os.path.normpath(str(fallback_dir or "").strip() or DEFAULT_OUT_DIR)
    fb = fb_cand if _has_drive_root(fb_cand) else local_fb

    cand = str(raw_dir or "").strip()
    if not cand:
        return fb
    norm = os.path.normpath(cand)
    if not _has_drive_root(norm):
        drive, _ = os.path.splitdrive(norm)
        _warn_out_dir_once(norm, f"ổ đĩa {drive} không tồn tại", fb)
        return fb
    return norm


def ensure_out_dir(raw_dir: str | None = None, fallback_dir: str | None = None, subdir: str = "") -> tuple[str, str]:
    """Đảm bảo tạo được thư mục xuất (hoặc thư mục con như 'temp'); lỗi OS/ổ đĩa tự fallback an toàn."""
    target = resolve_out_dir(raw_dir, fallback_dir=fallback_dir)
    full = os.path.join(target, subdir) if subdir else target
    try:
        os.makedirs(full, exist_ok=True)
        return target, full
    except OSError as exc:
        fb = resolve_out_dir("", fallback_dir=fallback_dir)
        if fb == target:
            fb = os.path.normpath(_LOCAL_FALLBACK_OUT_DIR)
        _warn_out_dir_once(target, str(exc), fb)
        full_fb = os.path.join(fb, subdir) if subdir else fb
        try:
            os.makedirs(full_fb, exist_ok=True)
            return fb, full_fb
        except OSError:
            local_fb = os.path.normpath(_LOCAL_FALLBACK_OUT_DIR)
            if fb == local_fb:
                raise
            local_full = os.path.join(local_fb, subdir) if subdir else local_fb
            os.makedirs(local_full, exist_ok=True)
            return local_fb, local_full


# Đọc lại tại thời điểm gọi (không đóng băng giá trị lúc import) — dùng khi caller không truyền server_url/api_key.
def _env_server_url() -> str:
    return os.environ.get("SHOPEE_SERVER_URL", "").strip() or DEFAULT_SERVER_URL or "http://100.79.170.67:3000"


def _env_api_key() -> str:
    return os.environ.get("SHOPEE_API_KEY", "").strip() or DEFAULT_API_KEY or "shopee_secret_2026"


def _env_client_id() -> str:
    return os.environ.get("SHOPEE_CLIENT_ID", "").strip() or DEFAULT_CLIENT_ID or get_computer_name()


# ==================== KHUNG CẢNH PRESET TỪ NOVAGATE ====================
SCENES = [
    ("📦 Tổng kho hàng hóa",
     "in a large organized warehouse with neatly stacked product shelves behind, bright industrial lighting",
     "trong nhà kho lớn ngăn nắp với các kệ sản phẩm xếp gọn phía sau, ánh sáng công nghiệp sáng rõ"),
    ("🛒 Siêu thị hiện đại",
     "inside a bright modern supermarket with shiny product displays, spacious shopping environment",
     "bên trong siêu thị hiện đại sáng sủa với các gian hàng trưng bày sản phẩm bóng loáng, không gian mua sắm rộng rãi"),
    ("🎥 Phòng review chuyên nghiệp",
     "in a professional product review studio with clean white background and softbox lighting setup",
     "trong phòng quay đánh giá sản phẩm chuyên nghiệp với phông nền trắng sạch và hệ thống đèn softbox"),
    ("🛋 Phòng khách sang trọng",
     "in a luxurious modern living room with leather sofa, warm golden ambient lighting, elegant decor",
     "trong phòng khách hiện đại sang trọng với sofa da, ánh sáng vàng ấm áp, nội thất thanh lịch"),
    ("💼 Văn phòng hiện đại",
     "in a stylish modern office with glass desk, ergonomic chair, minimalist decor and green plants",
     "trong văn phòng hiện đại phong cách với bàn kính, ghế công thái học, trang trí tối giản và cây xanh"),
    ("🌳 Ngoài trời công viên",
     "outdoors in a beautiful green park with natural sunlight filtering through tree canopy",
     "ngoài trời trong công viên xanh mát với ánh nắng tự nhiên xuyên qua tán cây"),
    ("📸 Studio chụp ảnh",
     "in a professional photo studio with grey backdrop, ring light, clean minimal setup",
     "trong studio chụp ảnh chuyên nghiệp với phông nền xám, đèn ring light, bố trí gọn gàng tối giản"),
    ("🏬 Showroom trưng bày",
     "in an upscale product showroom with glass display shelves and LED spotlight illumination",
     "trong showroom trưng bày sản phẩm cao cấp với kệ kính và đèn LED chiếu điểm"),
    ("☕ Quán café hiện đại",
     "in a stylish modern cafe with wooden table, warm ambient light, cozy relaxing atmosphere",
     "trong quán café hiện đại phong cách với bàn gỗ, ánh sáng ấm áp, không gian thư giãn ấm cúng"),
    ("📦 Bàn unboxing",
     "at a clean unboxing desk with brown kraft paper, scissors, and packaging materials, bright overhead lighting",
     "tại bàn unboxing gọn gàng với giấy kraft nâu, kéo và vật liệu đóng gói, ánh sáng trên đầu sáng rõ"),
    ("⚖ Bàn so sánh sản phẩm",
     "at a product comparison table with clean white surface, multiple items neatly arranged side by side, professional overhead lighting",
     "tại bàn so sánh sản phẩm với mặt bàn trắng sạch, nhiều sản phẩm xếp gọn cạnh nhau, ánh sáng chuyên nghiệp từ trên"),
    ("📱 Studio livestream",
     "in a professional livestream studio with ring light, camera tripod, colorful LED backdrop, and product display shelf",
     "trong studio livestream chuyên nghiệp với đèn ring light, chân tripod camera, phông nền LED nhiều màu, và kệ trưng bày sản phẩm"),
    ("🏭 Nhà máy sản xuất",
     "inside the actual manufacturing facility where this product is made, professional workers in clean uniform, bright industrial lighting, product assembly lines visible",
     "bên trong nhà máy sản xuất thực tế nơi sản phẩm được làm ra, công nhân mặc đồng phục, ánh sáng công nghiệp sáng rõ, dây chuyền đóng gói"),
]

SCENE_NAMES = [s[0] for s in SCENES]
SCENE_MAP_EN = {s[0]: s[1] for s in SCENES}
SCENE_MAP_VI = {s[0]: s[2] for s in SCENES}
SCENE_OPTIONS = ["🎲 Random"] + SCENE_NAMES

# ==================== ĐỊNH DẠNG NỘI DUNG (CONTENT FORMATS) ====================
CONTENT_MAP_VI = {
    "Review kho hàng": "[ĐỊNH DẠNG NỘI DUNG - REVIEW KHO HÀNG: Góc quay vừa/rộng trong tổng kho ngăn nắp với các kệ hàng xếp cao đằng sau. Người mẫu đứng tự tin giữa kho hàng, vừa cầm sản phẩm vừa chỉ tay tự hào giới thiệu số lượng lớn hàng sẵn có.]\n\n",
    "POV": "[ĐỊNH DẠNG NỘI DUNG - POV GÓC NHÌN THỨ NHẤT: Góc quay từ tầm mắt người xem nhìn xuống bàn. Không hiện mặt người mẫu mà chỉ tập trung cận cảnh đôi tay đang cầm, xoay lật và thao tác trực tiếp trên sản phẩm. Camera chuyển động theo sát thao tác bàn tay.]\n\n",
    "UGC": "[ĐỊNH DẠNG NỘI DUNG - UGC KHÁCH HÀNG TỰ QUAY: Phong cách máy quay điện thoại cầm tay gia đình tự nhiên, không gian góc phòng ngủ/bàn học ấm áp gần gũi. Người mẫu đưa sản phẩm sát camera với nụ cười thân thiện, mang lại cảm giác đánh giá chân thực như bạn bè chia sẻ với nhau.]\n\n",
    "Unboxing": "[ĐỊNH DẠNG NỘI DUNG - UNBOXING ĐẬP HỘP: Góc máy từ trên xuống mặt bàn. Đôi tay tỉ mỉ bóc gói hàng kraft, tháo lớp bọc chống sốc và hé lộ sản phẩm cùng phụ kiện nguyên vẹn bên trong. Ánh sáng nét rõ, tập trung vào trải nghiệm đập hộp.]\n\n",
    "Demo công dụng": "[ĐỊNH DẠNG NỘI DUNG - DEMO CÔNG DỤNG THỰC TẾ: Camera cận cảnh Macro tập trung 100% vào việc thử nghiệm tính năng, độ bền, kết cấu và hiệu quả sử dụng thực tế của sản phẩm. Thao tác dứt khoát, hình ảnh cực kỳ sắc nét.]\n\n",
    "Review tự nhiên": "[ĐỊNH DẠNG NỘI DUNG - REVIEW TỰ NHIÊN: Người mẫu đứng tự nhiên cầm sản phẩm, di chuyển thoải mái trong khung cảnh. Camera handheld có rung nhẹ tự nhiên, góc medium shot. Ánh sáng tự nhiên từ cửa sổ hoặc ngoài trời, không dàn dựng studio. Phong cách đời thường, năng động, chân thực như đang chia sẻ với bạn bè.]\n\n",
    "So Sánh/Đánh Giá": "[ĐỊNH DẠNG NỘI DUNG - SO SÁNH ĐÁNH GIÁ: Người mẫu cầm sản phẩm đặt cạnh các vật tham chiếu để so sánh kích thước, chất lượng. Camera chuyển đổi giữa góc rộng (thấy cả hai) và cận cảnh (chi tiết từng sản phẩm). Ánh sáng đều, trung tính. Biểu cảm phân tích, suy nghĩ chân thực — gật đầu hài lòng hoặc nhăn mặt nhẹ khi so sánh.]\n\n",
}

_LANG_MAP = {
    "ph": {
        "instruction": "The model speaks Filipino",
        "country_en": "Filipino",
        "country_vi": "Philippines",
        "language_tvc": "Filipino",
        "nationality_tvc": "Filipino"
    },
    "vi": {
        "instruction": "Người mẫu nói tiếng Việt",
        "country_en": "Vietnamese",
        "country_vi": "Việt Nam",
        "language_tvc": "tiếng Việt",
        "nationality_tvc": "Vietnamese"
    },
    "id": {
        "instruction": "The model speaks Indonesian",
        "country_en": "Indonesian",
        "country_vi": "Indonesia",
        "language_tvc": "tiếng Indonesia (Bahasa Indonesia)",
        "nationality_tvc": "Indonesian"
    },
    "th": {
        "instruction": "The model speaks Thai",
        "country_en": "Thai",
        "country_vi": "Thái Lan",
        "language_tvc": "Thai (ภาษาไทย)",
        "nationality_tvc": "Thai"
    },
    "my": {
        "instruction": "The model speaks Malay",
        "country_en": "Malaysian",
        "country_vi": "Malaysia",
        "language_tvc": "tiếng Malaysia (Bahasa Melayu)",
        "nationality_tvc": "Malaysian"
    },
    "tw": {
        "instruction": "The model speaks Traditional Chinese (Taiwanese Mandarin)",
        "country_en": "Taiwanese",
        "country_vi": "Đài Loan",
        "language_tvc": "Traditional Chinese (Taiwanese Mandarin)",
        "nationality_tvc": "Taiwanese"
    },
    "sg": {
        "instruction": "The model speaks English",
        "country_en": "Singaporean",
        "country_vi": "Singapore",
        "language_tvc": "English",
        "nationality_tvc": "Singaporean"
    },
    "en": {
        "instruction": "The model speaks English",
        "country_en": "American",
        "country_vi": "Mỹ",
        "language_tvc": "English",
        "nationality_tvc": "American"
    }
}

# ==================== NHÂN VẬT BẢN QUYỀN CẦN CHẶN ====================
BLOCKED_IP_KEYWORDS = [
    "spider-man", "spiderman", "spider man", "marvel", "avengers", "iron man", "captain america",
    "hulk", "thor", "batman", "superman", "disney", "pixar", "elsa", "hello kitty", "hellokitty",
    "sanrio", "kuromi", "cinnamoroll", "my melody", "pompompurin", "mickey mouse", "minnie mouse",
    "mickey", "minnie", "pokemon", "pokémon", "pikachu", "takara tomy", "sponge bob", "spongebob",
    "doraemon", "naruto", "one piece", "harry potter", "paw patrol", "transformers", "transformer",
    "optimus prime", "bumblebee", "nemo", "finding nemo", "dory", "stitch", "lilo & stitch",
    "super mario", "mario", "luigi", "dragon ball", "goku", "sailor moon", "demon slayer",
    "kimetsu", "attack on titan", "peppa pig", "shin chan", "totoro", "ghibli", "barbie",
    "star wars", "yoda", "lego", "labubu", "pop mart", "popmart", "genshin impact", "genshin",
    "roger federer", "cristiano ronaldo", "messi"
]
BLOCKED_IP_RE = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in BLOCKED_IP_KEYWORDS) + r")\b", re.IGNORECASE)


def is_blocked_ip(product_name: str):
    """Kiểm tra sản phẩm có dính nhân vật bản quyền hay không."""
    if not product_name:
        return None
    s = str(product_name)
    m = BLOCKED_IP_RE.search(s)
    if m:
        return m
    try:
        norm = unicodedata.normalize('NFKD', s).encode('ASCII', 'ignore').decode('utf-8')
        return BLOCKED_IP_RE.search(norm)
    except Exception:
        return None


def clean_product_title(name: str) -> str:
    r"""Làm sạch tên sản phẩm trước khi đưa vào prompt:
    1. Gỡ bỏ thẻ ngoặc quảng cáo: 【 】 [ ] ( ) | - * ~ # $ % ^ & _ = { } \ < > / ? : ; "
    2. Gỡ bỏ các từ nhạy cảm / vi phạm chính sách AI video.
    3. Trả về tối đa 12 từ chữ sạch tinh gọn.
    """
    if not name:
        return "featured item"
    s = str(name).lower()
    s = re.sub(r"[\【\[\(].*?[\】\]\)]", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)

    policy_risk_words = [
        "100%", "chính hãng", "chinh hang", "đặc trị", "dac tri", "chữa khỏi", "chua khoi",
        "dứt điểm", "dut diem", "phục hồi", "phuc hoi", "thần tốc", "than toc", "cam kết",
        "cam ket", "bao hành", "bao hanh", "replica", "fake", "super fake",
        "bra", "underwear", "panties", "bikini", "crop top", "lace", "breast", "nude",
        "sexy", "erotic", "lingerie", "magsafe", "iphone", "apple", "nike", "adidas",
        "bluetooth", "scratch", "remover", "jump starter", "compressor", "gold", "silver",
        "medicine", "cure", "medical", "treatment", "pill", "cream", "whitening", "slim",
        "slimming", "weight loss", "gun", "knife", "blade", "bomb", "chemical", "poison",
        "orthopedic", "plaster", "pain relief", "patches", "healing", "therapy",
        "pokemon", "pokémon", "pikachu", "hello kitty", "hellokitty", "sanrio",
        "disney", "nemo", "dory", "stitch", "paw patrol", "transformers", "transformer",
        "mickey", "minnie", "mario", "luigi", "takara tomy", "wilson", "roger federer",
        "gucci", "chanel", "louis vuitton", "dior", "prada", "rolex", "casio"
    ]
    for w in policy_risk_words:
        s = re.sub(r"\b" + re.escape(w) + r"\b", "", s, flags=re.IGNORECASE)

    words = [w.strip() for w in s.split() if len(w.strip()) > 1]
    # 12 từ: loại sản phẩm thường nằm cuối tiêu đề Shopee (vd "... Board Shorts", "... T-Shirt") — 4 từ hay cắt mất
    clean_str = " ".join(words[:12]).strip()
    return clean_str if clean_str else "featured item"


# Ràng buộc giữ nguyên sản phẩm theo ảnh tham chiếu (09/10: video Unboxing tự bịa hộp in hình, in chữ "SUPER SALE"
# từ banner quảng cáo lên bao bì, vẽ lại logo/chữ sai) -> gắn vào mọi prompt video.
PRODUCT_FIDELITY = (
    'The product must look exactly identical to the product in the attached reference image in every frame: '
    'same shape, proportions, colors, materials, patterns, logo and printed text. '
    'Do not redesign, morph, bend, stretch, melt or deform the product, and do not add or remove parts. '
    'Use only the product itself from the reference image and ignore any promotional text, prices, stickers, '
    'watermarks, badges or collage elements around it. '
    'Hands hold the product naturally without merging into it.'
)

_IMAGE_PRODUCT = "the product shown in the attached reference image"


def _image_first(prompt: str, short_name: str) -> str:
    """Hình ảnh theo ẢNH, tên Shopee chỉ dùng cho lời thoại.

    Thử 10/10: tên trong phần mô tả cảnh lấn át ảnh ("aquaflask ... silicon boot" = vỏ đế bình bị vẽ thành ủng cao su,
    kể cả khi dặn "làm theo ảnh") -> bỏ tên khỏi mô tả hình ảnh, chỉ đưa vào câu cho lời thoại."""
    prompt = prompt.replace(f'the product "{short_name}"', _IMAGE_PRODUCT).replace(f'"{short_name}"', _IMAGE_PRODUCT)
    return (f'{prompt} For the voiceover only, the shop listing title of this product is "{short_name}"; '
            f'the visuals must show exactly the item in the reference image even if the title suggests something else.')


def pick_scene(user_choice: str, lang: str = "en") -> tuple[str, str]:
    """Trả về (scene_name, scene_desc) dựa trên lựa chọn user."""
    smap = SCENE_MAP_VI if lang == "vi" else SCENE_MAP_EN
    default = "trong phòng quay đánh giá chuyên nghiệp" if lang == "vi" else "in a professional review studio"
    if user_choice == "🎲 Random" or not user_choice or "random" in user_choice.lower():
        scene = random.choice(SCENES)
        idx = 2 if lang == "vi" else 1
        return scene[0], scene[idx]
    return user_choice, smap.get(user_choice, default)


def build_tvc_prompt(product_name: str, lang: str = "ph", review_style: str = "Unboxing") -> tuple[str, str]:
    """Prompt TVC 8s/10s + ràng buộc giữ nguyên sản phẩm theo ảnh tham chiếu (PRODUCT_FIDELITY)."""
    prompt, label = _build_tvc_prompt_base(product_name, lang=lang, review_style=review_style)
    return f"{_image_first(prompt, clean_product_title(product_name))} {PRODUCT_FIDELITY}", label


def _build_tvc_prompt_base(product_name: str, lang: str = "ph", review_style: str = "Unboxing") -> tuple[str, str]:
    """Tạo 1 prompt TVC 8s chuẩn cho video quảng cáo / review sản phẩm Shopee từ Novagate.
    Hỗ trợ đầy đủ phong cách:
    - POV / Góc nhìn thứ nhất: TUYỆT ĐỐI KHÔNG có mặt người mẫu, chỉ có 2 bàn tay thao tác trên mặt bàn.
    - Unboxing / Đập hộp: Góc máy từ trên xuống, chỉ có 2 bàn tay bóc hộp / giới thiệu.
    - Demo Công Dụng: Cận cảnh macro 100% vào tính năng sản phẩm, không quay mặt.
    - Review có MC: Người mẫu nữ (hoặc nam nếu Malaysia).
    """
    _tvc = _LANG_MAP.get(lang, _LANG_MAP["ph"])
    short_name = clean_product_title(product_name)

    style_raw = str(review_style or "").strip()
    if not style_raw or "random" in style_raw.lower() or "🎲" in style_raw:
        style_raw = random.choice([
            "Unboxing", "POV (Góc nhìn thứ nhất)", "Review tự nhiên",
            "Ngồi Review", "UGC Authentic", "Demo Công Dụng", "So Sánh/Đánh Giá"
        ])

    style_lower = style_raw.lower()
    is_pov = any(k in style_lower for k in ("pov", "góc nhìn thứ nhất"))
    is_unbox = any(k in style_lower for k in ("unbox", "đập hộp"))
    is_demo = any(k in style_lower for k in ("demo", "công dụng", "feature"))

    if is_pov:
        prompt = (
            f'Create a product advertisement video (TVC) reviewing the product "{short_name}". '
            f'A first-person point of view (POV) shot looking down at a clean minimalist tabletop surface. '
            f'ABSOLUTELY NO human face, NO head, NO presenter body visible. '
            f'Only two clean, natural hands are visible holding, rotating, demonstrating, and interacting with the product. '
            f'The hands smoothly showcase the key features, texture, and quality of the product. '
            f'Voiceover speaks in {_tvc["language_tvc"]} explaining the product benefits right away without any introduction; no text is displayed in the video. '
            f'The product is accurately sized. '
            f'The product price is not mentioned in the video.'
        )
        return prompt, "POV"

    if is_unbox:
        prompt = (
            f'Create a product advertisement video (TVC) unboxing the product "{short_name}". '
            f'A top-down desk camera angle looking down at a clean tabletop. '
            f'ABSOLUTELY NO human face, NO head, NO presenter body visible. '
            f'Only two clean, natural hands are visible opening a plain, unbranded brown cardboard box '
            f'(no printing, no logo, no text on the box) and taking out the product, then presenting it to the camera. '
            f'The product taken out of the box is exactly the product from the reference image. '
            f'The hands smoothly showcase the product details and craftsmanship. '
            f'Voiceover speaks in {_tvc["language_tvc"]} explaining the product benefits right away without any introduction; no text is displayed in the video. '
            f'The product is accurately sized. '
            f'The product price is not mentioned in the video.'
        )
        return prompt, "Unboxing"

    if is_demo:
        prompt = (
            f'Create a product advertisement video (TVC) demonstrating the product "{short_name}". '
            f'Extreme close-up macro shots focusing 100% on demonstrating and testing the product features, durability, and practical utility on a clean surface. '
            f'ABSOLUTELY NO human face visible. Only clean hands interacting with and testing the product. '
            f'Voiceover speaks in {_tvc["language_tvc"]} explaining the product benefits right away without any introduction; no text is displayed in the video. '
            f'The product is accurately sized. '
            f'The product price is not mentioned in the video.'
        )
        return prompt, "Demo Công Dụng"

    if lang == "my":
        prompt = (
            f'Create a product advertisement video (TVC) reviewing the product "{short_name}". '
            f'A handsome Malay male model, modest clothing (clean long-sleeve shirt, dark trousers), about 25 years old, '
            f'holds the product and introduces its key benefits. '
            f'He states the benefits right away without any introduction. '
            f'He speaks {_tvc["language_tvc"]}; no text is displayed in the video. '
            f'The product is accurately sized. '
            f'The product price is not mentioned in the video.'
        )
        return prompt, "Mẫu Nam"
    else:
        prompt = (
            f'Create a product advertisement video (TVC) reviewing the product "{short_name}". '
            f'A beautiful {_tvc["nationality_tvc"]} woman, about 20 years old, holds the product and introduces its key benefits. '
            f'She states the benefits right away without any introduction. '
            f'She speaks {_tvc["language_tvc"]}; no text is displayed in the video. '
            f'The product is accurately sized. '
            f'Her outfit is modest and appropriate, not revealing or offensive. '
            f'The product price is not mentioned in the video.'
        )
        return prompt, "Mẫu Nữ"


CLIP_CACHE_FILE = os.path.join(CFG.data_dir, "shopee_clip_cache.json")

# Khóa cho từng file JSON (RLock để add_to_blacklist có thể giữ khóa xuyên suốt get -> sửa -> save).
_CLIP_CACHE_LOCK = threading.RLock()
_BLACKLIST_LOCK = threading.RLock()

# Bộ đệm RAM cho shopee_clip_cache.json: tránh đọc/parse/fsync file 30MB+ dưới khóa toàn cục trên mỗi video.
_CLIP_CACHE_DATA: dict[str, dict] | None = None
_CLIP_CACHE_PATH_LOADED: str | None = None
_CLIP_CACHE_MTIME: float = 0.0
_CLIP_CACHE_DIRTY: bool = False
_CLIP_CACHE_FLUSH_TIMER: threading.Timer | None = None
_CLIP_CACHE_FLUSH_LOCK = threading.Lock()
_CLIP_CACHE_CAN_WRITE: bool = True
CLIP_CACHE_FLUSH_INTERVAL = max(1.0, float(os.environ.get("MUSE2API_CLIP_CACHE_FLUSH_SEC", "4.0") or 4.0))
CLIP_CACHE_MAX_ENTRIES = max(2000, int(os.environ.get("MUSE2API_CLIP_CACHE_MAX", "25000") or 25000))

# Trạng thái trả về của _load_json_file
_JSON_MISSING = "missing"
_JSON_OK = "ok"
_JSON_CORRUPT_PRESERVED = "corrupt_preserved"      # file hỏng đã được đổi tên .corrupt-<ts> -> ghi mới an toàn
_JSON_CORRUPT_UNPRESERVED = "corrupt_unpreserved"  # file hỏng nhưng KHÔNG đổi tên được -> cấm ghi đè
_JSON_READ_ERROR = "read_error"                    # lỗi I/O khi đọc (khóa file, quyền...) -> không ghi đè


def _atomic_write_json(path: str, data, compact: bool = False) -> None:
    """Ghi JSON nguyên tử: ghi ra `path + '.tmp'` (cùng thư mục) -> flush + fsync -> os.replace (thử lại vài lần
    vì trên Windows file đích có thể đang bị process khác/antivirus mở tạm thời). Ném lỗi nếu thất bại."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    dump_kw = {"separators": (",", ":")} if compact else {"indent": 2}
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, **dump_kw)
            f.flush()
            os.fsync(f.fileno())
        last_err: Exception | None = None
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as e:
                last_err = e
                time.sleep(0.05 * (attempt + 1))
        raise last_err  # type: ignore[misc]
    except BaseException:
        _safe_remove(tmp)
        raise


def _preserve_corrupt_file(path: str, label: str, err) -> bool:
    """Đổi tên file JSON hỏng thành `<path>.corrupt-<ts>` để không mất dữ liệu. Trả về True nếu đổi tên thành công."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = f"{path}.corrupt-{ts}"
    n = 1
    while os.path.exists(dst):
        dst = f"{path}.corrupt-{ts}-{n}"
        n += 1
    try:
        os.replace(path, dst)
        log.error("File %s (%s) bị hỏng (%s) — đã lưu lại thành %s, dùng giá trị mặc định.", label, path, err, dst)
        return True
    except OSError as e2:
        log.error("File %s (%s) bị hỏng (%s) và KHÔNG đổi tên được (%s) — sẽ không ghi đè để tránh mất dữ liệu.",
                  label, path, err, e2)
        return False


def _load_json_file(path: str, label: str) -> tuple[dict | None, str]:
    """Đọc file JSON (object). Trả về (data | None, trạng thái _JSON_*).
    File hỏng (JSON lỗi / không phải object) được đổi tên .corrupt-<ts> trước khi trả về None."""
    if not os.path.isfile(path):
        return None, _JSON_MISSING
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        ok = _preserve_corrupt_file(path, label, e)
        return None, (_JSON_CORRUPT_PRESERVED if ok else _JSON_CORRUPT_UNPRESERVED)
    except OSError as e:
        log.warning("Không đọc được %s (%s): %s", label, path, e)
        return None, _JSON_READ_ERROR
    if not isinstance(data, dict):
        ok = _preserve_corrupt_file(path, label, f"gốc JSON là {type(data).__name__}, cần object")
        return None, (_JSON_CORRUPT_PRESERVED if ok else _JSON_CORRUPT_UNPRESERVED)
    return data, _JSON_OK


def _trim_clip_cache_locked(data: dict) -> bool:
    """Giữ kích thước shopee_clip_cache gọn (mặc định tối đa CLIP_CACHE_MAX_ENTRIES bản ghi),
    ưu tiên giữ các mục còn clip_a / clip_b (cho video 16s) và các mục mới cập nhật gần nhất."""
    if len(data) <= CLIP_CACHE_MAX_ENTRIES:
        return False
    ab_items = []
    other_items = []
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        ts = v.get("updated_at") or 0
        if v.get("clip_a") or v.get("clip_b"):
            ab_items.append((ts, k, v))
        else:
            other_items.append((ts, k, v))
    ab_items.sort(key=lambda x: x[0], reverse=True)
    keep: dict[str, dict] = {}
    for _, k, v in ab_items[:CLIP_CACHE_MAX_ENTRIES]:
        keep[k] = v
    rem = CLIP_CACHE_MAX_ENTRIES - len(keep)
    if rem > 0:
        other_items.sort(key=lambda x: x[0], reverse=True)
        for _, k, v in other_items[:rem]:
            keep[k] = v
    dropped = len(data) - len(keep)
    if dropped > 0:
        data.clear()
        data.update(keep)
        log.info("🧹 [Clip Cache] Đã tỉa bớt %d mục cũ khỏi cache (còn %d mục) để không nghẽn I/O.", dropped, len(data))
        return True
    return False


def _ensure_clip_cache_loaded_locked() -> dict[str, dict]:
    """Đảm bảo _CLIP_CACHE_DATA được nạp trong RAM; chỉ đọc đĩa lần đầu hoặc khi file ngoài thay đổi."""
    global _CLIP_CACHE_DATA, _CLIP_CACHE_PATH_LOADED, _CLIP_CACHE_MTIME, _CLIP_CACHE_DIRTY, _CLIP_CACHE_CAN_WRITE
    path = CLIP_CACHE_FILE
    if _CLIP_CACHE_DATA is not None and _CLIP_CACHE_PATH_LOADED == path:
        if not _CLIP_CACHE_DIRTY:
            try:
                cur_mtime = os.path.getmtime(path) if os.path.isfile(path) else 0.0
            except OSError:
                cur_mtime = _CLIP_CACHE_MTIME
            if abs(cur_mtime - _CLIP_CACHE_MTIME) > 1e-6:
                _CLIP_CACHE_DATA = None
        if _CLIP_CACHE_DATA is not None:
            return _CLIP_CACHE_DATA

    data, status = _load_json_file(path, "clip cache")
    _CLIP_CACHE_CAN_WRITE = status not in (_JSON_CORRUPT_UNPRESERVED, _JSON_READ_ERROR)
    loaded = data if isinstance(data, dict) else {}
    trimmed = _trim_clip_cache_locked(loaded)
    _CLIP_CACHE_DATA = loaded
    _CLIP_CACHE_PATH_LOADED = path
    try:
        _CLIP_CACHE_MTIME = os.path.getmtime(path) if os.path.isfile(path) else 0.0
    except OSError:
        _CLIP_CACHE_MTIME = 0.0
    _CLIP_CACHE_DIRTY = trimmed and _CLIP_CACHE_CAN_WRITE
    if _CLIP_CACHE_DIRTY:
        _schedule_clip_cache_flush_locked()
    return _CLIP_CACHE_DATA


def flush_clip_cache(force: bool = True) -> bool:
    """Ghi gộp shopee_clip_cache từ RAM xuống đĩa theo định dạng JSON compact; không giữ _CLIP_CACHE_LOCK lúc ghi."""
    global _CLIP_CACHE_DIRTY, _CLIP_CACHE_MTIME, _CLIP_CACHE_FLUSH_TIMER
    with _CLIP_CACHE_LOCK:
        if _CLIP_CACHE_FLUSH_TIMER is not None and not force:
            _CLIP_CACHE_FLUSH_TIMER = None
        if _CLIP_CACHE_DATA is None or (not _CLIP_CACHE_DIRTY and len(_CLIP_CACHE_DATA) <= CLIP_CACHE_MAX_ENTRIES) or not _CLIP_CACHE_CAN_WRITE:
            return True
        _trim_clip_cache_locked(_CLIP_CACHE_DATA)
        target_path = _CLIP_CACHE_PATH_LOADED or CLIP_CACHE_FILE
        snapshot = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _CLIP_CACHE_DATA.items()}
        _CLIP_CACHE_DIRTY = False
    with _CLIP_CACHE_FLUSH_LOCK:
        try:
            _atomic_write_json(target_path, snapshot, compact=True)
            try:
                mtime = os.path.getmtime(target_path)
            except OSError:
                mtime = 0.0
            with _CLIP_CACHE_LOCK:
                if (_CLIP_CACHE_PATH_LOADED or CLIP_CACHE_FILE) == target_path:
                    _CLIP_CACHE_MTIME = mtime
            return True
        except Exception as e:
            log.warning("Lỗi ghi clip cache xuống đĩa: %s", e)
            with _CLIP_CACHE_LOCK:
                if (_CLIP_CACHE_PATH_LOADED or CLIP_CACHE_FILE) == target_path:
                    _CLIP_CACHE_DIRTY = True
            return False


def _schedule_clip_cache_flush_locked() -> None:
    global _CLIP_CACHE_FLUSH_TIMER
    if _CLIP_CACHE_FLUSH_TIMER is not None and _CLIP_CACHE_FLUSH_TIMER.is_alive():
        return
    t = threading.Timer(CLIP_CACHE_FLUSH_INTERVAL, flush_clip_cache, kwargs={"force": False})
    t.daemon = True
    _CLIP_CACHE_FLUSH_TIMER = t
    t.start()


atexit.register(flush_clip_cache)


def get_cached_clips(item_id: str) -> dict:
    """Lấy các clip thành phần (clip_a / clip_b / clip_raw) còn tồn tại trên đĩa, kèm image_url đã lưu.

    image_url không phải file clip nên không kiểm tra tồn tại: set_cached_clip ghi nó vào cùng entry
    và đây là nguồn ảnh dự phòng để ghép Outro 12s khi danh sách sản phẩm trên giao diện đã bị xóa.
    """
    return get_cached_clips_many([item_id]).get(str(item_id), {})


def _valid_cache_entry(item_data) -> dict:
    if not isinstance(item_data, dict):
        return {}
    valid = {}
    for k in ("clip_a", "clip_b", "clip_raw"):
        fname = item_data.get(k)
        if fname:
            fpath = fname if os.path.isabs(fname) else os.path.join(CFG.media_dir, fname)
            if os.path.isfile(fpath) and os.path.getsize(fpath) > 10000:
                valid[k] = fname
    img = str(item_data.get("image_url") or "").strip()
    if img:
        valid["image_url"] = img
    return valid


def get_cached_clips_many(item_ids) -> dict[str, dict]:
    """Như get_cached_clips nhưng đọc trực tiếp từ cache RAM (nhả khóa trước khi stat file trên đĩa)."""
    with _CLIP_CACHE_LOCK:
        data = _ensure_clip_cache_loaded_locked()
        raw_hits = {}
        for iid in item_ids:
            sid = str(iid)
            v = data.get(sid)
            if isinstance(v, dict):
                raw_hits[sid] = dict(v)
    if not raw_hits:
        return {}
    out = {}
    for sid, entry_raw in raw_hits.items():
        try:
            entry = _valid_cache_entry(entry_raw)
        except Exception as e:  # noqa: BLE001
            log.warning("Lỗi đọc clip cache cho %s: %s", sid, e)
            continue
        if entry:
            out[sid] = entry
    return out


def set_cached_clips(item_id: str, updates: dict[str, str]) -> bool:
    """Cập nhật nhiều khóa clip cache cùng lúc trong RAM và ghi gộp nền xuống đĩa (0ms chờ I/O)."""
    global _CLIP_CACHE_DIRTY
    clean_updates = {k: v for k, v in (updates or {}).items() if v}
    if not clean_updates:
        return True
    with _CLIP_CACHE_LOCK:
        data = _ensure_clip_cache_loaded_locked()
        if not _CLIP_CACHE_CAN_WRITE:
            log.error("Bỏ qua ghi clip cache (%s) vì file hiện tại không đọc được/không sao lưu được.", item_id)
            return False
        str_id = str(item_id)
        entry = data.get(str_id)
        if not isinstance(entry, dict):
            entry = {}
            data[str_id] = entry
        entry.update(clean_updates)
        entry["updated_at"] = int(time.time())
        if len(data) > CLIP_CACHE_MAX_ENTRIES + 2000:
            _trim_clip_cache_locked(data)
        _CLIP_CACHE_DIRTY = True
        _schedule_clip_cache_flush_locked()
        return True


def set_cached_clip(item_id: str, clip_key: str, filename: str) -> bool:
    """Lưu lại tên file clip thành phần đã render thành công của sản phẩm (cập nhật RAM + flush gộp nền)."""
    return set_cached_clips(item_id, {clip_key: filename})


def clear_cached_clip_keys(item_id: str, keys: tuple[str, ...] | list[str]) -> None:
    """Xóa các khóa tạm (vd clip_raw sau khi đã ghép/xóa) khỏi cache để không giữ rác."""
    global _CLIP_CACHE_DIRTY
    str_id = str(item_id or "").strip()
    if not str_id or not keys:
        return
    with _CLIP_CACHE_LOCK:
        data = _ensure_clip_cache_loaded_locked()
        entry = data.get(str_id)
        if not isinstance(entry, dict):
            return
        changed = False
        for k in keys:
            if k in entry:
                entry.pop(k, None)
                changed = True
        # Nếu chỉ còn updated_at (không còn clip_a/clip_b/clip_raw/image_url), bỏ hẳn mục cho gọn
        if not any(k in entry for k in ("clip_a", "clip_b", "clip_raw", "image_url")):
            data.pop(str_id, None)
            changed = True
        if changed and _CLIP_CACHE_CAN_WRITE:
            _CLIP_CACHE_DIRTY = True
            _schedule_clip_cache_flush_locked()


BLACKLIST_FILE = os.path.join(CFG.data_dir, "shopee_blacklist.json")

DEFAULT_BRAND_POLICY_KEYWORDS = [
    "disney", "marvel", "nike", "air jordan", "adidas", "gucci", "chanel", "louis vuitton",
    "hermes", "fendi", "burberry", "ysl", "yves saint laurent",
    "apple", "iphone", "airpods", "macbook", "ipad", "samsung",
    "doraemon", "hello kitty", "pokemon", "pikachu", "naruto", "one piece",
    "dragon ball", "demon slayer", "pop mart", "labubu",
    "rolex", "zara", "h&m", "dior", "prada", "versace", "balenciaga",
    "sanrio", "cinnamoroll", "my melody", "kuromi", "marlboro",
]

DEFAULT_BLACKLIST = {
    "enabled": True,
    "auto_block_safety": True,
    "filter_brand_policy": True,
    "filter_shopee_policy": True,
    "keywords": [
        "vape", "vape pod", "pod kit", "thuốc lá", "thuoc la", "cigar", "cigarette", "xì gà", "xi ga",
        "bao cao su", "kích dục", "kich duc", "sextoy", "sex toy", "tình dục", "tinh duc",
        "18+", "đồ chơi người lớn", "dao găm", "súng", "sung", "vũ khí", "vu khi",
        "fake", "replica", "hàng nhái", "hang nhai", "shisha", "bóng cười", "bong cuoi"
    ],
    "brand_keywords": list(DEFAULT_BRAND_POLICY_KEYWORDS),
    "item_ids": []
}


def _default_blacklist() -> dict:
    """Bản sao sâu của DEFAULT_BLACKLIST — tránh việc sửa list bên trong làm hỏng giá trị mặc định của module."""
    return copy.deepcopy(DEFAULT_BLACKLIST)


def get_blacklist() -> dict:
    """Lấy danh sách Blacklist (từ khóa cấm, luật Shopee PH/MY/ID, thương hiệu/bản quyền & Item ID cấm)."""
    with _BLACKLIST_LOCK:
        data, _status = _load_json_file(BLACKLIST_FILE, "blacklist")
    if data is None:
        data = _default_blacklist()
    if not isinstance(data.get("keywords"), list):
        data["keywords"] = copy.deepcopy(DEFAULT_BLACKLIST["keywords"])
    if "brand_keywords" not in data or not isinstance(data.get("brand_keywords"), list):
        data["brand_keywords"] = copy.deepcopy(DEFAULT_BRAND_POLICY_KEYWORDS)
    if not isinstance(data.get("item_ids"), list):
        data["item_ids"] = []
    if "enabled" not in data:
        data["enabled"] = True
    if "auto_block_safety" not in data:
        data["auto_block_safety"] = bool(data.get("auto_block_safety_errors", True))
    if "filter_brand_policy" not in data:
        data["filter_brand_policy"] = True
    if "filter_shopee_policy" not in data:
        data["filter_shopee_policy"] = True
    # Giữ tương thích ngược cho frontend cũ đọc auto_block_safety_errors
    data["auto_block_safety_errors"] = bool(data["auto_block_safety"])
    return data


def save_blacklist(data: dict) -> bool:
    """Lưu danh sách Blacklist xuống file data/shopee_blacklist.json (dưới khóa, ghi nguyên tử)."""
    with _BLACKLIST_LOCK:
        try:
            def _clean_kws(raw_list) -> list[str]:
                out = []
                for kw in raw_list or []:
                    k = unicodedata.normalize("NFC", str(kw)).strip().lower()
                    if k and k not in out:
                        out.append(k)
                return out

            kw_list = _clean_kws(data.get("keywords", []))
            cur_bl = get_blacklist()
            if "brand_keywords" in data and isinstance(data.get("brand_keywords"), list):
                brand_kw_list = _clean_kws(data.get("brand_keywords"))
            else:
                brand_kw_list = _clean_kws(cur_bl.get("brand_keywords", DEFAULT_BRAND_POLICY_KEYWORDS))

            ids_list = []
            for iid in data.get("item_ids", []) or []:
                i = str(iid).strip()
                if i and i not in ids_list:
                    ids_list.append(i)

            if "auto_block_safety" in data:
                auto_block = bool(data.get("auto_block_safety"))
            elif "auto_block_safety_errors" in data:
                auto_block = bool(data.get("auto_block_safety_errors"))
            else:
                auto_block = True

            if "filter_brand_policy" in data:
                filter_brand = bool(data.get("filter_brand_policy"))
            else:
                filter_brand = bool(cur_bl.get("filter_brand_policy", True))

            if "filter_shopee_policy" in data:
                filter_shopee = bool(data.get("filter_shopee_policy"))
            else:
                filter_shopee = bool(cur_bl.get("filter_shopee_policy", True))

            clean_data = {
                "enabled": bool(data.get("enabled", True)),
                "auto_block_safety": auto_block,
                "filter_brand_policy": filter_brand,
                "filter_shopee_policy": filter_shopee,
                "keywords": kw_list,
                "brand_keywords": brand_kw_list,
                "item_ids": ids_list,
                "updated_at": int(time.time())
            }
            # Không ghi đè file hỏng khi chưa sao lưu được nó (.corrupt-<ts>).
            if os.path.isfile(BLACKLIST_FILE):
                _old, status = _load_json_file(BLACKLIST_FILE, "blacklist")
                if status == _JSON_CORRUPT_UNPRESERVED:
                    log.error("Không lưu blacklist: file hiện tại bị hỏng và chưa sao lưu được.")
                    return False
            _atomic_write_json(BLACKLIST_FILE, clean_data)
            return True
        except Exception as e:
            log.warning("Lỗi lưu blacklist: %s", e)
            return False


def add_to_blacklist(item_id: str | None = None, keyword: str | None = None, reason: str = "") -> dict:
    """Thêm một Item ID hoặc Từ khóa vào Blacklist (giữ khóa xuyên suốt đọc -> sửa -> ghi)."""
    with _BLACKLIST_LOCK:
        bl = get_blacklist()
        added = []
        if item_id:
            sid = str(item_id).strip()
            if sid and sid not in bl.get("item_ids", []):
                bl.setdefault("item_ids", []).append(sid)
                added.append(f"ID {sid}")
        if keyword:
            skw = unicodedata.normalize("NFC", str(keyword)).strip().lower()
            if skw and skw not in bl.get("keywords", []):
                bl.setdefault("keywords", []).append(skw)
                added.append(f"Từ khóa '{skw}'")
        if added:
            if save_blacklist(bl):
                log.info("🚫 Đã thêm vào Blacklist (%s): %s", reason or "manual", ", ".join(added))
            else:
                log.warning("Không lưu được Blacklist khi thêm: %s", ", ".join(added))
    return bl


# ---------- So khớp từ khóa Blacklist (theo ranh giới từ, có xử lý dấu tiếng Việt) ----------
# Ranh giới từ = không đứng cạnh chữ cái/chữ số (dấu "_" được coi là ký tự phân tách).
_BL_LEFT = r"(?<![^\W_])"
_BL_RIGHT = r"(?![^\W_])"
_BL_SEP = r"[\s\-_]+"  # cụm nhiều từ: cho phép nhiều khoảng trắng / gạch nối / gạch dưới giữa các từ


def _strip_accents(s: str) -> str:
    """Bỏ dấu tiếng Việt / Latin: NFD + bỏ dấu kết hợp (Mn), 'đ' -> 'd'."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s.replace("đ", "d").replace("Đ", "D")


def _has_latin_diacritics(s: str) -> bool:
    """True nếu chuỗi có chữ Latin mang dấu (ví dụ 'ổ', 'ú', 'đ') — tức người bán gõ tiếng Việt có dấu."""
    for ch in s:
        if ch in ("đ", "Đ"):
            return True
        if ord(ch) > 127 and ch.isalpha():
            dec = unicodedata.normalize("NFD", ch)
            if len(dec) > 1 and dec[0].isascii():
                return True
    return False


def _bl_pattern(kw: str) -> re.Pattern | None:
    parts = [p for p in re.split(_BL_SEP, kw) if p]
    if not parts:
        return None
    body = _BL_SEP.join(re.escape(p) for p in parts)
    # Từ khóa tiếng Anh (ASCII) kết thúc bằng chữ cái: cho phép số nhiều "s"/"es" (pods, vapes, cigars).
    suffix = "(?:e?s)?" if kw.isascii() and kw[-1].isalpha() else ""
    return re.compile(_BL_LEFT + body + suffix + _BL_RIGHT)


_BL_CACHE_LOCK = threading.Lock()
_BL_COMPILED_CACHE: dict[tuple, list] = {}


def _compile_blacklist_keywords(keywords: tuple) -> list[tuple[str, str, re.Pattern | None, re.Pattern | None]]:
    """Biên dịch danh sách từ khóa thành [(kw, loại, pattern_có_dấu, pattern_không_dấu)].
    loại:
      - "accented": từ khóa có dấu (súng, thuốc lá) -> khớp NGUYÊN DẤU với tên có dấu; với tên gõ không dấu
        thì khớp dạng bỏ dấu.
      - "alias":    từ khóa ASCII trùng dạng bỏ dấu của 1 từ khóa có dấu trong danh sách (sung <-> súng,
        thuoc la <-> thuốc lá) -> CHỈ áp dụng cho tên gõ không dấu (tránh 'bổ sung' bị coi là 'súng').
      - "ascii":    từ khóa ASCII thuần (vape, pod, cigar, 18+, bao cao su) -> khớp mọi tên (đã bỏ dấu).
    """
    with _BL_CACHE_LOCK:
        cached = _BL_COMPILED_CACHE.get(keywords)
    if cached is not None:
        return cached
    norm_kws = []
    for kw in keywords:
        k = unicodedata.normalize("NFC", str(kw if kw is not None else "")).strip().lower()
        if k and k not in norm_kws:
            norm_kws.append(k)
    accented_plain = {_strip_accents(k) for k in norm_kws if _strip_accents(k) != k}
    compiled = []
    for k in norm_kws:
        plain = _strip_accents(k)
        if plain != k:
            compiled.append((k, "accented", _bl_pattern(k), _bl_pattern(plain)))
        elif k in accented_plain:
            compiled.append((k, "alias", None, _bl_pattern(k)))
        else:
            compiled.append((k, "ascii", None, _bl_pattern(k)))
    with _BL_CACHE_LOCK:
        if len(_BL_COMPILED_CACHE) > 32:
            _BL_COMPILED_CACHE.clear()
        _BL_COMPILED_CACHE[keywords] = compiled
    return compiled


def _match_blacklist_keyword(name: str, keywords) -> str | None:
    """Trả về từ khóa cấm đầu tiên khớp với tên sản phẩm (theo ranh giới từ), hoặc None."""
    name_nfc = unicodedata.normalize("NFC", str(name or "")).lower()
    if not name_nfc.strip():
        return None
    name_plain = _strip_accents(name_nfc)
    name_has_diacritics = _has_latin_diacritics(name_nfc)
    try:
        key = tuple(str(k) for k in (keywords or []))
    except TypeError:
        return None
    for kw, kind, pat_acc, pat_plain in _compile_blacklist_keywords(key):
        if kind == "accented":
            if pat_acc is not None and pat_acc.search(name_nfc):
                return kw
            if not name_has_diacritics and pat_plain is not None and pat_plain.search(name_plain):
                return kw
        elif kind == "alias":
            if not name_has_diacritics and pat_plain is not None and pat_plain.search(name_plain):
                return kw
        else:
            if pat_plain is not None and pat_plain.search(name_plain):
                return kw
    return None


# ---------- Bộ luật Shopee Listing Violations đa thị trường (PH 24949 + MY 614 + ID 19582) ----------
# Mỗi luật gồm: (mã_nhóm, tên_nhóm, nguồn_chính_sách, regex_vi_phạm, regex_loại_trừ_bắt_nhầm)
# Đã đối chiếu trên 32.000 SP thật (PH/VN/MY/ID) để tránh bắt nhầm phụ kiện, đồ gia dụng, từ đồng âm.
_SHOPEE_POLICY_RULE_SPECS: tuple[tuple[str, str, str, str, str | None], ...] = (
    (
        "weapons",
        "Vũ khí / Súng / Tự vệ",
        "PH 24949 (11354) · MY 614 · ID",
        r"\b(?:firearms?|handguns?|revolvers?|rifles?|shotguns?|ammunitions?|airsoft\s+guns?|pellet\s+guns?|bb\s+guns?|"
        r"stun\s+guns?|tasers?|pepper\s+sprays?|tear\s+gas|brass\s+knuckles?|knuckle\s+dusters?|"
        r"switchblades?|butterfly\s+kni(?:fe|ves)|balisongs?|karambits?|kerambits?|machetes?|"
        r"tactical\s+kni(?:fe|ves)|throwing\s+kni(?:fe|ves)|crossbows?|kubotans?|bulletproof\s+vests?|body\s+armor|"
        r"senapan\s+angin|senjata\s+api|senjata\s+tajam|sajam|buku\s+lima|sangkur|celurit|alat\s+setrum|"
        r"renjatan\s+elektrik|semburan\s+lada|semprotan\s+merica|bình\s+xịt\s+hơi\s+cay|binh\s+xit\s+hoi\s+cay|roi\s+điện|roi\s+dien)\b",
        r"\b(?:glue\s+gun|súng\s+bắn\s+keo|súng\s+bắn\s+ghim|massage\s+gun|heat\s+gun|spray\s+gun|water\s+gun|nail\s+gun|bubble\s+gun|toy\s+gun)\b",
    ),
    (
        "adult_sexual",
        "Người lớn 18+ / Kích dục",
        "PH 24949 (25010) · MY 614 · ID",
        r"\b(?:sex\s*toys?|dildos?|vibrators?|fleshlights?|masturbators?|butt\s+plugs?|anal\s+plugs?|"
        r"bdsm|bondage|erotic|pornographic|porn|hentai|sex\s+dolls?|love\s+dolls?|blow[\s\-]+up\s+dolls?|"
        r"nipple\s+clamps?|cock\s+rings?|penis\s+rings?|penis\s+pumps?|penis\s+enlargers?|"
        r"genital\s+enlargement|erection\s+boost|delay\s+sprays?|aphrodisiacs?|condoms?|"
        r"pampatigas|pampalaki|robust\s+extreme|titan\s+gel|"
        r"ubat\s+kuat|gambir\s+sarawak|gambir\s+emas|candy\s+b|soloco|"
        r"obat\s+kuat|obat\s+perangsang|perangsang\s+(?:wanita|pria)|pembesar\s+penis|alat\s+bantu\s+sex|"
        r"kondom|tisu\s+magic|hajar\s+jahanam|viagra|cialis|kamagra|"
        r"đồ\s+chơi\s+người\s+lớn|do\s+choi\s+nguoi\s+lon|bao\s+cao\s+su|kích\s+dục|kich\s+duc)\b",
        r"\b(?:skun|kabel|cable|terminal\s+skun)\b",
    ),
    (
        "tobacco_vape_alcohol",
        "Thuốc lá / Vape / Rượu bia",
        "PH 24949 (25019/18262) · MY 614 · ID",
        r"\b(?:vapes?|vaping|e[\-\s]?cigarettes?|e[\-\s]?cigs?|e[\-\s]?liquids?|e[\-\s]?juices?|"
        r"nicotine|salt\s+nic|nic\s+salt|vape\s+pods?|disposable\s+pods?|pod\s+kits?|pod\s+vapes?|"
        r"relx|iqos|heets|terea|juul|puff\s+bars?|vape\s+coils?|vape\s+juices?|flavour\s+vape|liquid\s+vape|likuid\s+vape|occ\s+vape|cartridge\s+vape|"
        r"cigarettes?|cigars?|tobacco|shisha|hookahs?|rolling\s+papers?|snus|chewing\s+tobacco|"
        r"rokok|tembakau|cerut|cerutu|thuốc\s+lá|thuoc\s+la|xì\s+gà|xi\s+ga|bóng\s+cười|bong\s+cuoi|"
        r"vodka|tequila|cognac|bourbon|soju|red\s+horse\s+beer|san\s+miguel\s+beer|craft\s+beer|"
        r"emperador\s+(?:light|brandy)|tanduay\s+(?:rum|rhum|select)|alfonso\s+(?:light|brandy)|"
        r"alcoholic\s+beverages?|bootleg\s+alcohol|minuman\s+keras|miras|anggur\s+merah|arak\s+(?:bali|putih|beras))\b",
        r"\b(?:stain\s+removal|toothpaste|ubat\s+gigi|ice\s+mold|cake\s+topper|glass(?:es)?|opener|coaster)\b",
    ),
    (
        "drugs_illicit",
        "Ma túy & Chất cấm",
        "PH 24949 (25060) · MY 614 · ID",
        r"\b(?:cannabis|marijuana|kush|thc|cbd\s+oil|hemp\s+oil|kratom|ketum|daun\s+ketum|"
        r"cocaine|heroin|methamphetamine|shabu|syabu|sabu|ecstasy|ekstasi|psilocybin|"
        r"opium|morphine|fentanyl|codeine|tramadol|xanax|valium|ketamine|poppers|nitrous\s+oxide|"
        r"narkoba|narkotika|ganja|dadah|ma\s+túy|ma\s+tuy|cần\s+sa|can\s+sa)\b",
        r"\b(?:urine\s+drug\s+test|test\s+kit)\b",
    ),
    (
        "medical_rx_fda",
        "Thuốc kê đơn / Thiết bị y tế xâm lấn / FDA / Thú y",
        "PH 24949 (25021/25063) · MY 614",
        r"\b(?:rx\s+only|prescription\s+(?:drug|medicine|only)|antibiotics?|amoxicillin|azithromycin|ciprofloxacin|"
        r"metformin|losartan|amlodipine|mefenamic|omeprazole|anabolic\s+steroids?|steroids?|sarms|ivermectin|"
        r"antidepressants?|opioids?|abortion|cytotec|misoprostol|obat\s+aborsi|penggugur\s+kandungan|"
        r"gluta\s+drip|glutathione\s+iv|iv\s+push|botox|dermal\s+fillers?|mesotherapy|"
        r"hydroquinone|tretinoin|isotretinoin|clobetasol|ketoconazole|"
        r"hypodermic|disposable\s+syringes?|insulin\s+syringes?|iv\s+cannula|catheters?|scalpels?|surgical\s+blades?|"
        r"diy\s+braces|slimming\s+pills?|diet\s+pills?|weight\s+loss\s+pills?|appetite\s+suppressants?|"
        r"vet\s+medicine|veterinary\s+(?:medicine|drug|injection)|thuốc\s+thú\s+y|thuoc\s+thu\s+y|thuốc\s+kê\s+đơn|"
        r"stage\s*1\s+infant\s+milk|step\s*1\s+infant\s+milk|susu\s+formula\s+0[\-\s]?6)\b",
        r"\b(?:chiết\s+nước\s+hoa|perfume\s+refill|veterinary\s+diet|cat\s+food|dog\s+food|kucing|anjing)\b",
    ),
    (
        "chemicals_pesticides",
        "Hóa chất độc hại / Thuốc trừ sâu cấm / Chất nổ",
        "PH 24949 (25011) · MY 614 (DOA) · ID",
        r"\b(?:firecrackers?|pyrotechnics?|gunpowder|detonators?|petasan|bahan\s+peledak|pháo\s+hoa|thuốc\s+nổ|"
        r"muriatic\s+acid|hydrochloric\s+acid|sulfuric\s+acid|nitric\s+acid|caustic\s+soda|"
        r"potassium\s+nitrate|cyanide|arsenic|chloroform|asbestos|merkuri|formalin|boraks|"
        r"paraquat|pentachlorophenol|trichlorphon|dicofol|monocrotophos|chlorpyrifos|zinc\s+phosphide|"
        r"herbicides?|weed\s+killers?|rodenticides?|rat\s+poisons?|racun\s+rumpai|racun\s+rumput|racun\s+perosak|racun\s+tikus|"
        r"pestisida|thuốc\s+diệt\s+chuột|thuốc\s+trừ\s+sâu|magnetic\s+putty|burning\s+laser)\b",
        r"\b(?:insecticide[\-\s]?free|pesticide[\-\s]?free|tanpa\s+pestisida|bebas\s+pestisida|no\s+pesticide|mercury[\-\s]?free|formalin[\-\s]?free|test\s+kit|detector)\b",
    ),
    (
        "gov_royalty_ids",
        "Đồng phục / Giấy tờ chính phủ / Hoàng gia",
        "PH 24949 (25012) · MY 614 · ID",
        r"\b(?:pnp\s+uniform|afp\s+uniform|police\s+uniform|military\s+uniform|army\s+uniform|"
        r"bfp\s+uniform|bjmp\s+uniform|coast\s+guard\s+uniform|police\s+badge|police\s+blinker|wang\s+wang\s+siren|"
        r"rela\s+uniform|bomba\s+uniform|darjah\s+kebesaran|pingat\s+persekutuan|"
        r"government\s+id|fake\s+id|tin\s+id|philhealth\s+id|postal\s+id|drivers?\s+license|"
        r"lto\s+plate|replica\s+plate|mykad|ic\s+palsu|sijil\s+halal|halal\s+certificate|"
        r"ktp\s+palsu|sim\s+palsu|ijazah\s+palsu|surat\s+dokter\s+palsu|stnk\s+palsu|bpkb\s+palsu|materai\s+palsu|"
        r"đồng\s+phục\s+công\s+an|quân\s+phục)\b",
        None,
    ),
    (
        "prohibited_services",
        "Dịch vụ cấm / Doxing / Dữ liệu cá nhân / Tâm linh",
        "ID 19582 · MY 614 · PH 24949 (25062)",
        r"\b(?:jasa\s+doxing|doxing|jasa\s+hack|jasa\s+sadap|sadap\s+wa|sadap\s+whatsapp|"
        r"jasa\s+lacak|jasa\s+bobol|jasa\s+phising|jual\s+database|database\s+nomor|database\s+rekening|"
        r"jual\s+rekening|jasa\s+gestun|gesek\s+tunai|jasa\s+pinjol|joki\s+pinjol|jasa\s+galbay|"
        r"bi\s+checking|jasa\s+slik|jasa\s+edit\s+ktp|jasa\s+bikin\s+ktp|jasa\s+surat\s+sakit|"
        r"jasa\s+followers?|jasa\s+like|jasa\s+buzzer|jasa\s+review\s+palsu|jasa\s+fake\s+order|jasa\s+suntik|"
        r"open\s+bo|video\s+call\s+sex|pijat\s+plus|escort\s+service|webcam\s+service|"
        r"jasa\s+santet|jasa\s+pelet|penglaris|pesugihan|jasa\s+dukun|jasa\s+ramal|jasa\s+tarot|"
        r"tarot\s+reading|fortune\s+telling|bomoh|santau|minyak\s+dagu|minyak\s+pengasih|"
        r"tangkal|azimat|ritual\s+service|jual\s+kyc|verifikasi\s+kyc|"
        r"akun\s+dana\s+premium|akun\s+ovo|akun\s+gopay|akun\s+shopee|verified\s+gcash|gcash\s+account|maya\s+account)\b",
        None,
    ),
    (
        "digital_restricted_misc",
        "Hàng số / Phá sóng / TV Box / Tiền tệ / Cờ bạc / Động vật sống",
        "PH 24949 (25017/25062/25590/26803/25013) · MY 614",
        r"\b(?:signal\s+jammers?|gps\s+jammers?|wifi\s+jammers?|rf\s+jammers?|cell\s+phone\s+jammers?|"
        r"wiretaps?|telephone\s+bugging|cable\s+tv\s+descramblers?|radar\s+scanners?|lockpicks?|lock\s+picks?|bump\s+keys?|card\s+skimmers?|"
        r"android\s+tv\s+box|iptv\s+box|svicloud|evpad|unblock\s+tech|mxq\s+pro|"
        r"netflix\s+account|spotify\s+premium|canva\s+pro|chatgpt\s+plus|cracked\s+software|pirated\s+software|"
        r"digital\s+download|digital\s+product|activation\s+key|license\s+key|windows\s+key|"
        r"robux|mlbb\s+diamonds?|codm\s+cp|valorant\s+points?|steam\s+wallet|game\s+credits?|"
        r"concert\s+tickets?|event\s+tickets?|movie\s+tickets?|tiket\s+konsert|"
        r"prop\s+money|uang\s+palsu|specimen\s+bank\s*notes?|gold\s+bars?|bullion|bitcoin|usdt|"
        r"house\s+and\s+lot|condo\s+for\s+sale|lot\s+for\s+sale|used\s+car\s+for\s+sale|motorcycle\s+for\s+sale|"
        r"gambling|casino|slot\s+machines?|roulette|sabong|cockfighting|tari\s+sabong|jueteng|"
        r"lottery\s+tickets?|raffle\s+tickets?|judi|slot\s+gacor|chip\s+higgs\s+domino|chip\s+domino|togel|cờ\s+bạc|tài\s+xỉu|lô\s+đề|"
        r"remove\s+barcode|buang\s+barcode|cut\s+qr\s+code|buang\s+qr|"
        r"live\s+fish|live\s+betta|live\s+shrimp|live\s+turtle|live\s+bird|live\s+puppy|live\s+kitten|live\s+reptile|live\s+snake|"
        r"elephant\s+ivory|gading\s+gajah|shark\s+fin|taxidermy|human\s+skull|human\s+bone|human\s+organ|nazi|swastika|hitler)\b",
        r"\b(?:remote\s+control|replacement\s+remote|remote\s+for|storage\s+holder|wall\s+mount|rack|bracket|stand|money\s+detector|bill\s+checker)\b",
    ),
    (
        "counterfeit_spam",
        "Hàng nhái / Kéo giao dịch ngoài sàn / Link checkout ảo",
        "PH 24949 (1375/3101/24989) · MY 614 · ID",
        r"(?:https?://|www\.[a-z0-9\-]+\.[a-z]{2,}|(?:facebook|fb|instagram|tiktok)\.com/|t\.me/|wa\.me/)|"
        r"\b(?:super\s+fake|aaa\s+grade|top\s+grade|1:1\s+copy|mirror\s+copy|high\s+copy|knockoffs?|bootlegs?|"
        r"ua\s+pair|unauthorized\s+authentic|oem\s+replica|kw\s+super|grade\s+ori|miror\s+1:1|hàng\s+nhái|hang\s+nhai|"
        r"live\s+selling\s+checkout|live\s+checkout\s+link|mine\s+checkout|for\s+live\s+checkout|"
        r"checkout\s+link\s+only|do\s+not\s+checkout|shipping\s+fee\s+only|link\s+checkout\s+live|khusus\s+live\s+checkout|"
        r"gcash\s+direct|outside\s+shopee)\b",
        r"\b(?:money\s+detector|counterfeit\s+detector|bill\s+checker)\b",
    ),
)

_SHOPEE_POLICY_COMPILED = tuple(
    (key, label, src, re.compile(pat, re.I), re.compile(exc, re.I) if exc else None)
    for key, label, src, pat, exc in _SHOPEE_POLICY_RULE_SPECS
)


def get_shopee_policy_groups() -> list[dict]:
    """Danh sách tóm tắt các nhóm luật vi phạm Shopee (PH 24949 + MY 614 + ID 19582) cho giao diện."""
    return [{"key": k, "label": lbl, "sources": src} for k, lbl, src, _p, _e in _SHOPEE_POLICY_RULE_SPECS]


def match_shopee_policy_violation(name: str) -> tuple[str | None, str, str]:
    """Kiểm tra tên sản phẩm theo bộ luật Shopee Listing Violations (PH/MY/ID).
    Trả về (group_key, group_label, matched_term) hoặc (None, '', '')."""
    s = unicodedata.normalize("NFC", str(name or "")).strip()
    if not s:
        return None, "", ""
    for key, label, _src, pat, exc in _SHOPEE_POLICY_COMPILED:
        m = pat.search(s)
        if m and not (exc and exc.search(s)):
            return key, label, m.group(0).strip()
    return None, "", ""


def is_product_blacklisted(product: dict, blacklist_data: dict | None = None) -> tuple[bool, str]:
    """Kiểm tra sản phẩm có vi phạm Blacklist / Luật Shopee PH-MY-ID / Thương hiệu bản quyền không."""
    bl = blacklist_data if blacklist_data is not None else get_blacklist()
    if not bl.get("enabled", True):
        return False, ""

    # 1. Kiểm tra Item ID
    iid_str = str(product.get("item_id", "")).strip()
    blacklisted_ids = {str(x).strip() for x in bl.get("item_ids", []) if str(x).strip()}
    if iid_str and iid_str in blacklisted_ids:
        return True, f"ID {iid_str} nằm trong danh sách đen (Blacklist)"

    # 2. Kiểm tra tên sản phẩm
    name = product.get("name") or ""
    if not str(name).strip():
        return False, ""

    hit = _match_blacklist_keyword(name, bl.get("keywords", []))
    if hit:
        return True, f"Chứa từ khóa cấm '{hit}'"

    # 3. Bộ luật Listing Violations đa thị trường Shopee (PH 24949 + MY 614 + ID 19582)
    if bl.get("filter_shopee_policy", True):
        _gkey, glabel, term = match_shopee_policy_violation(name)
        if _gkey:
            return True, f"Vi phạm CS Shopee [{glabel}]: '{term}'"

    # 4. Thương hiệu / Bản quyền (IP Infringement)
    if bl.get("filter_brand_policy", True):
        brand_hit = _match_blacklist_keyword(name, bl.get("brand_keywords", []))
        if brand_hit:
            return True, f"Vi phạm thương hiệu / bản quyền ('{brand_hit}')"

    return False, ""


_POLICY_VIOLATION_ERR_RE = re.compile(
    r"safety|policy|nsfw|prohibited|copyright|trademark|violat|"
    r"vi\s*phạm|bản\s*quyền|kiểm\s*duyệt|từ\s*chối\s*(?:nội\s*dung|sản\s*xuất|tạo)|"
    r"logo\s*thương\s*hiệu|thuốc\s*(?:lá|thú\s*y)|veterinary|không\s*làm\s*tvc|"
    r"không\s*(?:thể\s*tạo|thể\s*làm|làm)\s*video[^\n]{0,50}(?:cho\s*(?:sản\s*phẩm|mẫu|món)|được)|"
    r"against\s*(?:our|the)\s*polic",
    re.I,
)


def is_policy_violation_error(message: str) -> bool:
    """Nhận diện lỗi do chính sản phẩm vi phạm chính sách/bản quyền (không phải lỗi tài khoản hay lỗi dịch vụ Muse)."""
    msg = str(message or "")
    if not msg or is_account_fault(msg):
        return False
    return bool(_POLICY_VIOLATION_ERR_RE.search(msg))


def _nvenc_try_order(use_gpu: bool) -> list[bool]:
    """Chỉ cấp slot GPU NVENC (tối đa _NVENC_MAX_SLOTS luồng cùng lúc); hết slot thì đi thẳng CPU libx264."""
    return [True, False] if use_gpu else [False]


def _run_encode_with_gpu_fallback(build_cmd_fn, output_path: str, timeout: int = 90) -> tuple[bool, str]:
    """Thử encode qua GPU NVENC nếu có slot rảnh trong _NVENC_SEMAPHORE, ngược lại chạy CPU libx264 ngay."""
    use_gpu = has_nvenc()
    got_gpu_slot = False
    try:
        if use_gpu:
            got_gpu_slot = _NVENC_SEMAPHORE.acquire(blocking=False)
        order = [True, False] if got_gpu_slot else [False]
        last_err = ""
        for try_gpu in order:
            cmd = build_cmd_fn(try_gpu)
            res = _run_ffmpeg_cmd(cmd, timeout=timeout)
            if res.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 1000:
                return True, ""
            last_err = (res.stderr.decode("utf-8", errors="ignore") if isinstance(res.stderr, bytes) else str(res.stderr))[-400:]
            _safe_remove(output_path)
            if try_gpu and got_gpu_slot:
                _NVENC_SEMAPHORE.release()
                got_gpu_slot = False
        return False, last_err
    finally:
        if got_gpu_slot:
            _NVENC_SEMAPHORE.release()


def concat_videos(clip_paths: list[str], output_path: str) -> bool:
    """Ghép nhiều clip video thành 1 video 16s hoàn chỉnh bằng FFmpeg (giới hạn đồng thời qua FFMPEG_SEMAPHORE,
    tiến trình ưu tiên thấp). Thất bại thì xóa file output dở dang."""
    if not clip_paths:
        return False
    if len(clip_paths) == 1:
        import shutil
        shutil.copy2(clip_paths[0], output_path)
        return True

    t0 = time.time()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    list_file = os.path.join(tempfile.gettempdir(), f"concat_{int(time.time()*1000)}_{random.randint(100, 999)}.txt")
    ok = False
    with FFMPEG_SEMAPHORE:
        try:
            with open(list_file, "w", encoding="utf-8") as f:
                for p in clip_paths:
                    clean_p = p.replace(os.sep, "/").replace("'", "'\\''")
                    f.write(f"file '{clean_p}'\n")

            # Thử 1: stream copy (siêu nhanh ~0.1s, giữ nguyên 100% chất lượng, 0% CPU)
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", list_file,
                "-c", "copy",
                "-fflags", "+genpts",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                output_path
            ]
            res = _run_ffmpeg_cmd(cmd, timeout=120)
            if res.returncode == 0 and os.path.isfile(output_path):
                # Kiểm tra lỗi tràn số 16-bit timebase (khiến Windows Explorer hiện 01:05:32)
                try:
                    test_dur = get_media_duration(output_path)
                    if test_dur > 25.0:
                        log.warning("concat_videos stream-copy bị lệch timebase (độ dài đọc được %.1fs > 25s), hủy và chuyển sang reencode...", test_dur)
                        _safe_remove(output_path)
                        res.returncode = 1
                except Exception:
                    pass

            if res.returncode != 0:
                err1 = (res.stderr.decode("utf-8", errors="ignore") if isinstance(res.stderr, bytes) else str(res.stderr))[-400:]
                log.warning("concat_videos copy failed: %s; falling back to reencode", err1)
                _safe_remove(output_path)  # bỏ output dở dang của lần stream-copy
                # Thử 2: re-encode nếu stream khác codec hoặc timeline (NVENC có kiểm soát slot -> CPU libx264)
                ok_reenc, err2 = _run_encode_with_gpu_fallback(
                    lambda try_gpu: [
                        "ffmpeg", "-y",
                        *_ffmpeg_global_thread_args(),
                        "-reinit_filter", "0",  # clip Muse có thể đổi định dạng điểm ảnh giữa chừng (xem ghep_anh_12s)
                        "-f", "concat", "-safe", "0",
                        "-i", list_file,
                        *_video_encode_args(try_gpu),
                        "-c:a", "aac", "-b:a", "192k",
                        "-movflags", "+faststart",
                        output_path
                    ],
                    output_path,
                    timeout=240,
                )
                if not ok_reenc:
                    log.error("concat_videos reencode failed: %s", err2)
                    ok = False
                else:
                    ok = True
            else:
                ok = os.path.isfile(output_path) and os.path.getsize(output_path) > 1000

            if ok:
                log.info("✓ [FFmpeg] Đã ghép 16s thành công: %s (%.2fs)", os.path.basename(output_path), time.time() - t0)
            return ok
        except Exception as e:
            log.warning("Lỗi concat_videos: %s", e)
            ok = False
            return False
        finally:
            _safe_remove(list_file)
            if not ok:
                _safe_remove(output_path)  # không để lại file video hỏng/dở dang


def build_video_prompts_16s(product_name: str, scene_choice: str = "🎲 Random", lang: str = "ph", review_style: str = "Unboxing") -> tuple[list[str], str]:
    """Prompt A + B (16s) + ràng buộc giữ nguyên sản phẩm theo ảnh tham chiếu (PRODUCT_FIDELITY) cho cả 2 đoạn."""
    prompts, label = _build_video_prompts_16s_base(product_name, scene_choice=scene_choice, lang=lang, review_style=review_style)
    short_name = clean_product_title(product_name)
    return [f"{_image_first(p, short_name)} {PRODUCT_FIDELITY}" for p in prompts], label


def _build_video_prompts_16s_base(product_name: str, scene_choice: str = "🎲 Random", lang: str = "ph", review_style: str = "Unboxing") -> tuple[list[str], str]:
    """Tạo 2 prompt Prompt A (0-8s) và Prompt B (8-16s) chuẩn TVC Review sản phẩm Shopee."""
    _tvc = _LANG_MAP.get(lang, _LANG_MAP["ph"])
    short_name = clean_product_title(product_name)
    scene_name, scene_desc = pick_scene(scene_choice, lang="vi" if lang == "vi" else "en")

    style_raw = str(review_style or "").strip().lower()
    is_pov = any(k in style_raw for k in ("pov", "góc nhìn thứ nhất"))
    is_unbox = any(k in style_raw for k in ("unbox", "đập hộp"))
    is_demo = any(k in style_raw for k in ("demo", "công dụng", "feature"))

    if is_pov or is_unbox:
        prompt_a = (
            f'Create segment 1 of a 2-part product review video (0-8 seconds) for "{short_name}". '
            f'First-person point of view (POV) looking down at a clean tabletop surface in {scene_desc}. '
            f'ABSOLUTELY NO human face, NO head visible. Only two clean natural hands opening a plain, unbranded brown cardboard box '
            f'(no printing, no logo, no text on the box), picking up and revealing the product, which is exactly the product from the reference image. '
            f'Camera executes dynamic push-in on the product for an impressive reveal moment. '
            f'Voiceover speaks in {_tvc["language_tvc"]} introducing key features without greeting; no text displayed. '
            f'CRITICAL HANDOFF: Segment ends with hands holding the product steady at center desk.'
        )
        prompt_b = (
            f'Create segment 2 of a 2-part product review video (8-16 seconds) for "{short_name}". '
            f'Continuation from segment 1. First-person POV looking down at the same clean desk in {scene_desc}. '
            f'ABSOLUTELY NO human face, NO head visible. Starting pose matches exactly: two hands holding the product. '
            f'The hands actively demonstrate how the product works, testing features and giving an enthusiastic thumbs-up next to the product. '
            f'Voiceover speaks in {_tvc["language_tvc"]} giving genuine recommendation and call to action; no text displayed.'
        )
        return [prompt_a, prompt_b], "POV / Unboxing (16s)"

    if is_demo:
        prompt_a = (
            f'Create segment 1 of a 2-part product commercial (0-8 seconds) for "{short_name}". '
            f'Extreme close-up macro shots in {scene_desc} highlighting texture, build quality, and opening features of the product. '
            f'ABSOLUTELY NO face visible. Clean hands interacting with the product. '
            f'Voiceover speaks in {_tvc["language_tvc"]} explaining top benefits directly; no text displayed.'
        )
        prompt_b = (
            f'Create segment 2 of a 2-part product commercial (8-16 seconds) for "{short_name}". '
            f'Continuation from segment 1. Demonstrating practical utility and real-world durability of "{short_name}". '
            f'Smooth camera push-in and rotation with professional studio lighting. '
            f'Voiceover speaks in {_tvc["language_tvc"]} giving strong call to action; no text displayed.'
        )
        return [prompt_a, prompt_b], "Demo Công Dụng (16s)"

    if lang == "my":
        mc_desc = "A handsome Malay male model, modest clean long-sleeve shirt, about 25 years old"
    else:
        mc_desc = f"A beautiful {_tvc['nationality_tvc']} woman, about 20 years old, wearing modest stylish clothing"

    prompt_a = (
        f'Create segment 1 of a 2-part product review video (0-8 seconds) for "{short_name}". '
        f'{mc_desc} in {scene_desc}, holding "{short_name}" at chest level and enthusiastically presenting it to the camera. '
        f'Camera executes dynamic push-in on reveal. '
        f'She speaks {_tvc["language_tvc"]} stating the product benefits right away; no text displayed. '
        f'CRITICAL HANDOFF: Segment ends with presenter holding product with both hands at chest level, facing camera smiling.'
    )
    prompt_b = (
        f'Create segment 2 of a 2-part product review video (8-16 seconds) for "{short_name}". '
        f'Continuation from segment 1. Same {mc_desc} holding "{short_name}" at chest level continuing seamlessly. '
        f'She demonstrates the product up close to the camera, nodding with genuine satisfaction, then smiles with a thumbs-up. '
        f'She speaks {_tvc["language_tvc"]} giving a warm recommendation and call to action; no text displayed.'
    )
    return [prompt_a, prompt_b], "Review MC (16s)"


# ==================== DATABASE SERVER API CALLS ====================

def call_seed_api(server_url: str, api_key: str, method: str, path: str, data: dict = None, timeout: int = 90):
    """Gọi API Server PostgreSQL trung tâm từ Novagate."""
    url = server_url.rstrip("/") + path
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    if method == "GET":
        req = urllib.request.Request(url, headers=headers)
    else:
        body = json.dumps(data or {}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx.SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_count(v) -> int:
    try:
        return int(re.sub(r'\D', '', str(v)) or "0")
    except Exception:
        return 0


def parse_price(v, default=0.0) -> float:
    try:
        return float(str(v).replace(",", "").strip() or default)
    except Exception:
        return default


def normalize_and_filter_products(
    raw: list[dict],
    min_item_id: int = 0,
    min_commission: float = 0.0,
    min_sold: int = 0,
    min_monthly_sold: int = 0,
    min_price: float = 0.0,
    max_price: float | None = None,
    return_rejected: bool = False,
) -> tuple:
    """Chuẩn hóa dữ liệu sản phẩm và lọc chặt theo thông số + Blacklist/Thương hiệu."""
    bl = get_blacklist()
    blacklisted_count = 0
    blacklisted_samples = []
    rejected_items: list[dict] = []

    products = []
    for item in raw:
        p = dict(item)
        try:
            iid = int(re.sub(r'\D', '', str(p.get("item_id", 0))))
        except Exception:
            iid = 0
        try:
            rc = float(p.get("commission_rate", 0) or 0)
            comm = rc * 100.0 if 0 < rc <= 1.0 else rc
            p["commission_rate"] = comm
        except Exception:
            comm = 0.0

        sold_val = parse_count(p.get("sold", 0))
        ms_raw = p.get("monthly_sold")
        ms_val = parse_count(ms_raw) if ms_raw is not None and str(ms_raw).strip() != "" else None
        if ms_val is not None:
            p["monthly_sold"] = ms_val
        price_val = parse_price(p.get("price", 0))

        # Lọc chặt chẽ ở client
        if (min_item_id > 0 and iid < min_item_id) or comm < min_commission \
                or sold_val < min_sold or price_val < min_price \
                or (max_price is not None and price_val > max_price):
            continue
        if min_monthly_sold > 0 and ms_raw is not None and (ms_val or 0) < min_monthly_sold:
            continue

        # Kiểm tra Blacklist & thương hiệu/bản quyền
        is_bl, reason = is_product_blacklisted(p, bl)
        if is_bl:
            blacklisted_count += 1
            rejected_items.append({"item_id": p.get("item_id"), "reason": reason})
            if len(blacklisted_samples) < 5:
                blacklisted_samples.append(f"SP {p.get('item_id')}: {reason}")
            continue

        # Chuẩn hóa link ảnh sản phẩm: đảm bảo luôn có cả image và image_url
        mkt_code = str(p.get("market") or "ph").strip().lower() or "ph"
        img_val = str(p.get("image_url") or p.get("image") or "").strip()
        if not img_val and p.get("images") and isinstance(p["images"], list) and len(p["images"]) > 0:
            img_val = str(p["images"][0]).strip()
        if img_val:
            if img_val.startswith("//"):
                img_val = "https:" + img_val
            elif not img_val.startswith(("http://", "https://", "data:")):
                img_val = f"https://cf.shopee.{mkt_code}/file/{img_val}"
        p["image"] = img_val
        p["image_url"] = img_val

        products.append(p)

    if blacklisted_count > 0:
        log.info("🚫 Blacklist đã loại bỏ %d sản phẩm vi phạm (%s)", blacklisted_count, "; ".join(blacklisted_samples))

    if return_rejected:
        return products, blacklisted_count, rejected_items
    return products, blacklisted_count


def mark_blacklisted_jobs_failed(
    server_url: str | None,
    api_key: str | None,
    item_ids: list,
) -> int:
    """Đánh dấu failed trên Server cho các SP vừa bị lọc Blacklist lúc claim để không còn kẹt ở trạng thái processing."""
    clean_ids = [str(i).strip() for i in (item_ids or []) if str(i).strip()]
    if not clean_ids:
        return 0
    import concurrent.futures
    done = 0

    def _one(iid: str) -> bool:
        return report_job_completion(server_url, api_key, iid, "failed", None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="bl_fail") as ex:
        for ok in ex.map(_one, clean_ids):
            if ok:
                done += 1
    if done:
        log.info("🗑️ [Blacklist DB] Đã đánh dấu bỏ qua (failed) %d/%d SP vi phạm trên Server", done, len(clean_ids))
    return done


def claim_jobs_from_server(
    server_url: str | None = None,
    api_key: str | None = None,
    client_id: str | None = None,
    market: str = "PH",
    limit: int = 2000,
    sort_by: str = "sold",
    min_item_id: int = 40000000000,
    min_commission: float = 1.0,
    min_sold: int = 0,
    min_monthly_sold: int = 0,
    min_price: float = 0.0,
    max_price: float = None,
    return_stats: bool = False,
    return_rejected: bool = False,
) -> list[dict] | tuple:
    """Nhận lô sản phẩm từ Server PostgreSQL trung tâm của Shopee và tự động lọc Blacklist."""
    server_url = server_url or _env_server_url()
    api_key = api_key or _env_api_key()
    client_id = client_id or _env_client_id()
    payload = {
        "market": market,
        "clientId": client_id,
        "limit": limit,
        "sortBy": sort_by,
        "tool": "novagate",
        "min_item_id": min_item_id,
        "minItemId": min_item_id,
        "min_commission": min_commission,
        "minCommission": min_commission,
        "min_sold": min_sold,
        "minSold": min_sold,
        "min_monthly_sold": min_monthly_sold,
        "minMonthlySold": min_monthly_sold,
        "min_price": min_price,
        "minPrice": min_price,
    }
    if max_price is not None:
        payload["max_price"] = max_price
        payload["maxPrice"] = max_price

    res = call_seed_api(server_url, api_key, "POST", "/api/thinaptm/claim-jobs", payload)
    raw = res.get("products", []) if isinstance(res, dict) else []

    products, blacklisted_count, rejected = normalize_and_filter_products(
        raw,
        min_item_id=min_item_id,
        min_commission=min_commission,
        min_sold=min_sold,
        min_monthly_sold=min_monthly_sold,
        min_price=min_price,
        max_price=max_price,
        return_rejected=True,
    )

    if return_rejected:
        return products, blacklisted_count, rejected
    if return_stats:
        return products, blacklisted_count
    return products


def release_stuck_jobs(server_url: str | None = None, api_key: str | None = None, client_id: str | None = None) -> int:
    """Giải phóng các sản phẩm đang kẹt ở trạng thái processing."""
    server_url = server_url or _env_server_url()
    api_key = api_key or _env_api_key()
    client_id = client_id or _env_client_id()
    res = call_seed_api(server_url, api_key, "POST", "/api/thinaptm/release-jobs", {"clientId": client_id})
    return res.get("released", 0) if isinstance(res, dict) else 0


# ==================== LỖI DO TK (KHÔNG PHẢI DO SẢN PHẨM) -> TRẢ SP VỀ HÀNG CHỜ ====================
# SP lỗi vì TK Muse hỏng / im lặng / proxy chết thì KHÔNG đánh `failed` (mất SP vĩnh viễn): trả về `pending` để máy khác (hoặc
# TK khác) làm lại. Lỗi do chính SP (Muse từ chối nội dung, chỉ trả chữ, không có ảnh) vẫn đánh `failed`.
ACCOUNT_FAULT_MARKERS = (
    "附加参考图失败", "Đính kèm ảnh tham chiếu thất bại", "no-file-input",   # TK không đính được ảnh (phiên hết hạn / Instagram)
    "Prompt đã gửi nhưng KHÔNG kèm ảnh tham chiếu",
    "等待生成超时", "Hết thời gian chờ tạo", "Hết thời gian chờ render",       # TK im lặng: nhận việc nhưng không trả video
    "找不到聊天输入框", "Không tìm thấy ô nhập chat", "Khởi tạo trang muse.ai",  # trang Muse không mở được (proxy / phiên)
    "Prompt chưa tới được máy ảo", "máy ảo Muse", "ngừng phản hồi quá",      # VM Muse không phản hồi / treo ngầm
    "Máy ảo Muse đọc sai đường dẫn ảnh",                                    # VM tìm ảnh ở /home/user/ thay vì /home/hatch/workspace/
    "Khu vực/Proxy không hỗ trợ", "403 Region Blocked", "区域不可用",         # Proxy bị Muse chặn vùng địa lý
    "Muse is disconnected", "Muse đã ngắt kết nối", "Phiên TK hết hạn", "Something went wrong",
    "ProxyError", "Không có tài khoản nào khả dụng", "tất cả tài khoản đang bận", "Đã hết thời gian chờ tác vụ",
    "Lỗi xác thực tài khoản", "MuseAuthError", "hết hạn ngạch", "额度不足",
    # Lỗi dịch vụ tạm thời từ chính máy chủ Muse.ai (502/quá tải/không khả dụng) -> trả SP về pending, KHÔNG đánh failed
    "Dịch vụ tạo video Muse.ai đang lỗi", "dịch vụ tạo video đang lỗi",
    "hệ thống tạo video đang", "hệ thống làm video đang lỗi", "hệ thống video đang lỗi", "hệ thống video báo lỗi",
    "công cụ tạo video đang không khả dụng", "bên tạo video đang không khả dụng",
    "lỗi hệ thống video", "lỗi hệ thống 502", "video service returned an error", "temporarily unavailable",
)
MAX_REQUEUE = 3                      # 1 SP bị trả về hàng chờ tối đa chừng này lần, quá thì đánh `failed`
REQUEUE_FILE = os.path.join(CFG.data_dir, "requeue_counts.json")
_requeue_lock = threading.Lock()
_requeue_counts: dict[str, int] | None = None


def is_account_fault(message: str) -> bool:
    msg = str(message or "")
    return any(m in msg for m in ACCOUNT_FAULT_MARKERS)


def _load_requeue() -> dict[str, int]:
    global _requeue_counts
    if _requeue_counts is None:
        try:
            with open(REQUEUE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            _requeue_counts = {str(k): int(v) for k, v in d.items()} if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            _requeue_counts = {}
    return _requeue_counts


def take_requeue_slot(item_id) -> int:
    """Xin 1 lượt trả SP về hàng chờ. Trả về số lượt đã dùng (1..MAX_REQUEUE) nếu còn được trả; 0 nếu đã hết lượt (đánh failed)."""
    key = str(item_id)
    with _requeue_lock:
        counts = _load_requeue()
        n = counts.get(key, 0)
        if n >= MAX_REQUEUE:
            return 0
        counts[key] = n + 1
        if len(counts) > 50000:                       # giữ file gọn: bỏ bớt các SP cũ nhất
            for k in list(counts)[:10000]:
                counts.pop(k, None)
        try:
            _atomic_write_json(REQUEUE_FILE, counts, compact=True)
        except Exception:  # noqa: BLE001
            pass
        return n + 1


def forget_requeue(item_id) -> None:
    """SP đã xong: bỏ đếm lượt trả về."""
    key = str(item_id)
    with _requeue_lock:
        counts = _load_requeue()
        if counts.pop(key, None) is not None:
            try:
                _atomic_write_json(REQUEUE_FILE, counts, compact=True)
            except Exception:  # noqa: BLE001
                pass


def report_job_completion(
    server_url: str | None = None,
    api_key: str | None = None,
    item_id: str = "",
    status: str = "completed",
    video_path: str = None
) -> bool:
    """Báo cáo trạng thái hoàn thành của sản phẩm về Server PostgreSQL."""
    server_url = server_url or _env_server_url()
    api_key = api_key or _env_api_key()
    payload = {"itemId": item_id, "status": status, "tool": "museai"}
    if video_path:
        payload["video_path"] = video_path
    try:
        call_seed_api(server_url, api_key, "POST", "/api/thinaptm/complete-job", payload)
        return True
    except Exception as e:
        log.warning("Báo cáo complete-job thất bại (item_id=%s, status=%s, server=%s): %s: %s",
                    item_id, status, server_url or "<chưa cấu hình>", type(e).__name__, e)
        return False


# ==================== TẢI ẢNH SẢN PHẨM (chống SSRF) ====================
MAX_IMAGE_BYTES = 15 * 1024 * 1024  # 15MB
_IMAGE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"}


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified or not ip.is_global
    )


def _validate_public_http_url(url: str) -> str:
    """Chỉ cho phép http/https tới host công khai. Phân giải DNS và từ chối IP private/loopback/link-local/
    reserved/multicast. Ném ValueError (thông điệp rõ ràng) nếu bị từ chối."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL ảnh trống")
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith(("http://", "https://", "data:")):
        url = f"https://cf.shopee.ph/file/{url}"
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as e:
        raise ValueError(f"URL ảnh không hợp lệ: {e}") from None
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"URL ảnh bị từ chối: chỉ cho phép http/https (nhận '{scheme or 'không có scheme'}')")
    host = parsed.hostname
    if not host:
        raise ValueError("URL ảnh bị từ chối: thiếu host")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise ValueError("URL ảnh bị từ chối: port không hợp lệ") from None
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as e:
        raise ValueError(f"URL ảnh bị từ chối: không phân giải được host '{host}' ({e})") from None
    if not infos:
        raise ValueError(f"URL ảnh bị từ chối: host '{host}' không có địa chỉ IP")
    for info in infos:
        ip_str = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise ValueError(f"URL ảnh bị từ chối: địa chỉ IP không hợp lệ '{ip_str}'") from None
        if not _is_public_ip(ip):
            raise ValueError(f"URL ảnh bị từ chối: host '{host}' trỏ tới địa chỉ nội bộ/không công khai ({ip})")
    return url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Kiểm tra lại từng URL redirect (tránh redirect sang 127.0.0.1 / mạng nội bộ / file://)."""
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# https_handler: thêm bộ CA certifi — máy có kho chứng chỉ Windows thiếu gốc vẫn tải được ảnh Shopee
_IMAGE_OPENER = urllib.request.build_opener(_SafeRedirectHandler(), ssl_ctx.https_handler())


def _read_capped(resp, cap: int = MAX_IMAGE_BYTES) -> bytes:
    """Đọc body theo từng khối, dừng & báo lỗi nếu vượt quá `cap` byte."""
    cl = (resp.headers.get("Content-Length") or "").strip()
    if cl.isdigit() and int(cl) > cap:
        raise ValueError(f"Ảnh quá lớn ({int(cl)} byte > giới hạn {cap} byte)")
    buf = bytearray()
    while True:
        chunk = resp.read(64 * 1024)
        if not chunk:
            break
        buf += chunk
        if len(buf) > cap:
            raise ValueError(f"Ảnh quá lớn (> giới hạn {cap} byte)")
    return bytes(buf)


def _candidate_shopee_cdn_urls(url: str) -> list[str]:
    """Chuẩn hóa URL ảnh Shopee (bỏ @resize / _tn để lấy ảnh gốc chất lượng cao) và tạo URL dự phòng giữa
    cf.shopee.<mkt>/file/<hash> và down-<mkt>.img.susercontent.com/file/<hash>."""
    u = url.strip()
    if "@resize" in u:
        u = u.split("@resize")[0]
    if u.endswith("_tn"):
        u = u[:-3]
    elif "_tn." in u:
        u = u.replace("_tn.", ".")
    cands = [u]
    m_cf = re.match(r"^https?://cf\.shopee\.([a-z.]+)/file/(.+)$", u, re.IGNORECASE)
    if m_cf:
        dom, fhash = m_cf.group(1).lower(), m_cf.group(2)
        mkt_short = {"vn": "vn", "ph": "ph", "co.id": "id", "com.my": "my", "co.th": "th", "sg": "sg", "tw": "tw"}.get(dom, "ph")
        alt = f"https://down-{mkt_short}.img.susercontent.com/file/{fhash}"
        if alt not in cands:
            cands.append(alt)
    m_sc = re.match(r"^https?://down-([a-z]+)\.img\.susercontent\.com/file/(.+)$", u, re.IGNORECASE)
    if m_sc:
        mkt_short, fhash = m_sc.group(1).lower(), m_sc.group(2)
        dom = {"vn": "vn", "ph": "ph", "id": "co.id", "my": "com.my", "th": "co.th", "sg": "sg", "tw": "tw"}.get(mkt_short, "ph")
        alt = f"https://cf.shopee.{dom}/file/{fhash}"
        if alt not in cands:
            cands.append(alt)
    return cands


def _fetch_image_bytes(image_url: str, timeout: int = 15, require_image_type: bool = True) -> tuple[bytes, str]:
    """Tải ảnh an toàn (tự động thử sang CDN dự phòng susercontent.com <-> cf.shopee nếu cần).
    Trả về (data, content_type). ValueError = bị từ chối (không nên thử lại)."""
    base_url = _validate_public_http_url(image_url)
    last_exc: Exception | None = None
    for cand in _candidate_shopee_cdn_urls(base_url):
        try:
            url = _validate_public_http_url(cand)
            req = urllib.request.Request(url, headers=_IMAGE_HEADERS)
            with _IMAGE_OPENER.open(req, timeout=timeout) as resp:
                raw_ct = resp.headers.get("Content-Type") or ""
                ctype = raw_ct.split(";", 1)[0].strip().lower()
                if require_image_type and ctype and not ctype.startswith("image/"):
                    raise ValueError(f"URL không trả về ảnh (Content-Type: {ctype})")
                data = _read_capped(resp, MAX_IMAGE_BYTES)
            return data, (ctype or "image/jpeg")
        except ValueError:
            raise
        except Exception as e:
            last_exc = e
    raise last_exc or RuntimeError("Không tải được ảnh từ Shopee CDN")



def download_image_as_data_url(image_url: str, retries: int = 3, cache_path: str | None = None) -> str | None:
    """Tải ảnh sản phẩm từ Shopee CDN về bộ nhớ và trả về chuỗi Data URL Base64.
    Nếu truyền cache_path hợp lệ, lưu đệm file ảnh lên đĩa (và dùng lại ngay nếu đã có sẵn) để bước ghép Outro
    12s dùng lại tức thì mà không phải tải lại hoặc truyền lại Base64.
    Ném ValueError nếu URL bị từ chối (scheme/IP nội bộ/không phải ảnh/quá 15MB); lỗi mạng -> thử lại rồi trả None."""
    if not image_url:
        return None
    _validate_public_http_url(image_url)
    if cache_path and os.path.isfile(cache_path) and os.path.getsize(cache_path) > 100:
        try:
            with open(cache_path, "rb") as f:
                cached_bytes = f.read()
            if cached_bytes:
                ctype = "image/png" if cached_bytes[:8] == b"\x89PNG\r\n\x1a\n" else (
                    "image/webp" if cached_bytes[:4] == b"RIFF" and cached_bytes[8:12] == b"WEBP" else "image/jpeg"
                )
                return f"data:{ctype};base64,{base64.b64encode(cached_bytes).decode('utf-8')}"
        except OSError:
            pass
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            data, content_type = _fetch_image_bytes(image_url, timeout=15, require_image_type=True)
            if len(data) > 0:
                if cache_path:
                    try:
                        os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
                        tmp_img = f"{cache_path}.{os.getpid()}_{threading.get_ident()}.tmp"
                        with open(tmp_img, "wb") as f:
                            f.write(data)
                        os.replace(tmp_img, cache_path)
                    except OSError:
                        pass
                b64 = base64.b64encode(data).decode("utf-8")
                return f"data:{content_type};base64,{b64}"
        except ValueError:
            raise
        except Exception as e:
            last_err = e
            log.debug("Tải ảnh thất bại (lần %d/%d) %s: %s", attempt + 1, retries, image_url, e)
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    # Ghi rõ lý do (trước đây chỉ ở mức debug -> máy tải ảnh hỏng hàng loạt mà log không có gì để tra)
    log.warning("⚠️ Không tải được ảnh SP sau %d lần: %s -> %s", retries, image_url, last_err)
    return None


def download_image_to_file(image_url: str, target_path: str, retries: int = 3) -> bool:
    """Tải ảnh sản phẩm từ Shopee CDN và lưu trực tiếp ra file trên ổ đĩa.
    Ném ValueError nếu URL bị từ chối (scheme/IP nội bộ/quá 15MB); lỗi mạng -> thử lại rồi trả False."""
    if not image_url or not target_path:
        return False
    _validate_public_http_url(image_url)
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            data, _ctype = _fetch_image_bytes(image_url, timeout=15, require_image_type=False)
            if len(data) > 0:
                with open(target_path, "wb") as f:
                    f.write(data)
                return True
        except ValueError:
            raise
        except Exception as e:
            last_err = e
            log.debug("Tải ảnh ra file thất bại (lần %d/%d) %s: %s", attempt + 1, retries, image_url, e)
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
    log.warning("⚠️ Không tải được ảnh SP ra file sau %d lần: %s -> %s", retries, image_url, last_err)
    return False



def probe_video_meta(vp: str) -> tuple[float | None, bool, int, int, float, bool]:
    """Đo toàn bộ thông số cần thiết (thời lượng, có audio, width, height, fps) trong MỘT lần gọi ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
        "-of", "json", vp,
    ]
    dur: float | None = None
    has_audio = False
    width, height, fps = 720, 1280, 24.0
    has_dims = False
    try:
        result = _run_ffmpeg_cmd(cmd, timeout=10, text=True)
        data = json.loads(result.stdout or "{}")
        fmt_dur = (data.get("format") or {}).get("duration")
        if fmt_dur is not None:
            try:
                d_val = float(fmt_dur)
                if d_val > 0:
                    dur = d_val
            except (TypeError, ValueError):
                pass
        for s in (data.get("streams") or []):
            ctype = s.get("codec_type")
            if ctype == "audio":
                has_audio = True
            elif ctype == "video" and not has_dims:
                try:
                    w = int(s.get("width") or 0)
                    h = int(s.get("height") or 0)
                    if w > 0 and h > 0:
                        width, height = w, h
                        has_dims = True
                    fps_str = str(s.get("r_frame_rate") or "")
                    if "/" in fps_str:
                        num, den = fps_str.split("/", 1)
                        if float(den) > 0:
                            fps = float(num) / float(den)
                    elif fps_str:
                        fps = float(fps_str)
                except Exception:
                    pass
    except Exception:
        pass
    return dur, has_audio, width, height, fps, has_dims


def probe_media_duration(vp: str) -> float | None:
    """Đo thời lượng video bằng ffprobe. Trả về None nếu không đo được (thiếu ffprobe, file hỏng...)."""
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', vp]
    try:
        result = _run_ffmpeg_cmd(cmd, timeout=10, text=True)
        dur = float(result.stdout.strip())
        return dur if dur > 0 else None
    except Exception:
        return None


def get_media_duration(vp: str) -> float:
    """Như probe_media_duration nhưng mặc định 8.0s khi không đo được (giữ hành vi cũ cho các chỗ gọi khác)."""
    dur = probe_media_duration(vp)
    return dur if dur is not None else 8.0


OUTRO_TARGET_SECONDS = 12.0
OUTRO_TOLERANCE_SECONDS = 0.5


def is_valid_12s_output(vp: str) -> tuple[bool, float | None]:
    """Kiểm tra video sau khi ghép Outro thực sự dài ~12s. Trả về (hợp_lệ, thời_lượng_đo_được)."""
    dur = probe_media_duration(vp)
    if dur is None:
        return False, None
    return abs(dur - OUTRO_TARGET_SECONDS) <= OUTRO_TOLERANCE_SECONDS, dur


def ghep_anh_12s(
    video_path: str,
    image_path: str,
    output_path: str,
    ai_duration: float | None = None,
    total_duration: float = 12.0,
    fallback_duration: float | None = None
) -> bool:
    """Ghép ảnh sản phẩm làm Outro vào video AI để tạo thành video chuẩn 12.0s:
    - Nếu video AI 8s: giữ nguyên tốc độ chuẩn 8s (không làm chậm), tạo Outro ảnh phóng to (zoom-in) 4s -> Tổng 12.0s.
    - Nếu video AI 10s: giữ nguyên tốc độ chuẩn 10s, tạo Outro ảnh phóng to (zoom-in) 2s -> Tổng 12.0s.
    - Xử lý làm mượt âm thanh (fade out tự nhiên cuối đoạn AI) để nối sang Outro không bị giật/nổ tiếng.
    - Bước encode FFmpeg được giới hạn đồng thời qua FFMPEG_SEMAPHORE (1 lần ffprobe chạy ngoài semaphore),
      giới hạn -threads 2 / -filter_threads 2 để tránh bùng nổ thread và ưu tiên tiến trình thấp.
    - Thất bại thì xóa file output dở dang.
    """
    if not os.path.isfile(video_path) or not os.path.isfile(image_path):
        return False

    t0 = time.time()

    # --- Chuẩn bị (1 lần ffprobe gộp + dựng filter) — KHÔNG giữ semaphore ---
    try:
        probed_dur, has_audio, width, height, fps, has_dims = probe_video_meta(video_path)
        fps_int = int(round(fps))
        if fps_int <= 0:
            fps_int = 24

        # Xác định độ dài video AI (8s hoặc 10s): ưu tiên giá trị truyền trực tiếp, rồi thời lượng thật vừa đo
        if ai_duration is not None and ai_duration > 0:
            chosen_ai_dur = float(ai_duration)
        elif probed_dur is not None and probed_dur > 0:
            chosen_ai_dur = float(probed_dur)
        elif fallback_duration is not None and fallback_duration > 0:
            chosen_ai_dur = float(fallback_duration)
        else:
            chosen_ai_dur = 8.0

        if chosen_ai_dur >= 9.0:
            video_dur = 10.0
            image_dur = 2.0
        else:
            video_dur = 8.0
            image_dur = 4.0

        total_image_frames = int(round(image_dur * fps_int))

        # Hiệu ứng phóng to mượt mà (Zoom-In) 1.0x -> 1.3x
        w_scale = int(width * 1.3)
        if w_scale % 2 != 0: w_scale += 1
        h_scale = int(height * 1.3)
        if h_scale % 2 != 0: h_scale += 1

        zoom_step = 0.3 / max(1, total_image_frames)
        zoom_expr = f"min(zoom+{zoom_step:.6f},1.3)"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"

        # Video AI nguyên bản, không làm chậm, cắt chuẩn video_dur.
        # Bỏ scale thừa trên [0:v] khi kích thước video nguồn đã đúng width x height.
        # setsar=1 (cả 2 đoạn): một số clip Muse có tỉ lệ điểm ảnh lạ (SAR 16:9, 1124:1125...) -> ép về 1:1.
        scale_v0 = "" if has_dims else f"scale={width}:{height}:flags=fast_bilinear,"
        v_part = f"[0:v]{scale_v0}fps={fps_int},tpad=stop_mode=clone:stop_duration={video_dur},trim=0:{video_dur},setsar=1,setpts=PTS-STARTPTS[v_part]"

        # Outro ảnh chuyển động phóng to (zoom-in) — chỉ đọc 1 khung ảnh rồi zoompan sinh ra đúng total_image_frames
        i_v = (
            f"[1:v]scale={w_scale}:{h_scale}:force_original_aspect_ratio=increase:flags=fast_bilinear,"
            f"crop={w_scale}:{h_scale},"
            f"zoompan=z='{zoom_expr}':d={total_image_frames}:x='{x_expr}':y='{y_expr}':s={width}x{height}:fps={fps_int},"
            f"trim=0:{image_dur},setsar=1,setpts=PTS-STARTPTS[i_v]"
        )

        filter_parts = [v_part, i_v]
        if has_audio:
            fade_dur = 0.6
            fade_start = max(0.0, video_dur - fade_dur)
            # Làm mượt âm thanh: chuẩn hóa stereo 48kHz, pad nếu thiếu, fade-out nhẹ 0.6s cuối video AI
            a_part = (
                f"[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                f"apad=whole_dur={video_dur},atrim=0:{video_dur},"
                f"afade=t=out:st={fade_start:.2f}:d={fade_dur:.2f},asetpts=PTS-STARTPTS[a_part]"
            )
            i_a = f"anullsrc=r=48000:cl=stereo,atrim=0:{image_dur},asetpts=PTS-STARTPTS[i_a]"
            concat = "[v_part][a_part][i_v][i_a]concat=n=2:v=1:a=1[outv][outa]"
            filter_parts.extend([a_part, i_a, concat])
            map_args = ["-map", "[outv]", "-map", "[outa]"]
        else:
            concat = "[v_part][i_v]concat=n=2:v=1:a=0[outv]"
            filter_parts.append(concat)
            map_args = ["-map", "[outv]"]

        filter_complex_str = "; ".join(filter_parts)
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        # -reinit_filter 0: một số clip Muse đổi định dạng điểm ảnh giữa chừng (vd 36 khung đầu yuv444p rồi yuv420p)
        # -> FFmpeg khởi tạo lại chuỗi bộ lọc, mốc thời gian quay về 0 và vứt ~35 khung -> video ra 10.54s thay vì 12s.
        base_cmd = [
            "ffmpeg", "-y",
            *_ffmpeg_global_thread_args(),
            "-reinit_filter", "0", "-i", video_path,
            "-i", image_path,
            "-filter_complex", filter_complex_str,
            *map_args,
        ]
    except Exception as e:
        log.warning("Lỗi chuẩn bị ghép video 12s: %s", e)
        return False

    # --- Encode (nặng) — giới hạn đồng thời qua FFMPEG_SEMAPHORE, GPU NVENC (giới hạn slot) hoặc CPU libx264 ---
    ok = False
    with FFMPEG_SEMAPHORE:
        try:
            def _build(try_gpu: bool) -> list[str]:
                cmd = list(base_cmd) + _video_encode_args(try_gpu)
                if has_audio:
                    cmd.extend(["-c:a", "aac", "-b:a", "192k"])
                cmd.extend(["-movflags", "+faststart", output_path])
                return cmd

            ok, last_err = _run_encode_with_gpu_fallback(_build, output_path, timeout=90)
            if not ok:
                log.warning("ghep_anh_12s failed: %s", last_err)
                return False
            log.info("✓ [FFmpeg] Đã ghép Outro 12s vào %s thành công (%.2fs)", os.path.basename(output_path), time.time() - t0)
            return True
        except Exception as e:
            log.warning("Lỗi ghép video 12s bằng ffmpeg: %s", e)
            return False
        finally:
            if not ok:
                _safe_remove(output_path)  # không để lại file video hỏng/dở dang
