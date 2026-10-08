"""
Tool Ghép Video 12s Shopee - MuseAI Studio
Tác giả: MuseAI Video Studio Pro
Chức năng:
  1. Quét toàn bộ video .mp4 trong thư mục input để trích xuất ItemID Shopee từ tên file.
  2. Tự động tìm / tải ảnh sản phẩm Shopee tương ứng:
     - Ưu tiên ảnh có sẵn trong thư mục hoặc cache cục bộ.
     - Lấy trực tiếp từ cơ sở dữ liệu PostgreSQL (shopee_products) hoặc Node.js API.
     - Cào trực tiếp từ Shopee Web (Bypass anti-bot bằng Facebook UA) nếu chưa có link ảnh trong DB.
  3. Ghép video với ảnh sản phẩm tạo thành video chuẩn 12.0s:
     - Video 8s  + 4s Outro ảnh phóng to (Zoom-In 1.0x -> 1.3x) = 12.0s.
     - Video 10s + 2s Outro ảnh phóng to (Zoom-In 1.0x -> 1.3x) = 12.0s.
     - Làm mượt âm thanh với hiệu ứng fade-out nhẹ, chuẩn âm thanh stereo.
  4. Giao diện trực quan Tkinter hiện đại, đa luồng mượt mà, không đơ máy.
"""
from __future__ import annotations

import concurrent.futures
import html as html_mod
import json
import logging
import os
import queue
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime
import urllib.parse
import urllib.request

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Cấu hình logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tool_ghep12s")

# Thư mục gốc
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# Thử import shopee_engine để dùng hàm ghep_anh_12s chuẩn
try:
    import shopee_engine
    HAS_SHOPEE_ENGINE = True
except Exception as e:
    log.warning("Không thể import shopee_engine: %s. Sẽ dùng ffmpeg nội bộ.", e)
    HAS_SHOPEE_ENGINE = False

# Thử import psycopg2
try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

# Thử import curl_cffi
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

# ----------------- CONSTANTS & CONFIG -----------------
FACEBOOK_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"

IMAGE_REGEX = re.compile(
    r'https://down-([a-z]{2})\.img\.susercontent\.com/file/([a-zA-Z0-9_-]+)'
)
RE_OG_IMAGE = re.compile(
    r'<meta[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']*)["\']', re.IGNORECASE
)
RE_OG_IMAGE_ALT = re.compile(
    r'<meta[^>]*content=["\']([^"\']*)["\'][^>]*property=["\']og:image["\']', re.IGNORECASE
)

MARKET_DOMAINS = {
    "PH": "shopee.ph",
    "VN": "shopee.vn",
    "MY": "shopee.com.my",
    "TH": "shopee.co.th",
    "ID": "shopee.co.id",
    "SG": "shopee.sg",
    "TW": "shopee.tw"
}

MARKET_OPTIONS = [
    "PH - Philippines",
    "VN - Việt Nam",
    "TH - Thái Lan",
    "MY - Malaysia",
    "ID - Indonesia",
    "SG - Singapore",
    "TW - Đài Loan"
]

DEFAULT_DB_CONFIG = {
    "host": "100.79.170.67",
    "port": "5432",
    "dbname": "shopee_db",
    "user": "postgres",
    "password": "postgres123",
    "api_url": "http://100.79.170.67:3000",
    "api_key": "shopee_secret_2026"
}

# Đọc cấu hình từ file .env nếu có
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")
if os.path.isfile(ENV_PATH):
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k == "SHOPEE_SERVER_URL" and v:
                        DEFAULT_DB_CONFIG["api_url"] = v
                    elif k == "SHOPEE_API_KEY" and v:
                        DEFAULT_DB_CONFIG["api_key"] = v
    except Exception:
        pass


# ----------------- HÀM TIỆN ÍCH HỆ THỐNG & FFMPEG -----------------
def _run_ffmpeg_cmd(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    """Chạy ffmpeg/ffprobe với độ ưu tiên thấp và ẩn cửa sổ CMD trên Windows."""
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "text": True
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000) |
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    return subprocess.run(cmd, **kwargs)


