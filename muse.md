# 📘 MUSEAI VIDEO STUDIO PRO - TÀI LIỆU TOÀN DIỆN VỀ KIẾN TRÚC, THUẬT TOÁN & CODE QUAN TRỌNG

> **Phiên bản hệ thống:** v1.2.2  
> **Nền tảng:** Windows 10/11 (Python 3.12, FastAPI, Chrome DevTools Protocol, FFmpeg)  
> **Mục đích:** Tài liệu kỹ thuật chi tiết lưu trữ toàn bộ kiến trúc lõi, thuật toán then chốt, quy trình xử lý luồng công việc và các đoạn code quan trọng nhất của hệ thống MuseAI Studio Pro.

---

## 📑 MỤC LỤC

1. [Tổng Quan Kiến Trúc Hệ Thống](#1-tổng-quan-kiến-trúc-hệ-thống)
2. [Quy Trình & Thuật Toán Tạo Video Shopee 16s](#2-quy-trình--thuật-toán-tạo-video-shopee-16s)
3. [Thuật Toán Tự Động Quét & Ghép Thư Mục Temp (Auto-Stitch)](#3-thuật-toán-tự-động-quét--ghép-thư-mục-temp-auto-stitch)
4. [Thuật Toán & Quy Trình Import URL Shopee Từ TXT](#4-thuật-toán--quy-trình-import-url-shopee-từ-txt)
5. [Thuật Toán Điều Phối Hồ Tài Khoản & Session Pooling](#5-thuật-toán-điều-phối-hồ-tài-khoản--session-pooling)
6. [Cơ Chế HomeProxy Sticky & Anti-Detect Stealth](#6-cơ-chế-homeproxy-sticky--anti-detect-stealth)
7. [Thuật Toán Tự Động Duy Trì Phiên (Keepalive & VM Wake)](#7-thuật-toán-tự-động-duy-trì-phiên-keepalive--vm-wake)
8. [Hệ Thống Lọc Nội Dung & Blacklist Tự Động](#8-hệ-thống-lọc-nội-dung--blacklist-tự-động)
9. [Thuật Toán Tính Tốc Độ Tạo Video (10-Phút Rolling Window)](#9-thuật-toán-tính-tốc-độ-tạo-video-10-phút-rolling-window)
10. [Các Đoạn Mã Nguồn Cốt Lõi (Core Code Snippets)](#10-các-đoạn-mã-nguồn-cốt-lõi-core-code-snippets)
11. [Xử Lý Sự Cố & Tối Ưu Hóa Timeout Render Đa Luồng (Fix Lỗi Hết Thời Gian Chờ Render Clip Video)](#11-xử-lý-sự-cố--tối-ưu-hóa-timeout-render-đa-luồng-fix-lỗi-hết-thời-gian-chờ-render-clip-video)

---

## 1. TỔNG QUAN KIẾN TRÚC HỆ THỐNG

MuseAI Video Studio Pro được thiết kế theo mô hình **Desktop-Assisted Microservices**:
- **Backend Core (`app.py`, FastAPI)**: Chạy trên cổng nội bộ `18610`, quản trị toàn bộ REST API, xử lý tác vụ bất đồng bộ, hàng đợi sản phẩm, và điều phối pool tài khoản.
- **CDP Engine (`engine.py`, `cdp.py`)**: Tương tác trực tiếp với Chrome thông qua Chrome DevTools Protocol qua WebSocket. Quản lý các Browser Context độc lập cho từng tài khoản.
- **Proxy Management (`proxy_manager.py`)**: Mở các cổng Local TCP Forwarder (127.0.0.1:port) chuyển tiếp tới proxy bên ngoài có xác thực username:password, hỗ trợ cơ chế Sticky Proxy cố định theo tài khoản.
- **Shopee Integration (`shopee_engine.py`, `shopee_scraper.py`)**: Module chuyên biệt lấy dữ liệu Shopee từ database nội bộ hoặc URL, làm sạch tiêu đề, tải ảnh bìa và chia tách kịch bản.
- **Frontend SPA (`studio.html`)**: Giao diện điều khiển Studio hiện đại, hỗ trợ giám sát tiến độ theo thời gian thực, quản lý blacklist, cấu hình TTL, và điều khiển hàng loạt.
- **Desktop Launcher (`desktop_app.py`, `Chay_MESUAI.bat`)**: Khởi động backend và mở cửa sổ WebView Desktop Windows độc lập.

```mermaid
flowchart TD
    UI[Frontend Studio HTML / Desktop GUI] <-->|HTTP / REST API| App[Backend FastAPI app.py]
    App <--> Store[Account Store store.py & accounts.json]
    App <--> ProxyMgr[Proxy Manager & Forwarders proxy_manager.py]
    App <--> ShopeeEng[Shopee Engine & Scraper shopee_engine.py]
    App <--> MuseEng[Muse Engine & Worker Session engine.py]
    MuseEng <-->|CDP WebSocket ws://127.0.0.1:9222| Chrome[Headless Chromium / Chrome Browser]
    ProxyMgr <-->|TCP Tunnel| ExternalProxy[Proxy Pool vproxy / uproxy / cproxy]
    ShopeeEng <-->|FFmpeg Subprocess| FFmpeg[FFmpeg Stream Copy Engine]
    FFmpeg --> OutputVideo[F:/Video AI/VN/Mesu/{item_id}.mp4]
```

---

## 2. QUY TRÌNH & THUẬT TOÁN TẠO VIDEO SHOPEE 16S

### 2.1. Bản Chất Kiến Trúc 16 Giây
Mô hình AI của Muse.ai giới hạn tối đa 8 giây cho mỗi lần sinh clip (`kind="video"`). Để tạo ra một video quảng cáo hoàn chỉnh 16 giây, hệ thống áp dụng kỹ thuật **Dual-Clip Chaining**:
1. **Clip A (8s)**: Cảnh mở đầu (ví dụ: góc quay cận cảnh hoặc đập hộp Unboxing giới thiệu sản phẩm từ ảnh bìa).
2. **Clip B (8s)**: Cảnh trình diễn công năng / góc nhìn thứ hai (trình diễn tính năng, trải nghiệm người dùng thực tế).

### 2.2. Quy Trình Xử Lý Chi Tiết (Pipeline)
```mermaid
flowchart LR
    A[Nhận SP / Import Link] --> B[Kiểm tra Blacklist & Trùng lặp]
    B --> C[Tải Ảnh Bìa HD]
    C --> D{Kiểm tra temp/ đã có clip chưa?}
    D -- Đã có Clip A & B --> G[Ghép nối FFmpeg 16s]
    D -- Chưa có Clip A --> E1[Thuê TK 1 sinh Clip A 8s]
    D -- Chưa có Clip B --> E2[Thuê TK 2 sinh Clip B 8s]
    E1 --> F[Lưu temp/{item_id}-a.mp4]
    E2 --> F2[Lưu temp/{item_id}-b.mp4]
    F & F2 --> G
    G --> H[Lưu video hoàn chỉnh 16s]
    H --> I[Tự động xóa sạch clip tạm A & B]
```

### 2.3. Cơ Chế Tái Sử Dụng File Tạm (Temp Reuse) Theo Độ Dài Video
Trước khi gửi yêu cầu render lên Muse.ai, hệ thống kiểm tra sự tồn tại của file theo độ dài video:
- **Với video độ dài 8s và 10s**: Tuyệt đối **KHÔNG** kiểm tra hoặc tận dụng clip trong thư mục `temp/` (chỉ kiểm tra xem đã có video thành phẩm hoàn chỉnh trong thư mục xuất hay chưa). Điều này tránh việc nhầm lẫn hoặc tái sử dụng nhầm các clip thô dở dang của video 16s trước đó.
- **Với video độ dài 16s** (Chế độ ghép Dual-Clip Prompt A + B): Quét thư mục `temp/` (hoặc cấu hình `clip_cache.json`):
  - Nếu `{item_id}-a.mp4` đã tồn tại: Bỏ qua tạo Clip A.
  - Nếu `{item_id}-b.mp4` đã tồn tại: Bỏ qua tạo Clip B.
  - Nếu cả hai clip A và B đã có sẵn: Chuyển thẳng sang bước ghép FFmpeg, tiết kiệm 100% thời gian và tài nguyên tài khoản.

### 2.4. Thuật Toán Ghép Video Bằng FFmpeg Stream Copy (Không Render Lại)
Hai clip 8s được tạo ra từ cùng một mô hình Muse.ai có cùng kích thước (`720x1280`), cùng codec (`h264`), cùng FPS (`24fps/25fps`) và cùng profile màu. Do đó, hệ thống sử dụng giao thức **FFmpeg Concat Demuxer** với cờ `-c copy`:
- **Tốc độ ghép**: ~0.05 giây (chỉ sao chép luồng bit trực tiếp, không re-encode).
- **Chất lượng**: 100% nguyên gốc, không suy giảm chất lượng qua quá trình mã hóa lại.

---

## 3. THUẬT TOÁN TỰ ĐỘNG QUÉT & GHÉP THƯ MỤC TEMP (AUTO-STITCH)

Khi người dùng nhấn nút **`[⚡ Ghép Clip Temp (16s)]`** hoặc gọi API `POST /api/shopee/auto-stitch-temp`:

```python
# Thuật toán quét và gom cặp file A & B trong temp
pattern = re.compile(r"^(.+)-([ab])\.(mp4|webm)$", re.IGNORECASE)
groups = {}  # { base_id: {"a": path_a, "b": path_b} }

for fname in os.listdir(temp_dir):
    m = pattern.match(fname)
    if m:
        item_id, part, ext = m.group(1), m.group(2).lower(), m.group(3).lower()
        if item_id not in groups:
            groups[item_id] = {}
        groups[item_id][part] = os.path.join(temp_dir, fname)

# Chỉ xử lý các item có đủ cả 2 phần 'a' và 'b'
valid_pairs = {k: v for k, v in groups.items() if "a" in v and "b" in v}
```

### Quy Trình Ghép & Dọn Dẹp:
1. Tạo file danh sách tạm thời `concat_{item_id}.txt`:
   ```
   file 'F:/Video AI/VN/Mesu/temp/12345-a.mp4'
   file 'F:/Video AI/VN/Mesu/temp/12345-b.mp4'
   ```
2. Thực thi lệnh FFmpeg:
   ```bash
   ffmpeg -y -f concat -safe 0 -i concat_12345.txt -c copy "F:/Video AI/VN/Mesu/12345.mp4"
   ```
3. Sau khi xuất file đích thành công: **Tự động xóa ngay lập tức** file `12345-a.mp4`, `12345-b.mp4` và file danh sách tạm để giải phóng dung lượng đĩa cứng.

---

## 4. THUẬT TOÁN & QUY TRÌNH IMPORT URL SHOPEE TỪ TXT

Hỗ trợ nhập link từ file `.txt` hoặc dán văn bản hàng loạt với bất kỳ định dạng link Shopee nào:

### 4.1. Thuật Toán Trích Xuất URL & Phân Tích ID
Hệ thống sử dụng các biểu thức chính quy (Regex) tối ưu để nhận diện toàn bộ các dạng URL Shopee:
1. **Link rút gọn chia sẻ**: `https://vn.shp.ee/xxxx` ➔ Tự động gửi HTTP GET theo dõi Redirect 302 để giải mã URL gốc chuẩn.
2. **Link chi tiết sản phẩm chuẩn**:
   ```python
   # Dạng 1: shopee.vn/product/{shop_id}/{item_id}
   r"/product/(\d+)/(\d+)"
   
   # Dạng 2: shopee.vn/{slug}-i.{shop_id}.{item_id}
   r"-i\.(\d+)\.(\d+)"
   ```

### 4.2. Cơ Chế Cào Dữ Liệu Sản Phẩm (Scraping Pipeline)
- **Phương thức 1 (Shopee Public API)**: Gọi trực tiếp endpoint `https://shopee.vn/api/v4/item/get?itemid={item_id}&shopid={shop_id}` để lấy tiêu đề gốc, danh sách ảnh HD (`https://down-vn.img.susercontent.com/file/{image_hash}`), giá bán và doanh số.
- **Phương thức 2 (Playwright Fallback)**: Nếu API bị hạn chế hoặc yêu cầu captcha, hệ thống kích hoạt Playwright Headless Browser nạp trang và bóc tách dữ liệu thông qua DOM selectors.
- **Làm sạch tiêu đề (`clean_product_title`)**: Tự động loại bỏ các tag khuyến mãi thừa như `[Mã giảm giá]`, `(Chính hãng 100%)`, `Freeship Xtra`, icon cảm xúc,... để prompt tạo video chuẩn xác nhất.

### 4.3. Quản Lý & Chỉnh Sửa Máy Chủ Database Trực Tiếp Từ Web UI
- **Giao diện**: Nút `[✏️ Sửa DB]` trên thanh tiêu đề Card 2 ("Nhận Lô Sản Phẩm từ Database Shopee") mở modal cấu hình trực tiếp:
  - `Server URL`: Địa chỉ IP/domain máy chủ database (ví dụ: `http://100.79.170.67:3000`).
  - `Client ID`: Tên định danh máy trạm (mặc định lấy theo tên máy tính đang chạy, ví dụ: `XEON-CT2A`).
  - `API Key`: Khóa bảo mật API (`shopee_secret_2026`).
- **Nút "⚡ Kiểm Tra Kết Nối (Ping)"**: Kiểm tra phản hồi HTTP và trạng thái mở cổng TCP tới máy chủ Database với timeout 4s.
- **Nút "💾 Lưu Cấu Hình"**: Tự động cập nhật đồng thời 4 tầng:
  1. Biến môi trường runtime `os.environ` và bộ nhớ RAM của module `shopee_engine`.
  2. Ghi vĩnh viễn vào file `.env` qua hàm `_update_env_file` (bảo toàn cấu hình khi khởi động lại).
  3. Ghi vào `data/shopee_settings.json`.
  4. Cập nhật nhãn hiển thị tức thì trên giao diện mà không cần tải lại trang.

---

## 5. THUẬT TOÁN ĐIỀU PHỐI HỒ TÀI KHOẢN & SESSION POOLING

### 5.1. Mô Hình Worker Session Độc Lập (Isolated Session)
Mỗi tác vụ tạo clip không dùng chung trình duyệt mà được cấp phát một **`MuseWorkerSession`** riêng biệt:
- Mỗi Session sở hữu một **Browser Context** riêng với bộ Cookie của tài khoản đó.
- Không chia sẻ bộ nhớ, không xung đột tab, không rò rỉ biến ngữ cảnh giữa các luồng.

```python
class MuseWorkerSession:
    def __init__(self, engine, bc_id, target_id, page, account_id, proxy_str, forwarder_port, ttl_seconds=1800, max_tasks=5):
        self.bc_id = bc_id              # ID của Browser Context trên Chrome
        self.target_id = target_id      # ID của DevTools Page Target
        self.page = page                # Kết nối CDP WebSocket
        self.account_id = account_id
        self.task_count = 0             # Đếm số task đã phục vụ
        self.ttl_seconds = ttl_seconds  # Thời gian sống tối đa (mặc định 1800s = 30 phút)
        self.max_tasks = max_tasks      # Tối đa 5 task / phiên trước khi tái sinh
        self.is_closed = False
        self.in_use = False
        self.pooled = True
        self.close_requested = False
```

### 5.2. Thuật Toán Điều Phối & Khóa An Toàn (Threading Condition)
- Quản lý trạng thái bận/rảnh bằng `threading.Condition` và khóa thread an toàn `_pool_lock`.
- Hàm `acquire_session()`: Kiểm tra xem tài khoản đã có session trong pool chưa. Nếu có và session còn sống (alive) + chưa hết hạn (not expired), tái sử dụng ngay (thời gian khởi tạo = 0s).
- Hàm `release_session()`: Khi tác vụ kết thúc, giải phóng session về pool. Nếu session đã hoàn thành đủ số task cho phép (`task_count >= max_tasks`) hoặc quá thời gian TTL (`now - created_at >= ttl_seconds`), hệ thống tự động đóng session và tái sinh phiên mới sạch sẽ.

### 5.3. Thuật Toán Tự Động Failover (Chuyển Tài Khoản Dự Phòng)
Khi một tài khoản gặp lỗi trong quá trình tạo video (hết hạn cookie, lỗi kết nối hoặc vi phạm an toàn nội dung):
1. Đánh dấu tài khoản hiện tại vào danh sách tạm dừng.
2. Tự động giải phóng phiên của tài khoản bị lỗi (`release_session(session, error=True)`).
3. Tìm và thuê tài khoản dự phòng còn sống (`store.acquire_account(exclude_id=failed_id)`).
4. Tiếp tục thực hiện tác vụ trên tài khoản mới mà không làm gián đoạn toàn bộ batch.

---

## 6. CƠ CHẾ HOMEPROXY STICKY & ANTI-DETECT STEALTH

### 6.1. Local Proxy Forwarder (`proxy_manager.py`)
Trình duyệt Chromium không hỗ trợ truyền trực tiếp `username:password` cho HTTP proxy qua cờ dòng lệnh `--proxy-server`.  
Giải pháp: Hệ thống tích hợp một máy chủ chuyển tiếp TCP nội bộ:
```
Chrome ➔ 127.0.0.1:{local_port} ➔ [Local Forwarder] ➔ Remote Proxy (vproxy/uproxy/cproxy) kèm Basic Auth
```

### 6.2. Cơ Chế Sticky Proxy
Để tránh việc một tài khoản bị đăng nhập từ nhiều địa chỉ IP khác nhau dẫn đến bị khóa phiên:
- Hệ thống duy trì tệp `data/account_proxies.json`.
- Mỗi tài khoản `account_id` được gán cố định (`Sticky`) với một Proxy xác định.
- Mỗi khi khởi tạo Browser Context cho tài khoản đó, hệ thống luôn chọn đúng Proxy đã gán.

### 6.3. Thuật Toán Đồng Bộ HomeProxy.vn (Chuẩn ThinAptm)
Để lấy danh sách proxy đang hoạt động từ dịch vụ HomeProxy.vn (22+ proxy):
1. **Bước 1 - Lấy `x-merchant-id`**: Gửi request tới `GET /orders?page=1&limit=1` kèm `Bearer token` để trích xuất `merchant_id` của tài khoản người dùng.
2. **Bước 2 - Lấy danh sách proxy thực sự**: Gửi request tới `GET /users/proxies?page=1&limit=500` kèm header `x-merchant-id`. Lọc các proxy có trạng thái `Completed` và chưa hết hạn `expiredAt > now_ms`.
3. **Bước 3 - Fallback Orders**: Nếu danh sách `/users/proxies` rỗng, tự động phân tích các sản phẩm trong `/orders` để lấy thông tin kết nối.
4. **Hỗ trợ biến môi trường `HOMEPROXY_TOKEN`**: Lưu token trong `.env` để khi chạy trên máy tính khác (kể cả không có tool ThinAptm), hệ thống vẫn tự động tải toàn bộ 22 proxy về máy trạm.

### 6.4. Cấu Hình Anti-Detect Stealth Trình Duyệt
Khi khởi chạy Chrome, hệ thống inject các cờ vô hiệu hóa cơ chế phát hiện tự động hóa của Cloudflare và Muse:
- `--disable-blink-features=AutomationControlled`
- Ghi đè `navigator.webdriver = false` qua CDP `Page.addScriptToEvaluateOnNewDocument`.
- Chặn rò rỉ WebRTC IP thật (`WebRTC Lock`).
- Giả lập thông số phần cứng GPU (`GPU Spoofing`).

---

## 7. THUẬT TOÁN TỰ ĐỘNG DUY TRÌ PHIÊN (KEEPALIVE & VM WAKE)

Muse.ai có 2 cơ chế giới hạn phiên:
1. `hatch_vml`: Cookie phiên máy ảo, mặc định hết hạn sau 48 giờ.
2. Máy ảo Cloud VM của Muse.ai tự động rơi vào trạng thái ngủ đông (`SUSPENDED`) nếu không có thao tác sau 10-15 phút.

### 7.1. Chu Trình Tự Động Gia Hạn (Keepalive Coroutine)
Một tác vụ nền (`background loop`) chạy liên tục trong `app.py`:
1. Quét toàn bộ tài khoản trong `accounts.json`.
2. Gửi request trực tiếp tới `https://muse.ai/api/session` kèm Cookie hiện tại.
3. Nhận về token mới và tự động gia hạn `hatch_vml` thêm **+48 giờ** và `hatch_sess` thêm **+30 ngày**.
4. Cập nhật thời hạn mới vào cơ sở dữ liệu mà không cần người dùng phải đăng nhập lại thủ công.

### 7.2. Kỹ Thuật Đánh Thức Máy Ảo (Cloud VM Wake)
- Gửi HTTP POST tới `/api/hatch/vm/wake` trước khi gửi prompt.
- Đảm bảo trạng thái VM chuyển từ `SUSPENDED` sang `RUNNING` trước khi tác vụ bắt đầu, loại bỏ hoàn toàn lỗi `"Cloud VM connection timeout"`.

---

## 8. HỆ THỐNG LỌC NỘI DUNG & BLACKLIST TỰ ĐỘNG

Dữ liệu lưu tại `data/blacklist.json`:
- `keywords`: Danh sách các từ khóa cấm (ví dụ: các từ nhạy cảm, cấm quảng cáo, vi phạm chính sách nội dung của AI).
- `item_ids`: Danh sách các mã sản phẩm bị cấm vĩnh viễn.

### 8.1. Thuật Toán Đối Soát Từ Khóa Đa Tầng (`_match_blacklist_keyword`)
- **Tầng 1 - Chuẩn hóa chữ**: Chuyển chuỗi về chữ thường, loại bỏ toàn bộ dấu tiếng Việt (`_strip_accents`).
- **Tầng 2 - Regex Word Boundary**: Sử dụng ranh giới từ `\bkeyword\b` để tránh nhận diện nhầm từ con (ví dụ: từ cấm "ba" không chặn từ "bánh").
- **Tầng 3 - Đối soát nguyên bản có dấu**: Đảm bảo bắt đúng các từ khóa đặc thù có dấu.

### 8.2. Cơ Chế Auto-Block Khi Gặp Lỗi Chính Sách
Khi Muse.ai trả về cảnh báo vi phạm an toàn nội dung (`Safety Policy Error`):
- Hệ thống tự động trích xuất `item_id` và từ khóa vi phạm.
- Tự động ghi vào `data/blacklist.json`.
- Loại bỏ sản phẩm đó khỏi hàng đợi ngay lập tức và chuyển sang sản phẩm kế tiếp.

---

## 9. THUẬT TOÁN TÍNH TỐC ĐỘ TẠO VIDEO (10-PHÚT ROLLING WINDOW)

Để phản ánh chính xác tốc độ sản xuất video thực tế (tránh bị méo số liệu do các khoảng nghỉ giữa các đợt chạy), chỉ số `video/phút` được tính toán bằng thuật toán **Cửa sổ trượt 10 phút (Rolling Window)**:

```javascript
// Thuật toán triển khai tại studio.html
function updateShopeeSpeedText() {
  const now = Date.now();
  const windowMs = 10 * 60 * 1000; // Cửa sổ 10 phút = 600,000 ms

  // Lọc chỉ giữ lại các mốc thời gian hoàn thành trong 10 phút gần nhất
  shopeeCompletionTimes = shopeeCompletionTimes.filter(t => (now - t) <= windowMs);
  
  if (shopeeCompletionTimes.length === 0) {
    speedEl.innerText = "⚡ 0.0 video/phút";
    return;
  }

  // Thời gian trôi qua thực tế trong cửa sổ 10 phút (tối đa 10 phút, tối thiểu 0.2 phút để tránh chia 0)
  const elapsedSinceStartMin = Math.max((now - shopeeBatchStartTime) / 60000, 0.2);
  const effectiveWindowMin = Math.min(elapsedSinceStartMin, 10.0);

  // Tốc độ = Tổng số video hoàn thành trong cửa sổ / Số phút thực tế
  const rate = (shopeeCompletionTimes.length / effectiveWindowMin).toFixed(1);
  speedEl.innerText = `⚡ ${rate} video/phút`;
}
```

---

## 10. CÁC ĐOẠN MÃ NGUỒN CỐT LÕI (CORE CODE SNIPPETS)

### 10.1. Ghép Video Bằng FFmpeg Stream Copy (`shopee_engine.py`)
```python
def concat_videos(clip_paths: list[str], output_path: str) -> bool:
    """Ghép nối danh sách video bằng FFmpeg Concat Demuxer với Stream Copy (không re-encode)."""
    if not clip_paths:
        return False
    if len(clip_paths) == 1:
        shutil.copy2(clip_paths[0], output_path)
        return True

    concat_list_path = output_path + ".txt"
    try:
        with open(concat_list_path, "w", encoding="utf-8") as f:
            for p in clip_paths:
                f.write(f"file '{os.path.abspath(p).replace(chr(92), '/')}'\n")

        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
            "-movflags", "+faststart",
            output_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        return res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000
    finally:
        if os.path.exists(concat_list_path):
            try:
                os.remove(concat_list_path)
            except Exception:
                pass
```

### 10.2. Bọc Lỗi Kết Nối Socket CDP An Toàn (`cdp.py`)
```python
def send(self, method: str, params: dict | None = None, timeout: float | None = None):
    """Gửi lệnh CDP qua WebSocket với bọc try-except an toàn tránh sập luồng do WinError 10054."""
    with self._lock:
        self._id += 1
        mid = self._id
        try:
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        except (websocket.WebSocketException, OSError) as exc:
            raise CDPError(f"{method}: CDP Kết nối đã ngắt ({type(exc).__name__})") from None

        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            try:
                msg = json.loads(self.ws.recv())
            except (websocket.WebSocketTimeoutException, socket.timeout):
                continue
            except (websocket.WebSocketException, OSError) as exc:
                raise CDPError(f"{method}: CDP Kết nối đã ngắt ({type(exc).__name__})") from None
            except ValueError:
                continue

            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(f"{method}: {msg['error']}")
                return msg
        raise TimeoutError(f"CDP {method} Quá thời gian chờ")
```

### 10.3. Nhập Prompt & Gửi Lệnh Bằng Javascript Injection (`engine.py`)
```javascript
// Mã JavaScript can thiệp trực tiếp vào React DOM của Muse.ai để gõ prompt và kích hoạt nút gửi
(function(text) {
    var ta = document.querySelector('textarea');
    if (!ta) return 0;
    ta.focus();
    
    // Ghi đè prototype setter của HTMLTextAreaElement để vượt qua bộ lắng nghe React Synthetic Event
    var s = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
    s.call(ta, text);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    ta.dispatchEvent(new Event('change', { bubbles: true }));
    return (ta.value || '').length;
})(prompt_text);
```

### 10.4. Trích Xuất ItemID và ShopID Từ URL Shopee (`shopee_scraper.py`)
```python
def extract_ids_from_url(url: str) -> tuple[str, str]:
    """Trích xuất (shop_id, item_id) từ mọi biến thể URL Shopee."""
    # Dạng 1: shopee.vn/product/{shop_id}/{item_id}
    m1 = re.search(r"/product/(\d+)/(\d+)", url)
    if m1:
        return m1.group(1), m1.group(2)
        
    # Dạng 2: shopee.vn/{product_name}-i.{shop_id}.{item_id}
    m2 = re.search(r"-i\.(\d+)\.(\d+)", url)
    if m2:
        return m2.group(1), m2.group(2)
        
    # Dạng 3: Tham số truy vấn ?item_id=xxx&shop_id=yyy
    m_item = re.search(r"[?&]item(?:_?id|Id)=(\d+)", url)
    m_shop = re.search(r"[?&]shop(?:_?id|Id)=(\d+)", url)
    if m_item:
        return (m_shop.group(1) if m_shop else "0"), m_item.group(1)
        
    # Dạng 4: Chuỗi số độc lập cuối URL
    m3 = re.search(r"/(\d{8,14})(?:[/?#]|$)", url)
    if m3:
        return "0", m3.group(1)
        
    return "", ""
```

---

## 11. XỬ LÝ SỰ CỐ & TỐI ƯU HÓA TIMEOUT RENDER ĐA LUỒNG (FIX LỖI HẾT THỜI GIAN CHỜ RENDER CLIP VIDEO)

### 11.1. Hiện Tượng & Triệu Chứng
Khi người dùng chạy nhiều luồng đồng thời (ví dụ: 15-20 luồng), nhật ký xử lý ghi nhận hàng loạt luồng báo lỗi đồng thời vào cùng một giây:
```text
[11:29:03] ❌ [Luồng 6] Lỗi SP 41209244493: Hết thời gian chờ render clip video
[11:29:03] ❌ [Luồng 8] Lỗi SP 41832672461: Hết thời gian chờ render clip video
[11:29:03] ❌ [Luồng 7] Lỗi SP 42024222334: Hết thời gian chờ render clip video
[11:29:03] ❌ [Luồng 13] Lỗi SP 42122076115: Hết thời gian chờ render clip video
```

### 11.2. Bản Chất Nguyên Nhân (Root Causes)
Qua phân tích mã nguồn và nhật ký thực thi chi tiết:
1. **Lệch Timeout giữa Frontend và Backend**:
   - Trong `config.py`, Backend cấu hình `MUSE2API_VIDEO_TIMEOUT = 600` (10 phút = 600 giây).
   - Nhưng trong `studio.html` (`renderSingleVideoClip`), vòng lặp polling kiểm tra trạng thái task (`fetch("/v1/videos/" + taskId)`) bị gán cứng giới hạn:
     `while (Date.now() - startPoll < 300000)` (300,000ms = 300 giây = đúng 5 phút).
   - Khi chạy nhiều luồng cùng lúc (15-20 luồng), các tác vụ phải xếp hàng thuê tài khoản (`acquire_account`), khởi tạo Browser Context qua HomeProxy, điều hướng và gửi prompt lên Muse.ai. Dưới tải trọng cao, thời gian render thực tế của Muse.ai có thể kéo dài từ 3.5 đến 5.5 phút.
   - Khi vượt qua giây thứ 300, Frontend ngay lập tức tự hủy vòng lặp và ném lỗi `throw new Error("Hết thời gian chờ render clip video")`, trong khi Backend thực tế vẫn đang xử lý bình thường. Vì các luồng cùng đợt xuất phát cùng lúc, chúng cùng chạm mốc 300 giây tại cùng một thời điểm.

2. **Thiếu Cơ Chế Phát Hiện Sớm (Quick Fail) Khi Mô Hình Từ Chối / Chỉ Trả Về Văn Bản**:
   - Trong `engine.py` (lớp `MuseWorkerSession._wait_attachment`), thiếu biến theo dõi văn bản ổn định `last_txt`, `txt_stable`. Nếu tài khoản hết lượt, bị giới hạn tần suất, hoặc mô hình AI trả lời bằng văn bản từ chối (refusal/policy) thay vì sinh video, luồng worker sẽ bị treo ngủ chờ mù quáng suốt 5-10 phút thay vì báo lỗi và giải phóng tài khoản ngay lập tức.

### 11.3. Giải Pháp Đã Triển Khai (Fix Triệt Để)
1. **Đồng Bộ Timeout Frontend Lên 10 Phút (600s)** trong `studio.html`:
   - Nâng `maxPollMs = 600000` (600 giây), khớp hoàn toàn với `MUSE2API_VIDEO_TIMEOUT` của Backend.
   - Bổ sung tham số `timeout: 600` gửi trực tiếp trong payload POST `/v1/videos`.
   - Bổ sung log báo tiến độ định kỳ mỗi 60 giây (`> [Luồng X] Task ... vẫn đang render trên Muse.ai (120s/600s, tiến độ ~85%)...`) giúp người dùng theo dõi trực quan trạng thái tác vụ.
2. **Cơ Chế Quick Fail & Quota Detect Trong `MuseWorkerSession`** (`engine.py`):
   - Bổ sung phát hiện sớm nếu mô hình chỉ trả về văn bản mà không sinh video (kiểm tra `hasStop == False` và `cur_cnt > base_agent_cnt` cùng sự ổn định của văn bản `txt_stable >= 15`), ngay lập tức ném lỗi rõ ràng sau ~8-10 giây thay vì đợi 10 phút.
   - Nhận diện các từ khóa hết quota/rate-limit mở rộng: `quota exceeded`, `limit reached`, `out of credits`, `token limit`.
3. **Khuyến Nghị Vận Hành Đa Luồng**:
   - Khi chạy trên máy trạm thông thường, nên duy trì từ **8 đến 12 luồng song song** để đạt tỷ lệ hoàn thành cao nhất, tránh hiện tượng nghẽn I/O Chrome và nghẽn băng thông proxy.

---

## 📌 GHI CHÚ BẢO TRÌ & QUY TẮC PHÁT TRIỂN
1. **Tuyệt đối không tự động upload code lên GitHub**: Mọi thay đổi mã nguồn trên máy phải để người dùng chủ động chạy `upload-github.bat`.
2. **Khả năng tương thích Windows**: Giữ cho toàn bộ các script batch, đường dẫn file (`\\` và `/`), và mã hóa chuỗi luôn là `UTF-8` tương thích hoàn toàn với hệ điều hành Windows.
3. **Giải phóng tài nguyên đĩa cứng**: Mọi quy trình ghép video đều phải kèm bước tự động dọn dẹp các tệp tạm `-a.mp4`, `-b.mp4` và `-raw` để bảo vệ dung lượng ổ cứng của người dùng.
4. **Cơ chế Cập Nhật Kép (Dual-Engine / Zero-Git Mode)**: Công cụ `update.bat` và `tools/update_app.py` hỗ trợ cập nhật trên bất kỳ máy tính Windows nào mà **không bắt buộc phải cài đặt Git**. Nếu máy không có Git, hệ thống tự động tải gói ZIP mới nhất trực tiếp từ GitHub, bảo toàn nguyên vẹn tệp `.env`, thư mục `data/` và `output/`.
5. **Cơ chế Nạp Proxy Đa Nguồn**: `proxy_manager.py` tự động quét tìm HomeProxy từ thư mục ThinAptm trên nhiều ổ đĩa (`E:`, `D:`, `C:`, `F:`) hoặc đọc trực tiếp danh sách proxy từ tệp `proxy.txt` ngay trong thư mục MuseAI.

