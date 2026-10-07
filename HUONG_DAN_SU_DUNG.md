# 🎬 Hướng Dẫn Sử Dụng Phần Mềm Desktop "MuseAI Video Studio Pro"

**MuseAI Video Studio Pro** là phần mềm Desktop chuyên dụng trên Windows, được xây dựng dựa trên kiến trúc đảo ngược (reverse-engineered) của **[muse.ai](https://muse.ai/)**, chuyển đổi toàn bộ sức mạnh tạo video & hình ảnh AI của nền tảng thành một studio đồ họa desktop hiện đại, mượt mà và trực quan 100% bằng Tiếng Việt.

---

## 🌟 1. CÁC TÍNH NĂNG CHÍNH

1. **📝 Tạo Video từ Văn Bản (Text-to-Video)**:
   - Nhập kịch bản chi tiết bằng tiếng Việt hoặc tiếng Anh.
   - **Tối Ưu Prompt AI (Prompt Enhancer)**: Tự động bổ sung các từ khóa kỹ thuật điện ảnh đỉnh cao (8k cinematic lighting, ray-tracing, volumetric fog, sharp textures).
   - **Bộ điều khiển Chuyển Động Camera (Camera Movement)**: Chọn nhanh các góc quay: *Zoom In (Tiến lại), Zoom Out (Lùi ra), Pan Trái, Pan Phải, Orbit (Quay vòng quanh chủ thể), Flycam FPV Drone, Slow-Mo (Chuyển động siêu chậm), Tilt Up (Ngửa lên)*.
   - **Tùy chỉnh Tỷ lệ & Thời lượng**: 16:9 (Ngang - YouTube, PC), 9:16 (Dọc - TikTok, Facebook Reels, Shorts), 1:1 (Vuông), thời lượng 5s, 6s, 8s, 10s.

2. **🖼️ Tạo Video từ Ảnh (Image-to-Video / First Frame Animation)**:
   - Dùng ảnh có sẵn làm khung hình bắt đầu (First Frame).
   - Hỗ trợ chọn ảnh từ máy tính qua hộp thoại Windows, kéo thả file hoặc dán trực tiếp từ bộ nhớ tạm Clipboard (`Ctrl + V`).
   - Mô tả chuyển động tiếp theo để AI diễn hoạt bức ảnh thành video sống động.

3. **🎨 Tạo Ảnh Mẫu Trước (Text-to-Image Helper)**:
   - Tạo ảnh AI nghệ thuật trước để duyệt concept.
   - Chỉ với 1 click: *"Dùng ảnh này tạo Video ngay"* để chuyển tiếp sang tạo video.

4. **⚡ Bảng Theo Dõi Tiến Trình Render Thời Gian Thực**:
   - Hiển thị thanh % tiến độ (10% ➔ 30% ➔ 60% ➔ 90% ➔ 100%).
   - Cập nhật từng trạng thái: *Đang xếp hàng ➔ Đang kết nối Muse.ai ➔ Đang tổng hợp khung hình AI ➔ Hoàn tất*.
   - Đồng hồ đếm thời gian render thực tế.

5. **🎞️ Trình Phát Video & Thư Viện Media Tích Hợp**:
   - Trình phát video MP4 HD ngay trong giao diện với chế độ lặp vô tận (Loop), tua nhanh chậm, toàn màn hình.
   - Nút **"Lưu Video (.MP4)"**: Mở hộp thoại lưu file chuẩn của Windows.
   - Nút **"Mở Thư Mục Video"**: Mở trực tiếp thư mục `data/media` trong Windows File Explorer.
   - Thư viện lưu trữ lịch sử toàn bộ các tác phẩm đã tạo.

6. **👥 Quản Lý Hồ Tài Khoản Muse.ai & Tự Động Gia Hạn 48h**:
   - Quản lý danh sách nhiều tài khoản để luân phiên (Account Pool).
   - Tự động duy trì phiên đăng nhập 48 giờ (`keepalive`) và đánh thức máy ảo Muse.ai (`VM wake`).

---

## 🚀 2. CÁCH KHỞI CHẠY PHẦN MỀM

Bạn có thể mở phần mềm bằng một trong các cách sau:

### Cách 1: Chạy Bằng File Batch (Khuyên dùng)
- Nhấp đúp chuột vào file:
  ```
  Chay_Phan_Mem.bat
  ```
- Cửa sổ Desktop **MuseAI Video Studio Pro** sẽ tự động mở lên với kích thước chuẩn và nạp sẵn toàn bộ cấu hình.

### Cách 2: Chạy Ẩn Cửa Sổ Đen (Silent Mode)
- Nhấp đúp chuột vào file:
  ```
  Chay_An_Cua_So_Den.vbs
  ```
- Phần mềm sẽ khởi chạy trực tiếp mà không hiển thị cửa sổ dòng lệnh (CMD đen) phía sau, mang lại trải nghiệm phần mềm Desktop chuyên nghiệp.

### Cách 3: Mở Bằng Trình Duyệt Web (Nếu Muốn)
- Khi phần mềm đang chạy, bạn cũng có thể mở trình duyệt Chrome/Edge bất kỳ và truy cập địa chỉ:
  ```
  http://127.0.0.1:18610/
  ```

---

## 🔑 3. HƯỚNG DẪN THÊM TÀI KHOẢN MUSE.AI

Để tạo video, bạn cần ít nhất 1 tài khoản Muse.ai (hoàn toàn miễn phí):

### Bước 1: Đăng ký tài khoản
- Truy cập trang chủ **[https://muse.ai/](https://muse.ai/)** và đăng ký một tài khoản miễn phí bằng email hoặc Google.

### Bước 2: Lấy Cookie của Muse.ai

Có 3 cách đơn giản để lấy Cookie:

#### Cách A: Dùng Tiện Ích Chrome Extension Có Sẵn (Nhanh nhất)
1. Mở trình duyệt Chrome hoặc Edge, truy cập đường dẫn:
   `chrome://extensions`
2. Bật công tắc **Developer mode (Chế độ cho nhà phát triển)** ở góc trên bên phải.
3. Bấm vào nút **Load unpacked (Tải tiện ích đã giải nén)** ở góc trên bên trái.
4. Chọn thư mục `extension` nằm ngay trong thư mục phần mềm `E:\0 - MuseAI\extension`.
5. Đăng nhập vào trang **[muse.ai](https://muse.ai/)** đến giao diện trò chuyện.
6. Bấm vào biểu tượng tiện ích mở rộng ở góc trên trình duyệt, điền:
   - Server: `http://127.0.0.1:18610`
   - Key: giá trị `MUSE2API_KEY` trong file `.env` (hoặc xem ở Studio → Cài đặt)
7. Bấm **"Đọc và Nhập"** -> Tài khoản sẽ được chuyển thẳng vào phần mềm ngay lập tức!

#### Cách B: Lấy Thủ Công Bằng Phím F12
1. Trên trang web **muse.ai** sau khi đã đăng nhập, nhấn phím **F12** trên bàn phím.
2. Chọn tab **Application** (hoặc **Ứng dụng**) -> Ở cột trái chọn **Cookies** -> `https://muse.ai`.
3. Tìm 2 cookie quan trọng nhất là:
   - `hatch_sess`
   - `hatch_vml`
4. Copy giá trị của chúng dưới dạng chuỗi:
   `hatch_sess=giá_trị_ở_đây; hatch_vml=giá_trị_ở_đây`
5. Mở phần mềm -> Vào tab **Tài Khoản Muse** -> Dán vào ô Cookie và bấm **"Thêm Tài Khoản"**.

---

## 💡 4. MẸO TẠO VIDEO AI ĐẸP NHẤT

- **Tỷ lệ 16:9**: Dùng cho video YouTube, phim ngắn, màn hình máy tính.
- **Tỷ lệ 9:16**: Dùng cho TikTok, Reels, YouTube Shorts (AI sẽ tối ưu khung hình dọc không bị viền đen).
- **Viết Prompt kết hợp Camera Motion**: Hãy thử chọn nút chuyển động camera như `Flycam FPV` hoặc `Slow-Mo` để video có nhịp điệu mượt mà như phim chiếu rạp.
- **Tính năng Tạo Video Từ Ảnh**: Nếu muốn tạo video một nhân vật hoặc logo cụ thể, hãy tạo ảnh nhân vật đó trước ở Tab **Tạo Ảnh AI**, sau đó bấm *"Dùng ảnh này tạo Video ngay"*, video tạo ra sẽ giữ nguyên nhân vật và cảnh quan của ảnh đầu tiên!

---

## 🚀 5. CÀI ĐẶT, CẬP NHẬT & ĐẨY CODE GITHUB

1. **`install.bat` - Cài đặt môi trường tự động**:
   - Dùng khi tải dự án về máy tính mới hoặc cài lại môi trường.
   - Tự động nhận diện Python, cập nhật pip, cài đặt toàn bộ thư viện cần thiết (`FastAPI`, `Uvicorn`, `PyWebView`, `PyQt6`, `DrissionPage`...).
   - Tự động tạo thư mục và file cấu hình `.env` ban đầu.

2. **`upload-github.bat` - Đẩy code & Quản lý phiên bản lên GitHub**:
   - Đẩy toàn bộ mã nguồn lên kho [https://github.com/thincole/MuseAI](https://github.com/thincole/MuseAI).
   - Tự động quản lý số phiên bản (khởi đầu mặc định là **1.2.1**, các lần sau tự động tăng `1.2.2`, `1.2.3`... hoặc cho phép nhập tùy ý).
   - Tự động tạo Git Tag tương ứng (`v1.2.1`, `v1.2.2`...) và đẩy đồng bộ lên GitHub.

3. **`update.bat` - Cập nhật phiên bản mới nhất**:
   - Dùng để cập nhật mã nguồn mới nhất từ kho GitHub về máy tính bất cứ lúc nào.
   - Tự động xử lý xung đột tệp cục bộ (`git stash`), cập nhật thư viện `pip` mới (nếu có) và hiển thị số phiên bản mới.