def get_video_duration(video_path: str) -> float:
    """Đo thời lượng video bằng ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path
    ]
    try:
        res = _run_ffmpeg_cmd(cmd, timeout=10)
        return float(res.stdout.strip())
    except Exception:
        return 8.0


def extract_item_id_from_filename(filename: str) -> str | None:
    """Trích xuất Shopee Item ID từ tên file MP4."""
    stem = os.path.splitext(os.path.basename(filename))[0].strip()
    if stem.isdigit() and len(stem) >= 6:
        return stem
    # Tìm chuỗi số dài từ 7 đến 16 chữ số
    matches = re.findall(r"\b\d{7,16}\b", stem)
    if matches:
        return max(matches, key=len)
    matches_all = re.findall(r"\d{7,16}", stem)
    if matches_all:
        return max(matches_all, key=len)
    return None


def internal_ghep_anh_12s(video_path: str, image_path: str, output_path: str) -> bool:
    """Ghép ảnh Outro vào video tạo thành video 12.0s chuẩn."""
    if HAS_SHOPEE_ENGINE:
        try:
            return shopee_engine.ghep_anh_12s(video_path, image_path, output_path)
        except Exception as e:
            log.warning("shopee_engine.ghep_anh_12s lỗi: %s. Chạy ffmpeg fallback.", e)

    # Fallback trực tiếp bằng FFmpeg nếu shopee_engine gặp lỗi
    try:
        dur = get_video_duration(video_path)
        if dur >= 9.0:
            video_dur = 10.0
            image_dur = 2.0
        else:
            video_dur = 8.0
            image_dur = 4.0

        # Lấy thông tin video
        probe_cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "json", video_path
        ]
        probe_res = _run_ffmpeg_cmd(probe_cmd, timeout=10)
        vdata = json.loads(probe_res.stdout)["streams"][0]
        width = int(vdata.get("width", 720))
        height = int(vdata.get("height", 1280))
        fps_str = vdata.get("r_frame_rate", "24/1")
        if "/" in fps_str:
            num, den = fps_str.split("/")
            fps = float(num) / float(den)
        else:
            fps = float(fps_str)
        fps_int = int(round(fps)) if round(fps) > 0 else 24

        # Kiểm tra audio
        audio_probe = [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path
        ]
        a_res = _run_ffmpeg_cmd(audio_probe, timeout=10)
        has_audio = len(a_res.stdout.strip()) > 0

        # Outro Zoom-In 1.0x -> 1.3x
        total_image_frames = int(round(image_dur * fps_int))
        w_scale = int(width * 1.3)
        if w_scale % 2 != 0: w_scale += 1
        h_scale = int(height * 1.3)
        if h_scale % 2 != 0: h_scale += 1

        zoom_step = 0.3 / max(1, total_image_frames)
        zoom_expr = f"min(zoom+{zoom_step:.6f},1.3)"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"

        v_part = f"[0:v]scale={width}:{height},fps={fps_int},tpad=stop_mode=clone:stop_duration={video_dur},trim=0:{video_dur},setpts=PTS-STARTPTS[v_part]"
        i_v = (
            f"[1:v]scale={w_scale}:{h_scale}:force_original_aspect_ratio=increase,"
            f"crop={w_scale}:{h_scale},"
            f"zoompan=z='{zoom_expr}':d={total_image_frames}:x='{x_expr}':y='{y_expr}':s={width}x{height},"
            f"fps={fps_int},trim=0:{image_dur},setpts=PTS-STARTPTS[i_v]"
        )

        filter_parts = [v_part, i_v]
        if has_audio:
            fade_dur = 0.6
            fade_start = max(0.0, video_dur - fade_dur)
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

        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-loop", "1", "-t", str(image_dur), "-i", image_path,
            "-filter_complex", filter_complex_str
        ]
        cmd.extend(map_args)
        cmd.extend([
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-threads", "2",
            "-pix_fmt", "yuv420p"
        ])
        if has_audio:
            cmd.extend(["-c:a", "aac", "-b:a", "192k"])
        cmd.append(output_path)

        res = _run_ffmpeg_cmd(cmd, timeout=120)
        return res.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 1000
    except Exception as e:
        log.error("Lỗi ffmpeg fallback: %s", e)
        return False


# ----------------- HÀM TẢI ẢNH & TRA CỨU SHOPEE -----------------
def download_image_direct(url: str, save_path: str) -> bool:
    """Tải ảnh từ Shopee CDN về máy tính."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://shopee.ph/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
        }
        req = urllib.request.Request(url, headers=headers)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
            data = resp.read()
            if len(data) > 1024:
                with open(save_path, "wb") as f:
                    f.write(data)
                return True
    except Exception as e:
        log.debug("download_image_direct failed for %s: %s", url, e)
    return False


def scrape_shopee_image_url(
    item_id: str,
    market: str = "PH",
    shop_id: str | None = None,
    product_url: str | None = None
) -> str | None:
    """Cào link ảnh sản phẩm từ Shopee bằng ItemID / ShopID / ProductURL."""
    domain = MARKET_DOMAINS.get(market.upper(), "shopee.ph")
    if product_url and str(product_url).startswith("http"):
        target_url = product_url
    elif shop_id and str(shop_id).strip() and str(shop_id) != "0":
        target_url = f"https://{domain}/product/{shop_id}/{item_id}"
    else:
        target_url = f"https://{domain}/product/0/{item_id}"

    html = ""
    try:
        if HAS_CURL_CFFI:
            resp = cffi_requests.get(
                target_url,
                impersonate="chrome124",
                headers={"User-Agent": FACEBOOK_UA},
                timeout=15,
                allow_redirects=True
            )
            html = resp.text
        else:
            req = urllib.request.Request(target_url, headers={"User-Agent": FACEBOOK_UA})
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log.debug("scrape_shopee_image_url failed for item %s: %s", item_id, e)

    # 1. Tìm qua og:image
    if html:
        m_img = RE_OG_IMAGE.search(html) or RE_OG_IMAGE_ALT.search(html)
        if m_img:
            img_url = html_mod.unescape(m_img.group(1)).strip()
            if "@resize" in img_url:
                img_url = img_url.split("@resize")[0]
            if "_tn" in img_url:
                img_url = img_url.replace("_tn", "")
            return img_url

        # 2. Tìm qua regex CDN
        matches = IMAGE_REGEX.findall(html)
        if matches:
            c_code, img_hash = matches[0]
            return f"https://down-{c_code}.img.susercontent.com/file/{img_hash}"

    # 3. Fallback: Nếu không cào được trang sản phẩm, thử cào trang tìm kiếm
    try:
        search_url = f"https://{domain}/search?keyword={item_id}"
        req2 = urllib.request.Request(search_url, headers={"User-Agent": FACEBOOK_UA})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req2, context=ctx, timeout=12) as resp:
            s_html = resp.read().decode("utf-8", errors="ignore")
            matches = IMAGE_REGEX.findall(s_html)
            if matches:
                c_code, img_hash = matches[0]
                return f"https://down-{c_code}.img.susercontent.com/file/{img_hash}"
    except Exception:
        pass

    return None


