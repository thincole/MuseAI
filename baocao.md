# Báo cáo log MuseAI: nguyên nhân không tạo được video AI

- **Khoảng thời gian:** 09/10/2026, 00:15 → 03:00 (165 phút)
- **Nguồn dữ liệu:** `root/log.txt` (log phiên chạy bắt đầu 00:12)
- **Phiên bản phần mềm:** v1.2.10 (đã có chẩn đoán `[Stuck Debug]`, cửa sổ 1440x900, ngôn ngữ en-US)

## 1. Kết luận

**Phần lớn lần tạo video thất bại vì máy ảo (VM) của tài khoản Muse không kết nối được.** Prompt và ảnh tham chiếu vẫn được gửi đúng. Muse không bao giờ chuyển sang giai đoạn xử lý, và phần mềm chờ 150 giây rồi đổi tài khoản.

Lỗi nằm ở phía Muse, không phải ở bộ chọn nút hay cách gửi prompt của phần mềm.

## 2. Số liệu tổng

| Kết quả | Số lần | Tỉ lệ trên tổng lần thử |
|---|---:|---:|
| Clip tạo thành công | 87 | 9,5% |
| Kẹt `/thread/new` (VM "Connecting...") | 808 | 88,0% |
| Khởi tạo trang muse.ai thất bại / timeout | 14 | 1,5% |
| Ảnh tham chiếu không đính kèm sau 15s | 4 | 0,4% |
| Mô hình chỉ trả lời bằng văn bản, không sinh video | 3 | 0,3% |
| Chờ sinh video quá hạn | 2 | 0,2% |
| **Tổng lần thử** | **918** | 100% |

- Tốc độ trung bình: khoảng 31–32 clip thành công mỗi giờ (87 clip trong 165 phút).
- Mỗi sản phẩm lưu thành công là một sản phẩm riêng (87 clip, 87 sản phẩm khác nhau, không bị lặp).

## 3. Diễn biến theo thời gian

| Khung giờ | Thành công | Kẹt | Khởi tạo timeout | Ảnh không đính kèm | Chỉ trả lời văn bản | Chờ quá hạn |
|---|---:|---:|---:|---:|---:|---:|
| 00:15–00:44 | 13 | 149 | 2 | 2 | 0 | 0 |
| 00:45–01:14 | 17 | 143 | 3 | 0 | 0 | 0 |
| 01:15–01:44 | 21 | 144 | 2 | 0 | 1 | 1 |
| 01:45–02:14 | 14 | 147 | 3 | 0 | 1 | 1 |
| 02:15–02:44 | 15 | 147 | 2 | 1 | 0 | 0 |
| 02:45–03:00 (16 phút) | 7 | 78 | 2 | 1 | 1 | 0 |

Tỉ lệ kẹt gần như không đổi theo thời gian (khoảng 143–149 lần mỗi 30 phút). Đây là tình trạng ổn định, không phải đang xấu đi hay đang hồi phục.

## 4. Bằng chứng từ dòng `[Stuck Debug]`

Có 808 dòng chẩn đoán, và **cả 808 dòng đều có cùng trạng thái**:

| Tiêu chí | Kết quả |
|---|---|
| Trang có bong bóng tin nhắn người dùng (`user_bubbles` ≥ 1) | 808 / 808 |
| Ô nhập đã trống (`textarea_len` = 0), tức tin nhắn đã gửi | 808 / 808 |
| Giao diện hiện "Connecting..." | 808 / 808 |
| Bong bóng tin nhắn ghi "Still sending" | 808 / 808 |
| Ngôn ngữ giao diện (`lang`) | `en-US` (808) |
| Kích thước trang (`viewport`) | `1424x805` (808) |

Ảnh chụp màn hình khi kẹt (lưu trong `root/data/stuck_debug/`) cho thấy:
- Tin nhắn gồm ảnh sản phẩm và prompt đã hiện trong khung chat.
- Dưới tin nhắn là chữ **"Still sending"**.
- Khung bên phải là **"Connecting..."** kèm vòng xoay, tức VM của tài khoản chưa kết nối.

Có 60 tài khoản khác nhau bị kẹt, mỗi tài khoản khoảng 14 lần. Vì vậy đây không phải lỗi của một vài tài khoản riêng lẻ.

## 5. Các lỗi phụ (hiếm)

| Lỗi | Số lần | Ghi chú |
|---|---:|---|
| Khởi tạo trang muse.ai thất bại / timeout | 14 | Xuất hiện đều ở mọi khung giờ, có thể do trang tải chậm khi nhiều phiên cùng chạy |
| Ảnh tham chiếu không đính kèm sau 15s | 4 | Phần mềm dừng trước khi gửi prompt, rồi đổi tài khoản |
| Mô hình chỉ trả lời bằng văn bản | 3 | Có lần agent báo công cụ video tự gặp lỗi (dùng sai đường dẫn ảnh tham chiếu). Lỗi nằm ở phía Muse |
| Chờ sinh video quá hạn | 2 | Muse không trả kết quả mới trong thời gian chờ |

## 6. Những gì đã loại trừ

| Giả thuyết | Kết luận |
|---|---|
| Sai bộ chọn nút gửi / giao diện đổi ngôn ngữ | Loại trừ: tin nhắn đã gửi thành công và giao diện đang ở `en-US` |
| Cửa sổ trình duyệt quá nhỏ làm đổi bố cục | Loại trừ: viewport `1424x805`, tin nhắn vẫn gửi bình thường |
| Chưa cập nhật các sửa lỗi của repo gốc `czg86389-hub/muse2api` | Loại trừ: mọi sửa lỗi UI và logic đánh thức VM đã có sẵn trong `engine.py` |
| Lỗi ghép Outro 12s | Loại trừ: bước ghép chạy bình thường (video đầu ra đúng 12.00s) |

## 7. Ước tính mức lãng phí

- Mỗi lần kẹt tốn tối thiểu 150 giây trước khi đổi tài khoản.
- 808 lần kẹt tương đương khoảng 33,7 giờ thời gian chờ trong 2,75 giờ thực tế. Điều này gợi ý khoảng 12 luồng chạy song song (đây là ước tính từ số liệu log, chưa kiểm chứng trực tiếp).

## 8. Đề xuất

1. **Rút ngưỡng chờ từ 150 giây xuống 60–90 giây** để thử được nhiều tài khoản hơn trong cùng thời gian.
   - Cần đo trước thời gian từ lúc gửi prompt đến lúc agent trả lời của các clip thành công, để không cắt oan clip chậm.
2. **Giảm số luồng chạy song song.** Nhiều tài khoản cùng dựng VM từ một IP trực tiếp có thể là nguyên nhân. Cần thử để kiểm chứng.
3. **Thử proxy riêng cho từng tài khoản** (chỉ nếu hai cách trên không đủ). Nên dùng proxy dân cư chất lượng tốt, mỗi tài khoản một IP.

## 9. Hạn chế của báo cáo

- Báo cáo mô tả triệu chứng và bằng chứng từ log. Nguyên nhân gốc (vì sao VM của Muse không kết nối) nằm ở phía Muse nên không thể xác định từ log này.
- Hai đề xuất đầu là các hướng thử, chưa được kiểm chứng bằng thực nghiệm.
