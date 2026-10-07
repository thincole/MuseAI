"""Shopee Engine cho MuseAI — Sao chép toàn bộ logic lấy sản phẩm từ Database PostgreSQL
và sinh prompt chuẩn TVC / Video quảng cáo từ Novagate sang MuseAI.
"""
from __future__ import annotations

import base64
import collections
import json
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

from config import CFG

log = logging.getLogger("shopee_engine")

# Concurrency Limiter: Chỉ cho phép tối đa 1 tiến trình FFmpeg ghép/render video chạy tại 1 thời điểm.
# Các luồng worker khác sẽ xếp hàng tuần tự. Vì mỗi tác vụ ghép chỉ mất 0.3s - 1.5s,
# việc xếp hàng này giúp tránh nghẽn I/O ổ đĩa, giải phóng 100% tình trạng quá tải CPU và loại bỏ hoàn toàn hiện tượng đơ máy.
FFMPEG_LOCK = threading.Semaphore(1)


def _run_ffmpeg_cmd(cmd: list[str], timeout: int = 180, check: bool = False, text: bool = False) -> subprocess.CompletedProcess:
    """Chạy FFmpeg / FFprobe với độ ưu tiên tiến trình thấp (BELOW_NORMAL_PRIORITY_CLASS trên Windows)
    và ẩn hoàn toàn cửa sổ đen con (CREATE_NO_WINDOW).
    Đảm bảo CPU luôn ưu tiên giao diện Windows, chuột, bàn phím và hệ điều hành, máy tính không bao giờ bị đơ/lag."""
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
    return subprocess.run(cmd, **kwargs)

# ==================== CẤU HÌNH DATABASE NOVAGATE (đọc từ .env, không ghi cứng secret) ====================
# Server URL / API key / Client ID để trong .env (file này đã được .gitignore) để không lộ khi commit.
# Để trống nếu chưa cấu hình: /api/shopee/claim sẽ báo lỗi rõ ràng thay vì dùng key bí mật ghi cứng.
# config._load_dotenv() đã chạy khi import config ở trên nên os.environ đã có giá trị từ .env.
DEFAULT_SERVER_URL = os.environ.get("SHOPEE_SERVER_URL", "").strip()
DEFAULT_API_KEY = os.environ.get("SHOPEE_API_KEY", "").strip()
DEFAULT_CLIENT_ID = os.environ.get("SHOPEE_CLIENT_ID", "").strip()
DEFAULT_OUT_DIR = os.environ.get("SHOPEE_OUT_DIR", "").strip() or os.path.join(CFG.base_dir, "output")

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
    3. Trả về tối đa 4-5 từ chữ sạch tinh gọn.
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
    clean_str = " ".join(words[:4]).strip()
    return clean_str if clean_str else "featured item"


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
            f'Only two clean, natural hands are visible carefully opening packaging, unboxing, and presenting the product. '
            f'The hands smoothly showcase the product details, packaging, and craftsmanship. '
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