# ----------------- GIAO DIỆN CHÍNH TKINTER -----------------
class Ghep12sApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("🎬 Tool Ghép Video 12s Shopee - Tự Động Tải Ảnh Theo ItemID")
        self.root.geometry("980x760")
        self.root.minsize(860, 620)

        # Style & màu sắc giao diện Dark Theme
        self.setup_theme()

        # Biến trạng thái
        default_in = r"F:\Video AI\PH\MuseAI\8s 10s"
        if not os.path.exists(default_in):
            default_in = r"F:\Video AI\PH\MuseAI"
            if not os.path.exists(default_in):
                default_in = os.path.join(SCRIPT_DIR, "input_videos")

        default_out = r"F:\Video AI\PH\MuseAI\12s ghep"
        if not os.path.exists(os.path.dirname(default_out)):
            default_out = os.path.join(SCRIPT_DIR, "output_12s")

        self.input_folder_var = tk.StringVar(value=default_in)
        self.output_folder_var = tk.StringVar(value=default_out)
        self.market_var = tk.StringVar(value=MARKET_OPTIONS[0])
        self.threads_var = tk.IntVar(value=4)
        self.recursive_var = tk.BooleanVar(value=False)
        self.skip_existing_var = tk.BooleanVar(value=True)
        self.rename_item_id_var = tk.BooleanVar(value=True)
        self.del_raw_var = tk.BooleanVar(value=True)
        self.del_img_var = tk.BooleanVar(value=True)

        # DB Variables
        self.db_host_var = tk.StringVar(value=DEFAULT_DB_CONFIG["host"])
        self.db_port_var = tk.StringVar(value=DEFAULT_DB_CONFIG["port"])
        self.db_name_var = tk.StringVar(value=DEFAULT_DB_CONFIG["dbname"])
        self.db_user_var = tk.StringVar(value=DEFAULT_DB_CONFIG["user"])
        self.db_pass_var = tk.StringVar(value=DEFAULT_DB_CONFIG["password"])
        self.db_status_var = tk.StringVar(value="Chưa kiểm tra DB")
        self.db_connected = False

        # Thống kê
        self.stat_total = tk.StringVar(value="0")
        self.stat_images = tk.StringVar(value="0")
        self.stat_stitched = tk.StringVar(value="0")
        self.stat_skipped = tk.StringVar(value="0")
        self.status_text_var = tk.StringVar(value="Sẵn sàng ghép video 12s.")

        # Biến điều khiển luồng
        self.is_running = False
        self.stop_event = threading.Event()
        self.log_queue = queue.Queue()

        # Cache đường dẫn ảnh nội bộ
        self.image_cache_dir = os.path.join(SCRIPT_DIR, "data", "shopee_images_cache")
        os.makedirs(self.image_cache_dir, exist_ok=True)

        # Xây dựng giao diện
        self.create_widgets()

        # Khởi động bộ ghi log định kỳ
        self.root.after(100, self._process_log_queue)

        # Tự động test kết nối DB nhẹ nhàng ở chế độ nền
        self.root.after(500, self.test_db_connection_async)

    def setup_theme(self):
        """Thiết lập màu sắc và giao diện hiện đại."""
        self.bg_main = "#0f172a"      # Slate 900
        self.bg_card = "#1e293b"      # Slate 800
        self.bg_input = "#0b1329"     # Dark Navy
        self.fg_text = "#f8fafc"      # White slate
        self.fg_dim = "#94a3b8"       # Slate 400
        self.accent = "#3b82f6"       # Blue 500
        self.accent_hover = "#2563eb"
        self.success = "#10b981"      # Emerald 500
        self.danger = "#ef4444"       # Red 500
        self.border = "#334155"       # Slate 700

        self.root.configure(bg=self.bg_main)
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("TFrame", background=self.bg_main)
        style.configure("Card.TFrame", background=self.bg_card, relief="flat")
        style.configure("TLabel", background=self.bg_main, foreground=self.fg_text, font=("Segoe UI", 9))
        style.configure("Card.TLabel", background=self.bg_card, foreground=self.fg_text, font=("Segoe UI", 9))
        style.configure("CardTitle.TLabel", background=self.bg_card, foreground="#38bdf8", font=("Segoe UI", 10, "bold"))

        style.configure("TProgressbar", troughcolor=self.bg_input, background=self.accent, borderwidth=0)
        style.configure("TCombobox", fieldbackground=self.bg_input, background=self.bg_card, foreground=self.fg_text)

    def create_widgets(self):
        container = tk.Frame(self.root, bg=self.bg_main, padx=16, pady=12)
        container.pack(fill=tk.BOTH, expand=True)

        # ==================== HEADER ====================
        header = tk.Frame(container, bg=self.bg_main)
        header.pack(fill=tk.X, pady=(0, 10))

        title_box = tk.Frame(header, bg=self.bg_main)
        title_box.pack(side=tk.LEFT)

        tk.Label(
            title_box,
            text="🎬 TOOL GHÉP VIDEO 12S SHOPEE",
            font=("Segoe UI", 15, "bold"),
            bg=self.bg_main,
            fg="#38bdf8"
        ).pack(anchor=tk.W)

        tk.Label(
            title_box,
            text="Quét video MP4 ➔ Lấy ItemID ➔ Tự tải ảnh sản phẩm từ DB ➔ Ghép Outro 12.0s hoàn chỉnh",
            font=("Segoe UI", 9),
            bg=self.bg_main,
            fg=self.fg_dim
        ).pack(anchor=tk.W)

        # Nút trạng thái DB ở góc trên bên phải
        right_box = tk.Frame(header, bg=self.bg_main)
        right_box.pack(side=tk.RIGHT)

        self.lbl_db_badge = tk.Label(
            right_box,
            textvariable=self.db_status_var,
            font=("Segoe UI", 8, "bold"),
            bg="#334155",
            fg="#f8fafc",
            padx=8,
            pady=3,
            relief="flat"
        )
        self.lbl_db_badge.pack(side=tk.RIGHT, padx=(6, 0))

        btn_test_db = tk.Button(
            right_box,
            text="🔄 Kiểm tra DB",
            font=("Segoe UI", 8),
            bg=self.bg_card,
            fg=self.fg_text,
            relief="groove",
            cursor="hand2",
            command=self.test_db_connection_async
        )
        btn_test_db.pack(side=tk.RIGHT)

        # ==================== CARD 1: CẤU HÌNH THƯ MỤC ====================
        card_dir = tk.Frame(container, bg=self.bg_card, bd=1, relief="solid", highlightbackground=self.border, padx=12, pady=10)
        card_dir.pack(fill=tk.X, pady=(0, 8))

        # Thư mục Input
        r1 = tk.Frame(card_dir, bg=self.bg_card)
        r1.pack(fill=tk.X, pady=(0, 6))

        tk.Label(r1, text="📁 Thư mục Video gốc (Input):", font=("Segoe UI", 9, "bold"), bg=self.bg_card, fg=self.fg_text, width=26, anchor=tk.W).pack(side=tk.LEFT)
        e_in = tk.Entry(r1, textvariable=self.input_folder_var, font=("Segoe UI", 9), bg=self.bg_input, fg=self.fg_text, insertbackground=self.fg_text, relief="flat")
        e_in.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6), ipady=3)

        btn_br_in = tk.Button(r1, text="Chọn thư mục...", bg=self.accent, fg="white", font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2", padx=8, command=self.browse_input_folder)
        btn_br_in.pack(side=tk.RIGHT)

        # Thư mục Output
        r2 = tk.Frame(card_dir, bg=self.bg_card)
        r2.pack(fill=tk.X, pady=(0, 4))

        tk.Label(r2, text="💾 Thư mục xuất Video 12s:", font=("Segoe UI", 9, "bold"), bg=self.bg_card, fg=self.fg_text, width=26, anchor=tk.W).pack(side=tk.LEFT)
        e_out = tk.Entry(r2, textvariable=self.output_folder_var, font=("Segoe UI", 9), bg=self.bg_input, fg=self.fg_text, insertbackground=self.fg_text, relief="flat")
        e_out.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6), ipady=3)

        btn_br_out = tk.Button(r2, text="Chọn nơi lưu...", bg=self.accent, fg="white", font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2", padx=8, command=self.browse_output_folder)
        btn_br_out.pack(side=tk.RIGHT)

        # ==================== CARD 2: TÙY CHỌN & THÔNG SỐ ====================
        card_opt = tk.Frame(container, bg=self.bg_card, bd=1, relief="solid", highlightbackground=self.border, padx=12, pady=8)
        card_opt.pack(fill=tk.X, pady=(0, 8))

        row_opt = tk.Frame(card_opt, bg=self.bg_card)
        row_opt.pack(fill=tk.X)

        # Thị trường (Market)
        tk.Label(row_opt, text="🌐 Quốc gia:", font=("Segoe UI", 9, "bold"), bg=self.bg_card, fg=self.fg_text).pack(side=tk.LEFT, padx=(0, 4))
        self.market_combo = ttk.Combobox(row_opt, textvariable=self.market_var, values=MARKET_OPTIONS, state="readonly", width=18, font=("Segoe UI", 9))
        self.market_combo.pack(side=tk.LEFT, padx=(0, 16))

        # Số luồng (Threads)
        tk.Label(row_opt, text="⚡ Số luồng xử lý:", font=("Segoe UI", 9, "bold"), bg=self.bg_card, fg=self.fg_text).pack(side=tk.LEFT, padx=(0, 4))
        spin_threads = tk.Spinbox(row_opt, from_=1, to=8, width=4, textvariable=self.threads_var, font=("Segoe UI", 9, "bold"), bg=self.bg_input, fg=self.fg_text, justify="center", relief="flat")
        spin_threads.pack(side=tk.LEFT, padx=(0, 16))

        # Checkboxes
        chk_skip = tk.Checkbutton(row_opt, text="Bỏ qua file đã ghép 12s", variable=self.skip_existing_var, bg=self.bg_card, fg=self.fg_text, selectcolor=self.bg_input, activebackground=self.bg_card, activeforeground=self.fg_text, font=("Segoe UI", 8))
        chk_skip.pack(side=tk.LEFT, padx=(0, 12))

        chk_rec = tk.Checkbutton(row_opt, text="Quét thư mục con", variable=self.recursive_var, bg=self.bg_card, fg=self.fg_text, selectcolor=self.bg_input, activebackground=self.bg_card, activeforeground=self.fg_text, font=("Segoe UI", 8))
        chk_rec.pack(side=tk.LEFT)

        # Row 2 Options: Tùy chọn dọn dẹp sau khi ghép
        row_opt2 = tk.Frame(card_opt, bg=self.bg_card)
        row_opt2.pack(fill=tk.X, pady=(6, 0))

        chk_del_raw = tk.Checkbutton(
            row_opt2,
            text="🗑️ Xóa video gốc sau khi ghép xong 12s",
            variable=self.del_raw_var,
            bg=self.bg_card,
            fg="#fca5a5",
            selectcolor=self.bg_input,
            activebackground=self.bg_card,
            activeforeground="#fca5a5",
            font=("Segoe UI", 9, "bold")
        )
        chk_del_raw.pack(side=tk.LEFT, padx=(0, 20))

        chk_del_img = tk.Checkbutton(
            row_opt2,
            text="🗑️ Xóa ảnh sau khi ghép xong",
            variable=self.del_img_var,
            bg=self.bg_card,
            fg="#fde68a",
            selectcolor=self.bg_input,
            activebackground=self.bg_card,
            activeforeground="#fde68a",
            font=("Segoe UI", 9, "bold")
        )
        chk_del_img.pack(side=tk.LEFT)

        # ==================== CARD 3: ACTION CONTROLS & STATS ====================
        card_action = tk.Frame(container, bg=self.bg_main)
        card_action.pack(fill=tk.X, pady=(0, 8))

        # Nút Bắt đầu & Dừng
        act_btns = tk.Frame(card_action, bg=self.bg_main)
        act_btns.pack(side=tk.LEFT)

        self.btn_start = tk.Button(
            act_btns,
            text="🚀 BẮT ĐẦU GHÉP 12S",
            font=("Segoe UI", 11, "bold"),
            bg="#22c55e",
            fg="white",
            activebackground="#16a34a",
            activeforeground="white",
            relief="flat",
            cursor="hand2",
            padx=16,
            pady=6,
            command=self.start_process_async
        )
        self.btn_start.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_stop = tk.Button(
            act_btns,
            text="⏹ DỪNG LẠI",
            font=("Segoe UI", 10, "bold"),
            bg="#64748b",
            fg="white",
            activebackground="#475569",
            activeforeground="white",
            relief="flat",
            state=tk.DISABLED,
            cursor="hand2",
            padx=12,
            pady=6,
            command=self.stop_process
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 12))

        # Thẻ Thống Kê (Badges)
        stats_box = tk.Frame(card_action, bg=self.bg_main)
        stats_box.pack(side=tk.RIGHT)

        def make_stat_badge(parent, label, var, fg_color):
            f = tk.Frame(parent, bg=self.bg_card, padx=10, pady=4, relief="solid", bd=1, highlightbackground=self.border)
            f.pack(side=tk.LEFT, padx=3)
            tk.Label(f, text=label, font=("Segoe UI", 7), bg=self.bg_card, fg=self.fg_dim).pack()
            tk.Label(f, textvariable=var, font=("Segoe UI", 11, "bold"), bg=self.bg_card, fg=fg_color).pack()

        make_stat_badge(stats_box, "📁 TỔNG VIDEO", self.stat_total, "#f8fafc")
        make_stat_badge(stats_box, "🖼️ ĐÃ TẢI ẢNH", self.stat_images, "#38bdf8")
        make_stat_badge(stats_box, "🎬 ĐÃ GHÉP 12S", self.stat_stitched, "#4ade80")
        make_stat_badge(stats_box, "⚠️ BỎ QUA / LỖI", self.stat_skipped, "#f87171")

        # ==================== PROGRESS BAR & STATUS ====================
        prog_frame = tk.Frame(container, bg=self.bg_main)
        prog_frame.pack(fill=tk.X, pady=(0, 8))

        self.progress_bar = ttk.Progressbar(prog_frame, mode="determinate")
        self.progress_bar.pack(fill=tk.X, pady=(0, 3))

        lbl_status = tk.Label(prog_frame, textvariable=self.status_text_var, font=("Segoe UI", 9, "italic"), bg=self.bg_main, fg=self.fg_dim, anchor=tk.W)
        lbl_status.pack(fill=tk.X)

        # ==================== LOG CONSOLE ====================
        log_frame = tk.Frame(container, bg=self.bg_card, bd=1, relief="solid", highlightbackground=self.border)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        log_hdr = tk.Frame(log_frame, bg=self.bg_card, padx=8, pady=4)
        log_hdr.pack(fill=tk.X)

        tk.Label(log_hdr, text="📜 NHẬT KÝ HOẠT ĐỘNG (LOG)", font=("Segoe UI", 9, "bold"), bg=self.bg_card, fg="#38bdf8").pack(side=tk.LEFT)

        btn_clear_log = tk.Button(log_hdr, text="🧹 Xóa log", font=("Segoe UI", 8), bg=self.bg_input, fg=self.fg_dim, relief="flat", cursor="hand2", padx=6, command=self.clear_log)
        btn_clear_log.pack(side=tk.RIGHT)

        btn_open_out = tk.Button(log_hdr, text="📂 Mở thư mục 12s", font=("Segoe UI", 8, "bold"), bg=self.accent, fg="white", relief="flat", cursor="hand2", padx=8, command=self.open_output_folder)
        btn_open_out.pack(side=tk.RIGHT, padx=6)

        btn_open_in = tk.Button(log_hdr, text="📁 Mở thư mục gốc", font=("Segoe UI", 8), bg=self.bg_input, fg=self.fg_text, relief="flat", cursor="hand2", padx=6, command=self.open_input_folder)
        btn_open_in.pack(side=tk.RIGHT)

        # Scrolled Text
        text_scroll = tk.Scrollbar(log_frame)
        text_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.log_widget = tk.Text(
            log_frame,
            wrap=tk.WORD,
            font=("Consolas", 9),
            bg="#090d16",
            fg="#e2e8f0",
            insertbackground="white",
            relief="flat",
            yscrollcommand=text_scroll.set,
            padx=10,
            pady=8
        )
        self.log_widget.pack(fill=tk.BOTH, expand=True)
        text_scroll.config(command=self.log_widget.yview)

        # Định dạng màu cho log tags
        self.log_widget.tag_configure("info", foreground="#93c5fd")
        self.log_widget.tag_configure("success", foreground="#4ade80")
        self.log_widget.tag_configure("warn", foreground="#fbbf24")
        self.log_widget.tag_configure("error", foreground="#f87171")
        self.log_widget.tag_configure("header", foreground="#c084fc", font=("Consolas", 9, "bold"))

        self.log("🚀 Sẵn sàng khởi chạy Tool Ghép Video 12s Shopee!", "header")
        self.log("💡 Hướng dẫn: Chọn Thư mục Video gốc -> Bấm [BẮT ĐẦU GHÉP 12S] để chạy tự động.", "info")

    # ----------------- LOGGING SYSTEM -----------------
    def log(self, message: str, level: str = "info"):
        """Đưa thông điệp vào queue để cập nhật giao diện an toàn."""
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put((f"[{ts}] {message}", level))

    def _process_log_queue(self):
        """Timer callback đẩy nội dung từ queue ra Text widget."""
        try:
            count = 0
            while not self.log_queue.empty() and count < 40:
                msg, tag = self.log_queue.get_nowait()
                self.log_widget.insert(tk.END, msg + "\n", tag)
                count += 1
            if count > 0:
                self.log_widget.see(tk.END)
        except Exception:
            pass
        finally:
            self.root.after(100, self._process_log_queue)

    def clear_log(self):
        self.log_widget.delete("1.0", tk.END)

    # ----------------- UI ACTIONS -----------------
    def browse_input_folder(self):
        folder = filedialog.askdirectory(title="Chọn thư mục chứa video MP4 gốc")
        if folder:
            self.input_folder_var.set(folder)
            self.log(f"📁 Đã chọn thư mục Input: {folder}", "info")
            # Tự động gợi ý thư mục output
            suggested_out = os.path.join(os.path.dirname(folder), "12s ghep")
            self.output_folder_var.set(suggested_out)

    def browse_output_folder(self):
        folder = filedialog.askdirectory(title="Chọn thư mục xuất video 12s")
        if folder:
            self.output_folder_var.set(folder)
            self.log(f"💾 Đã chọn thư mục Output: {folder}", "info")

    def open_input_folder(self):
        f = self.input_folder_var.get().strip()
        if os.path.exists(f):
            os.startfile(f)

    def open_output_folder(self):
        f = self.output_folder_var.get().strip()
        os.makedirs(f, exist_ok=True)
        os.startfile(f)

    # ----------------- DATABASE CONNECTION -----------------
    def test_db_connection_async(self):
        threading.Thread(target=self._test_db_worker, daemon=True).start()

    def _test_db_worker(self):
        self.db_status_var.set("⏳ Đang kết nối DB...")
        self.lbl_db_badge.config(bg="#f59e0b", fg="#000000")

        # Thử kết nối PostgreSQL
        if HAS_PSYCOPG2:
            try:
                conn = psycopg2.connect(
                    host=self.db_host_var.get().strip() or "100.79.170.67",
                    port=int(self.db_port_var.get().strip() or "5432"),
                    dbname=self.db_name_var.get().strip() or "shopee_db",
                    user=self.db_user_var.get().strip() or "postgres",
                    password=self.db_pass_var.get().strip() or "postgres123",
                    connect_timeout=4
                )
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM shopee_products;")
                cnt = cur.fetchone()[0]
                cur.close()
                conn.close()

                self.db_connected = True
                self.db_status_var.set(f"🟢 DB Sẵn sàng ({cnt:,} SP)")
                self.lbl_db_badge.config(bg="#10b981", fg="#ffffff")
                self.log(f"✅ Đã kết nối PostgreSQL thành công! shopee_products có {cnt:,} sản phẩm.", "success")
                return
            except Exception as e:
                log.debug("PostgreSQL trực tiếp thất bại: %s", e)

        # Fallback thử API Server Port 3000
        api_url = DEFAULT_DB_CONFIG.get("api_url", "http://100.79.170.67:3000").rstrip("/")
        try:
            req = urllib.request.Request(
                f"{api_url}/api/products/export?limit=1",
                headers={"X-API-Key": DEFAULT_DB_CONFIG.get("api_key", "shopee_secret_2026")}
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("success"):
                    self.db_connected = True
                    self.db_status_var.set("🟢 API Server Sẵn sàng")
                    self.lbl_db_badge.config(bg="#10b981", fg="#ffffff")
                    self.log("✅ Đã kết nối thành công qua Node.js API Server (Cổng 3000).", "success")
                    return
        except Exception:
            pass

        self.db_connected = False
        self.db_status_var.set("🟡 Dùng Web (Bypass UA)")
        self.lbl_db_badge.config(bg="#334155", fg="#f8fafc")
        self.log("ℹ️ Database offline hoặc chưa kết nối. Tool sẽ tự động cào ảnh trực tiếp từ Shopee Web.", "info")

    def query_db_item_images(self, item_ids: list[str], market: str) -> dict[str, dict]:
        """Truy vấn hàng loạt ItemID từ Database để lấy link sản phẩm & link ảnh trực tiếp."""
        results: dict[str, dict] = {}
        if not item_ids:
            return results

        # 1. Thử trực tiếp PostgreSQL
        if HAS_PSYCOPG2:
            try:
                conn = psycopg2.connect(
                    host=self.db_host_var.get().strip() or "100.79.170.67",
                    port=int(self.db_port_var.get().strip() or "5432"),
                    dbname=self.db_name_var.get().strip() or "shopee_db",
                    user=self.db_user_var.get().strip() or "postgres",
                    password=self.db_pass_var.get().strip() or "postgres123",
                    connect_timeout=4
                )
                cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

                # Lấy danh sách ID số
                num_ids = [int(i) for i in item_ids if str(i).isdigit()]
                if num_ids:
                    query = """
                        SELECT item_id, image_url, product_url, shop_id, market, name 
                        FROM shopee_products 
                        WHERE item_id = ANY(%s);
                    """
                    cur.execute(query, (num_ids,))
                    rows = cur.fetchall()
                    for r in rows:
                        iid = str(r["item_id"])
                        raw_img = str(r["image_url"] or "").strip()
                        raw_url = str(r["product_url"] or "").strip()
                        shop_id = str(r["shop_id"] or "").strip()
                        mkt = (r["market"] or market).upper()

                        clean_img = ""
                        if raw_img:
                            if raw_img.startswith("//"):
                                clean_img = "https:" + raw_img
                            elif raw_img.startswith("http://") or raw_img.startswith("https://"):
                                clean_img = raw_img
                            else:
                                clean_img = f"https://cf.shopee.{mkt.lower()}/file/{raw_img}"

                            if "@resize" in clean_img:
                                clean_img = clean_img.split("@resize")[0]
                            if "_tn" in clean_img:
                                clean_img = clean_img.replace("_tn", "")

                        if not raw_url or not raw_url.startswith("http"):
                            dom = MARKET_DOMAINS.get(mkt, "shopee.ph")
                            if shop_id:
                                raw_url = f"https://{dom}/product/{shop_id}/{iid}"

                        results[iid] = {
                            "image_url": clean_img,
                            "product_url": raw_url,
                            "shop_id": shop_id,
                            "market": mkt,
                            "name": r["name"] or ""
                        }

                cur.close()
                conn.close()
                if results:
                    found_imgs = sum(1 for v in results.values() if v.get("image_url"))
                    self.log(f"🔎 Database khớp được {len(results)}/{len(item_ids)} SP (có sẵn ảnh: {found_imgs}/{len(results)}).", "success")
                return results
            except Exception as e:
                log.debug("Lỗi query PostgreSQL batch: %s", e)

        return results

    # ----------------- MAIN PROCESSING WORKER -----------------
    def start_process_async(self):
        in_folder = self.input_folder_var.get().strip()
        out_folder = self.output_folder_var.get().strip()

        if not os.path.exists(in_folder):
            messagebox.showerror("Lỗi", "Thư mục Video gốc không tồn tại!")
            return

        os.makedirs(out_folder, exist_ok=True)

        self.is_running = True
        self.stop_event.clear()
        self.btn_start.config(state=tk.DISABLED, bg="#64748b")
        self.btn_stop.config(state=tk.NORMAL, bg=self.danger)

        threading.Thread(target=self._process_worker, args=(in_folder, out_folder), daemon=True).start()

    def stop_process(self):
        if self.is_running:
            self.stop_event.set()
            self.log("⏹ Đang gửi tín hiệu dừng tới các luồng xử lý...", "warn")
            self.status_text_var.set("Đang dừng...")

    def _process_worker(self, in_folder: str, out_folder: str):
        try:
            self.status_text_var.set("⏳ Đang quét danh sách file MP4...")
            self.log(f"📂 Bắt đầu quét thư mục: {in_folder}", "header")

            # 1. Quét toàn bộ file .mp4
            mp4_files = []
            if self.recursive_var.get():
                for root_dir, _, files in os.walk(in_folder):
                    for f in files:
                        if f.lower().endswith(".mp4"):
                            mp4_files.append(os.path.join(root_dir, f))
            else:
                for f in os.listdir(in_folder):
                    if f.lower().endswith(".mp4"):
                        mp4_files.append(os.path.join(in_folder, f))

            total_files = len(mp4_files)
            self.stat_total.set(str(total_files))
            self.log(f"📁 Tìm thấy tổng cộng {total_files} file video .mp4.", "info")

            if total_files == 0:
                self.status_text_var.set("⚠️ Không tìm thấy file MP4 nào!")
                self.log("⚠️ Không có video .mp4 nào trong thư mục!", "warn")
                return

            # 2. Bóc tách ItemID từ tên file
            video_items = []
            extracted_ids_set = set()
            for v_path in mp4_files:
                fname = os.path.basename(v_path)
                item_id = extract_item_id_from_filename(fname)
                if item_id:
                    extracted_ids_set.add(item_id)
                video_items.append({
                    "video_path": v_path,
                    "filename": fname,
                    "item_id": item_id
                })

            self.log(f"🔍 Trích xuất được {len(extracted_ids_set)} ItemID hợp lệ từ {total_files} video.", "info")

            # 3. Lấy thông tin từ Database
            mkt_choice = self.market_var.get().split(" - ")[0].strip().upper()
            db_data = self.query_db_item_images(list(extracted_ids_set), mkt_choice)

            # Thư mục lưu ảnh cục bộ cho session này
            session_img_dir = os.path.join(out_folder, "_images")
            os.makedirs(session_img_dir, exist_ok=True)

            # Biến đếm tiến độ
            count_stitched = 0
            count_images = 0
            count_skipped = 0
            lock = threading.Lock()

            # 4. Pipeline xử lý 1 video
            def process_single_video(item_info: dict) -> bool:
                nonlocal count_stitched, count_images, count_skipped
                if self.stop_event.is_set():
                    return False

                v_path = item_info["video_path"]
                fname = item_info["filename"]
                item_id = item_info["item_id"]

                if not item_id:
                    self.log(f"⚠️ Bỏ qua '{fname}': Không nhận diện được ItemID trong tên file.", "warn")
                    with lock:
                        count_skipped += 1
                        self.stat_skipped.set(str(count_skipped))
                    return False

                # Kiểm tra file output đích
                stem = os.path.splitext(fname)[0]
                out_name = f"{item_id}.mp4" if self.rename_item_id_var.get() else f"{stem}.mp4"
                out_path = os.path.join(out_folder, out_name)

                # Kiểm tra nếu đã có video 12s hợp lệ
                if self.skip_existing_var.get() and os.path.isfile(out_path):
                    try:
                        ex_dur = get_video_duration(out_path)
                        if 11.5 <= ex_dur <= 12.5 and os.path.getsize(out_path) > 10000:
                            extra_del = ""
                            if self.del_raw_var.get() and os.path.abspath(v_path) != os.path.abspath(out_path):
                                try:
                                    if os.path.isfile(v_path):
                                        os.remove(v_path)
                                        extra_del = " [đã xóa video gốc thừa]"
                                except Exception:
                                    pass
                            self.log(f"⏭ Đã có sẵn 12.0s: {out_name} ({ex_dur:.1f}s){extra_del} -> Bỏ qua", "info")
                            with lock:
                                count_skipped += 1
                                self.stat_skipped.set(str(count_skipped))
                            return True
                    except Exception:
                        pass

                # --- BƯỚC A: TÌM / TẢI ẢNH SẢN PHẨM ---
                img_path = None
                # A.1: Tìm ảnh có sẵn trong máy
                possible_locals = [
                    os.path.join(in_folder, f"{item_id}.jpg"),
                    os.path.join(in_folder, f"{item_id}.png"),
                    os.path.join(in_folder, f"{item_id}.webp"),
                    os.path.join(session_img_dir, f"{item_id}.jpg"),
                    os.path.join(self.image_cache_dir, f"{item_id}.jpg"),
                ]
                for pl in possible_locals:
                    if os.path.isfile(pl) and os.path.getsize(pl) > 1024:
                        img_path = pl
                        break

                # A.2: Nếu chưa có ảnh trên máy, tải từ Database hoặc Shopee Web
                if not img_path:
                    target_save = os.path.join(session_img_dir, f"{item_id}.jpg")
                    db_item = db_data.get(item_id) or {}
                    db_img_url = db_item.get("image_url")
                    db_shop_id = db_item.get("shop_id")
                    db_prod_url = db_item.get("product_url")

                    # Ưu tiên 1: Tải trực tiếp bằng link ảnh đã có sẵn trong Database
                    if db_img_url:
                        if download_image_direct(db_img_url, target_save):
                            img_path = target_save
                            self.log(f"🖼️ Đã tải ảnh SP {item_id} từ Database!", "success")

                    # Ưu tiên 2: Nếu chưa có link ảnh trong DB hoặc tải lỗi -> cào từ Shopee Web
                    if not img_path:
                        scraped_img_url = scrape_shopee_image_url(
                            item_id,
                            market=mkt_choice,
                            shop_id=db_shop_id,
                            product_url=db_prod_url
                        )
                        if scraped_img_url:
                            if download_image_direct(scraped_img_url, target_save):
                                img_path = target_save
                                self.log(f"🖼️ Đã cào & tải ảnh SP {item_id} từ Shopee Web!", "success")

                    if img_path:
                        # Mirror sang cache dùng chung
                        try:
                            global_cache = os.path.join(self.image_cache_dir, f"{item_id}.jpg")
                            if not os.path.exists(global_cache):
                                with open(target_save, "rb") as f_in, open(global_cache, "wb") as f_out:
                                    f_out.write(f_in.read())
                        except Exception:
                            pass
                        with lock:
                            count_images += 1
                            self.stat_images.set(str(count_images))
                    else:
                        self.log(f"❌ Không tìm được link ảnh Shopee cho SP {item_id}", "error")

                if not img_path or not os.path.isfile(img_path):
                    self.log(f"❌ Bỏ qua {fname}: Thiếu ảnh sản phẩm để ghép Outro 12s.", "error")
                    with lock:
                        count_skipped += 1
                        self.stat_skipped.set(str(count_skipped))
                    return False

                # --- BƯỚC B: GHÉP 12S BẰNG FFMPEG ---
                raw_dur = get_video_duration(v_path)
                self.log(f"⚡ Đang ghép 12s: {fname} ({raw_dur:.1f}s) + Outro ảnh {item_id}...", "info")

                success = internal_ghep_anh_12s(v_path, img_path, out_path)
                if success and os.path.isfile(out_path):
                    final_dur = get_video_duration(out_path)
                    del_notes = []

                    # 1. Xóa video gốc sau khi ghép xong 12s
                    if self.del_raw_var.get() and os.path.abspath(v_path) != os.path.abspath(out_path):
                        try:
                            if os.path.isfile(v_path):
                                os.remove(v_path)
                                del_notes.append("đã xóa video gốc")
                        except Exception as e_vdel:
                            log.warning("Không thể xóa video gốc %s: %s", v_path, e_vdel)

                    # 2. Xóa ảnh sản phẩm sau khi ghép xong
                    if self.del_img_var.get():
                        try:
                            if img_path and os.path.isfile(img_path):
                                os.remove(img_path)
                                del_notes.append("đã xóa ảnh")
                        except Exception as e_idel:
                            log.warning("Không thể xóa ảnh %s: %s", img_path, e_idel)

                    del_str = f" [{', '.join(del_notes)}]" if del_notes else ""
                    self.log(f"🎉 Ghép thành công: {out_name} chuẩn {final_dur:.1f}s!{del_str}", "success")
                    with lock:
                        count_stitched += 1
                        self.stat_stitched.set(str(count_stitched))
                    return True
                else:
                    self.log(f"❌ Lỗi khi ghép FFmpeg cho file {fname}", "error")
                    with lock:
                        count_skipped += 1
                        self.stat_skipped.set(str(count_skipped))
                    return False

            # 5. Chạy đa luồng ThreadPoolExecutor
            num_workers = max(1, min(8, self.threads_var.get()))
            self.log(f"🚀 Bắt đầu xử lý song song với {num_workers} luồng...", "header")

            done_count = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
                future_map = {executor.submit(process_single_video, it): it for it in video_items}
                for future in concurrent.futures.as_completed(future_map):
                    if self.stop_event.is_set():
                        break
                    done_count += 1
                    pct = (done_count / total_files) * 100
                    self.progress_bar["value"] = pct
                    self.status_text_var.set(f"Đang xử lý: {done_count}/{total_files} ({pct:.1f}%)")

            # Dọn dẹp thư mục _images tạm nếu đã xóa hết ảnh bên trong
            if self.del_img_var.get() and os.path.isdir(session_img_dir):
                try:
                    if not os.listdir(session_img_dir):
                        os.rmdir(session_img_dir)
                except Exception:
                    pass

            if self.stop_event.is_set():
                self.log("⏹ ĐÃ DỪNG TIẾN TRÌNH THEO YÊU CẦU.", "warn")
                self.status_text_var.set("Đã dừng tiến trình.")
            else:
                self.progress_bar["value"] = 100
                self.log(f"🎉🎉 HOÀN THÀNH TOÀN BỘ! Đã tạo thành công {count_stitched} video chuẩn 12.0s.", "header")
                self.status_text_var.set(f"Hoàn thành! Đã ghép {count_stitched}/{total_files} video 12s.")

        except Exception as e:
            self.log(f"❌ CÓ LỖI XẢY RA: {e}", "error")
            self.status_text_var.set("Có lỗi xảy ra.")
        finally:
            self.is_running = False
            self.root.after(0, self._reset_ui_state)

    def _reset_ui_state(self):
        self.btn_start.config(state=tk.NORMAL, bg="#22c55e")
        self.btn_stop.config(state=tk.DISABLED, bg="#64748b")


# ----------------- ENTRY POINT -----------------
def main():
    root = tk.Tk()
    app = Ghep12sApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
