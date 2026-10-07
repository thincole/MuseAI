# Tiện Ích Mở Rộng Nhập Cookie MuseAI (Chrome / Edge Extension)
=============================================================

Tiện ích này giúp bạn đồng bộ toàn bộ Cookie phiên đăng nhập từ **muse.ai** vào ứng dụng **MuseAI Video Studio** chỉ với 1 click chuột, tự động trích xuất các Cookie bảo mật (HttpOnly) mà không cần thao tác F12 thủ công.

---

## I. Hướng Dẫn Cài Đặt Vào Trình Duyệt

### Dành cho Google Chrome:
1. Mở Chrome, nhập `chrome://extensions` vào thanh địa chỉ rồi nhấn Enter.
2. Bật công tắc **"Chế độ dành cho nhà phát triển" (Developer mode)** ở góc trên bên phải.
3. Nhấn vào nút **"Tải tiện ích đã giải nén" (Load unpacked)** ở góc trên bên trái.
4. Chọn đúng thư mục `E:\0 - MuseAI\extension`.
5. Biểu tượng tiện ích sẽ xuất hiện trên thanh công cụ của trình duyệt (bấm vào biểu tượng mảnh ghép để ghim tiện ích ra ngoài).

### Dành cho Microsoft Edge:
1. Mở Edge, nhập `edge://extensions` vào thanh địa chỉ rồi nhấn Enter.
2. Bật công tắc **"Chế độ dành cho nhà phát triển" (Developer mode)** ở góc dưới bên trái.
3. Nhấn vào nút **"Tải phần mở rộng đã giải nén" (Load unpacked)** ở phía trên.
4. Chọn thư mục `E:\0 - MuseAI\extension`.

*(Các trình duyệt dùng nhân Chromium khác như Cốc Cốc, Brave, Vivaldi... thao tác hoàn toàn tương tự).*

---

## II. Cách Sử Dụng Trong 1 Phút

1. Mở tab mới trên trình duyệt này, truy cập [https://muse.ai/](https://muse.ai/) và **đăng nhập vào tài khoản của bạn** (đảm bảo nhìn thấy giao diện tạo video/ảnh).
2. Mở ứng dụng **MuseAI Video Studio** trên máy tính (chạy file `Chay_Phan_Mem.bat`).
3. Bấm vào biểu tượng tiện ích **Nhập Cookie MuseAI** trên thanh công cụ trình duyệt:
   - **Địa chỉ máy chủ (Base URL)**: `http://127.0.0.1:18610` (đã được điền sẵn tự động).
   - **API Key**: Điền API Key của bạn (có sẵn trong tab *Cài Đặt* của phần mềm MuseAI Studio).
   - **Tên tài khoản**: Đặt tên ghi nhớ (ví dụ: *Tài khoản 1*).
4. Nhấn nút **🚀 ĐỌC COOKIE VÀ NHẬP VÀO STUDIO**.
5. Khi thấy thông báo `✓ ĐỒNG BỘ THÀNH CÔNG!`, tài khoản đã được nạp ngay lập tức vào phần mềm MuseAI Studio để tạo video!

---

## III. Câu Hỏi Thường Gặp (FAQ)

- **Q: Báo lỗi "Không tìm thấy Cookie của muse.ai"?**
  - **A**: Nghĩa là bạn chưa đăng nhập tài khoản trên trang web muse.ai trong trình duyệt này. Hãy mở [https://muse.ai/](https://muse.ai/) và đăng nhập trước.
- **Q: Báo lỗi "Thiếu các mục cốt lõi"?**
  - **A**: Phiên đăng nhập trên web chưa xong hoặc bị gián đoạn. Hãy tải lại trang muse.ai để chắc chắn đã vào màn hình chính rồi nhấn lại.
- **Q: Báo lỗi 401?**
  - **A**: API Key chưa chính xác. Bạn hãy vào tab *Cài Đặt* trong phần mềm MuseAI Studio để sao chép chuẩn API Key (dạng `m2a_...`).
- **Q: Tiện ích có làm lộ thông tin tài khoản không?**
  - **A**: Hoàn toàn không. Tiện ích chỉ gửi cookie trực tiếp về máy tính cá nhân của bạn qua cổng nội bộ `127.0.0.1:18610`. Mã nguồn hoàn toàn mở trong file `popup.js`.