def get_cached_clips(item_id: str) -> dict:
    """Lấy danh sách các clip thành phần (Clip A, Clip B) đã render thành công trước đó."""
    if not os.path.isfile(CLIP_CACHE_FILE):
        return {}
    try:
        with open(CLIP_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        item_data = data.get(str(item_id)) or {}
        valid = {}
        for k in ("clip_a", "clip_b"):
            fname = item_data.get(k)
            if fname:
                fpath = fname if os.path.isabs(fname) else os.path.join(CFG.media_dir, fname)
                if os.path.isfile(fpath) and os.path.getsize(fpath) > 10000:
                    valid[k] = fname
        return valid
    except Exception:
        return {}


def set_cached_clip(item_id: str, clip_key: str, filename: str):
    """Lưu lại tên file clip thành phần đã render thành công của sản phẩm."""
    os.makedirs(os.path.dirname(CLIP_CACHE_FILE), exist_ok=True)
    data = {}
    if os.path.isfile(CLIP_CACHE_FILE):
        try:
            with open(CLIP_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    str_id = str(item_id)
    if str_id not in data:
        data[str_id] = {}
    data[str_id][clip_key] = filename
    data[str_id]["updated_at"] = int(time.time())
    try:
        with open(CLIP_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

BLACKLIST_FILE = os.path.join(CFG.data_dir, "shopee_blacklist.json")

DEFAULT_BLACKLIST = {
    "enabled": True,
    "auto_block_safety": True,
    "keywords": [
        "vape", "pod", "thuốc lá", "thuoc la", "cigar", "xì gà", "xi ga",
        "bao cao su", "kích dục", "kich duc", "sextoy", "sex toy", "tình dục", "tinh duc",
        "18+", "người lớn", "nguoi lon", "dao găm", "súng", "sung", "vũ khí", "vu khi",
        "fake", "replica", "hàng nhái", "hang nhai", "shisha", "bóng cười", "bong cuoi"
    ],
    "item_ids": []
}


def get_blacklist() -> dict:
    """Lấy danh sách Blacklist (từ khóa cấm & Item ID cấm)."""
    if not os.path.isfile(BLACKLIST_FILE):
        return dict(DEFAULT_BLACKLIST)
    try:
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "keywords" not in data:
                data["keywords"] = list(DEFAULT_BLACKLIST["keywords"])
            if "item_ids" not in data:
                data["item_ids"] = []
            if "enabled" not in data:
                data["enabled"] = True
            if "auto_block_safety" not in data:
                data["auto_block_safety"] = True
            return data
    except Exception:
        return dict(DEFAULT_BLACKLIST)


def save_blacklist(data: dict) -> bool:
    """Lưu danh sách Blacklist xuống file data/shopee_blacklist.json."""
    os.makedirs(os.path.dirname(BLACKLIST_FILE), exist_ok=True)
    try:
        kw_list = []
        for kw in data.get("keywords", []):
            k = str(kw).strip().lower()
            if k and k not in kw_list:
                kw_list.append(k)

        ids_list = []
        for iid in data.get("item_ids", []):
            i = str(iid).strip()
            if i and i not in ids_list:
                ids_list.append(i)

        clean_data = {
            "enabled": bool(data.get("enabled", True)),
            "auto_block_safety": bool(data.get("auto_block_safety", True)),
            "keywords": kw_list,
            "item_ids": ids_list,
            "updated_at": int(time.time())
        }
        with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(clean_data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log.warning("Lỗi lưu blacklist: %s", e)
        return False


def add_to_blacklist(item_id: str | None = None, keyword: str | None = None, reason: str = "") -> dict:
    """Thêm một Item ID hoặc Từ khóa vào Blacklist."""
    bl = get_blacklist()
    added = []
    if item_id:
        sid = str(item_id).strip()
        if sid and sid not in bl.get("item_ids", []):
            bl.setdefault("item_ids", []).append(sid)
            added.append(f"ID {sid}")
    if keyword:
        skw = str(keyword).strip().lower()
        if skw and skw not in bl.get("keywords", []):
            bl.setdefault("keywords", []).append(skw)
            added.append(f"Từ khóa '{skw}'")
    if added:
        save_blacklist(bl)
        log.info("🚫 Đã thêm vào Blacklist (%s): %s", reason or "manual", ", ".join(added))
    return bl


def is_product_blacklisted(product: dict, blacklist_data: dict | None = None) -> tuple[bool, str]:
    """Kiểm tra sản phẩm có vi phạm Blacklist không. Trả về (is_blacklisted, lý do)."""
    bl = blacklist_data if blacklist_data is not None else get_blacklist()
    if not bl.get("enabled", True):
        return False, ""

    # 1. Kiểm tra Item ID
    iid_str = str(product.get("item_id", "")).strip()
    blacklisted_ids = {str(x).strip() for x in bl.get("item_ids", []) if str(x).strip()}
    if iid_str and iid_str in blacklisted_ids:
        return True, f"ID {iid_str} nằm trong danh sách đen (Blacklist)"

    # 2. Kiểm tra Từ khóa cấm trong tên sản phẩm
    name = (product.get("name") or "").lower()
    if not name:
        return False, ""

    for kw in bl.get("keywords", []):
        skw = str(kw).strip().lower()
        if not skw:
            continue
        if skw in name:
            return True, f"Chứa từ khóa cấm '{skw}'"

    return False, ""


def concat_videos(clip_paths: list[str], output_path: str) -> bool:
    """Ghép nhiều clip video thành 1 video 16s hoàn chỉnh bằng FFmpeg, có khóa FFMPEG_LOCK và luồng ưu tiên thấp."""
    if not clip_paths:
        return False
    if len(clip_paths) == 1:
        import shutil
        shutil.copy2(clip_paths[0], output_path)
        return True

    t0 = time.time()
    with FFMPEG_LOCK:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        list_file = os.path.join(tempfile.gettempdir(), f"concat_{int(time.time()*1000)}_{random.randint(100, 999)}.txt")
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
                "-movflags", "+faststart",
                output_path
            ]
            res = _run_ffmpeg_cmd(cmd, timeout=120)
            if res.returncode != 0:
                err1 = (res.stderr.decode("utf-8", errors="ignore") if isinstance(res.stderr, bytes) else str(res.stderr))[-400:]
                log.warning("concat_videos copy failed: %s; falling back to reencode", err1)
                # Thử 2: re-encode nếu stream khác codec hoặc timeline
                # Giới hạn -threads 2 để không chiếm dụng toàn bộ core CPU của máy (tránh đơ máy)
                cmd_reencode = [
                    "ffmpeg", "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", list_file,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-threads", "2",
                    "-c:a", "aac", "-b:a", "192k",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    output_path
                ]
                res = _run_ffmpeg_cmd(cmd_reencode, timeout=240)
                if res.returncode != 0:
                    err2 = (res.stderr.decode("utf-8", errors="ignore") if isinstance(res.stderr, bytes) else str(res.stderr))[-400:]
                    log.error("concat_videos reencode failed: %s", err2)

            ok = res.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 1000
            if ok:
                log.info("✓ [FFmpeg] Đã ghép 16s thành công: %s (%.2fs)", os.path.basename(output_path), time.time() - t0)
            return ok
        except Exception as e:
            log.warning("Lỗi concat_videos: %s", e)
            return False
        finally:
            if os.path.exists(list_file):
                try:
                    os.remove(list_file)
                except Exception:
                    pass


def build_video_prompts_16s(product_name: str, scene_choice: str = "🎲 Random", lang: str = "ph", review_style: str = "Unboxing") -> tuple[list[str], str]:
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
            f'ABSOLUTELY NO human face, NO head visible. Only two clean natural hands unboxing, picking up, and revealing the product. '
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

def call_seed_api(server_url: str, api_key: str, method: str, path: str, data: dict = None, timeout: int = 30):
    """Gọi API Server PostgreSQL trung tâm từ Novagate."""
    url = server_url.rstrip("/") + path
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    if method == "GET":
        req = urllib.request.Request(url, headers=headers)
    else:
        body = json.dumps(data or {}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def claim_jobs_from_server(
    server_url: str = DEFAULT_SERVER_URL,
    api_key: str = DEFAULT_API_KEY,
    client_id: str = DEFAULT_CLIENT_ID,
    market: str = "PH",
    limit: int = 2000,
    sort_by: str = "sold",
    min_item_id: int = 40000000000,
    min_commission: float = 1.0,
    min_sold: int = 0,
    min_price: float = 0.0,
    max_price: float = None,
    return_stats: bool = False,
) -> list[dict] | tuple[list[dict], int]:
    """Nhận lô sản phẩm từ Server PostgreSQL trung tâm của Shopee và tự động lọc Blacklist."""
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
        "min_price": min_price,
        "minPrice": min_price,
    }
    if max_price is not None:
        payload["max_price"] = max_price
        payload["maxPrice"] = max_price

    res = call_seed_api(server_url, api_key, "POST", "/api/thinaptm/claim-jobs", payload)
    raw = res.get("products", []) if isinstance(res, dict) else []

    bl = get_blacklist()
    blacklisted_count = 0
    blacklisted_samples = []

    products = []
    for p in raw:
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
        price_val = parse_price(p.get("price", 0))

        # Lọc chặt chẽ ở client
        if (min_item_id > 0 and iid < min_item_id) or comm < min_commission \
                or sold_val < min_sold or price_val < min_price \
                or (max_price is not None and price_val > max_price):
            continue

        # Kiểm tra Blacklist
        is_bl, reason = is_product_blacklisted(p, bl)
        if is_bl:
            blacklisted_count += 1
            if len(blacklisted_samples) < 5:
                blacklisted_samples.append(f"SP {p.get('item_id')}: {reason}")
            continue

        products.append(p)

    if blacklisted_count > 0:
        log.info("🚫 Blacklist đã loại bỏ %d sản phẩm vi phạm (%s)", blacklisted_count, "; ".join(blacklisted_samples))

    if return_stats:
        return products, blacklisted_count
    return products


def release_stuck_jobs(server_url: str = DEFAULT_SERVER_URL, api_key: str = DEFAULT_API_KEY, client_id: str = DEFAULT_CLIENT_ID) -> int:
    """Giải phóng các sản phẩm đang kẹt ở trạng thái processing."""
    res = call_seed_api(server_url, api_key, "POST", "/api/thinaptm/release-jobs", {"clientId": client_id})
    return res.get("released", 0) if isinstance(res, dict) else 0


def report_job_completion(
    server_url: str = DEFAULT_SERVER_URL,
    api_key: str = DEFAULT_API_KEY,
    item_id: str = "",
    status: str = "completed",
    video_path: str = None
) -> bool:
    """Báo cáo trạng thái hoàn thành của sản phẩm về Server PostgreSQL."""
    payload = {"itemId": item_id, "status": status, "tool": "museai"}
    if video_path:
        payload["video_path"] = video_path
    try:
        call_seed_api(server_url, api_key, "POST", "/api/thinaptm/complete-job", payload)
        return True
    except Exception:
        return False


def download_image_as_data_url(image_url: str, retries: int = 3) -> str | None:
    """Tải ảnh sản phẩm từ Shopee CDN về bộ nhớ và trả về chuỗi Data URL Base64."""
    if not image_url:
        return None
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(image_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                content_type = resp.headers.get("Content-Type", "image/jpeg")
                data = resp.read()
                if len(data) > 0:
                    b64 = base64.b64encode(data).decode("utf-8")
                    return f"data:{content_type};base64,{b64}"
        except Exception:
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    return None


def download_image_to_file(image_url: str, target_path: str, retries: int = 3) -> bool:
    """Tải ảnh sản phẩm từ Shopee CDN và lưu trực tiếp ra file trên ổ đĩa."""
    if not image_url or not target_path:
        return False
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"}
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(image_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
                if len(data) > 0:
                    with open(target_path, "wb") as f:
                        f.write(data)
                    return True
        except Exception:
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
    return False


def get_media_duration(vp: str) -> float:
    """Đo thời lượng video bằng ffprobe."""
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', vp]
    try:
        result = _run_ffmpeg_cmd(cmd, timeout=10, text=True)
        return float(result.stdout.strip())
    except Exception:
        return 8.0


def ghep_anh_12s(
    video_path: str,
    image_path: str,
    output_path: str,
    ai_duration: float | None = None,
    total_duration: float = 12.0
) -> bool:
    """Ghép ảnh sản phẩm làm Outro vào video AI để tạo thành video chuẩn 12.0s:
    - Nếu video AI 8s: giữ nguyên tốc độ chuẩn 8s (không làm chậm), tạo Outro ảnh phóng to (zoom-in) 4s -> Tổng 12.0s.
    - Nếu video AI 10s: giữ nguyên tốc độ chuẩn 10s, tạo Outro ảnh phóng to (zoom-in) 2s -> Tổng 12.0s.
    - Xử lý làm mượt âm thanh (fade out tự nhiên cuối đoạn AI) để nối sang Outro không bị giật/nổ tiếng.
    - Được đồng bộ qua FFMPEG_LOCK và ưu tiên tiến trình thấp để CPU không bị quá tải làm đơ máy.
    """
    if not os.path.isfile(video_path) or not os.path.isfile(image_path):
        return False

    t0 = time.time()
    with FFMPEG_LOCK:
        def check_has_audio(vp):
            cmd = ['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=codec_name', '-of', 'default=noprint_wrappers=1:nokey=1', vp]
            try:
                result = _run_ffmpeg_cmd(cmd, timeout=10, text=True)
                return len(result.stdout.strip()) > 0
            except Exception:
                return False

        def get_video_info(vp):
            cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,r_frame_rate', '-of', 'json', vp]
            try:
                result = _run_ffmpeg_cmd(cmd, timeout=10, text=True)
                data = json.loads(result.stdout)
                stream = data['streams'][0]
                width = int(stream['width'])
                height = int(stream['height'])
                fps_str = stream['r_frame_rate']
                if '/' in fps_str:
                    num, den = fps_str.split('/')
                    fps = float(num) / float(den)
                else:
                    fps = float(fps_str)
                return width, height, fps
            except Exception:
                return 720, 1280, 24.0

        try:
            has_audio = check_has_audio(video_path)
            width, height, fps = get_video_info(video_path)
            fps_int = int(round(fps))
            if fps_int <= 0:
                fps_int = 24

            # Xác định độ dài video AI (8s hoặc 10s)
            if ai_duration is not None and ai_duration > 0:
                chosen_ai_dur = float(ai_duration)
            else:
                chosen_ai_dur = get_media_duration(video_path)

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

            # Video AI nguyên bản, không làm chậm, cắt chuẩn video_dur
            v_part = f"[0:v]scale={width}:{height},fps={fps_int},tpad=stop_mode=clone:stop_duration={video_dur},trim=0:{video_dur},setpts=PTS-STARTPTS[v_part]"

            # Outro ảnh chuyển động phóng to (zoom-in)
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

            res = _run_ffmpeg_cmd(cmd, timeout=90)
            if res.returncode != 0:
                err = (res.stderr.decode("utf-8", errors="ignore") if isinstance(res.stderr, bytes) else str(res.stderr))[-400:]
                log.warning("ghep_anh_12s failed: %s", err)
                return False
            ok = os.path.isfile(output_path) and os.path.getsize(output_path) > 1000
            if ok:
                log.info("✓ [FFmpeg] Đã ghép Outro 12s vào %s thành công (%.2fs)", os.path.basename(output_path), time.time() - t0)
            return ok
        except Exception as e:
            log.warning("Lỗi ghép video 12s bằng ffmpeg: %s", e)
            return False
