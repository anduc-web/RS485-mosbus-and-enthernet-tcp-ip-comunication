# AGRI MES · PC2 — app Python (PyQt5)

MES (PC2) là **app desktop Python** đọc/ghi hệ thống nhà kính qua
**Modbus TCP** từ PC1. Giao diện 4 phần hệ giống PumpVision MES:

| Trang | Nội dung |
|---|---|
| **Tổng quan** | Biểu đồ nhiệt độ/độ ẩm 6 giờ · tỉ lệ bơm chạy/nghỉ 7 ngày · số đợt quá nhiệt tuần · trạng thái hiện tại |
| **Lịch sử cảnh báo** | Các sự cố MES phát hiện: mất cảm biến, E‑STOP, quá nhiệt, mất kết nối |
| **Quản lý bảo trì** | Thời gian chạy HMI · watchdog bơm/đèn · failsafe · tuổi dữ liệu · ngưỡng quá nhiệt (đặt được) |
| **Công thức vận hành** | Đọc/ghi 6 ngưỡng điều khiển (FC06) · điều khiển bơm/quạt/đèn/E‑STOP (FC05) · xuất tag cho MES |

```
PC2 (app này)  ──Modbus TCP──►  PC1 (HMI, mở cổng 502)
```

**Không cần bridge web, không cần trình duyệt** — MES nói thẳng Modbus TCP
với PC1, giống hệt cách một client Modbus thật.

---

## 1. Chạy

```powershell
cd MES
python mes_app.py --host 192.168.1.50 --port 502
```

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `--host` | `127.0.0.1` | IP của PC1 (lấy từ ô **"Nhập ở PC2"** trên HMI) |
| `--port` | `502` | cổng Modbus TCP của PC1 |
| `--device` | `254` | device id của HMI Bridge |
| `--db` | `mes_history.db` | file SQLite lịch sử |

Trên **PC1** phải: mở app HMI → khung 🌐 → **BẬT CỔNG TCP**, và thêm IP của
PC2 vào ô **IP cho phép** (Modbus TCP không có xác thực).

Trong app có ô **IP:cổng** ở thanh trên cùng → sửa và bấm **Kết nối** để đổi
PC1 mà không cần khởi động lại.

## 2. Cấu trúc

| File | Vai trò |
|---|---|
| `mes_app.py` | App PyQt5: sidebar + topbar + 4 trang, worker đọc/ghi Modbus TCP |
| `mes_contract.py` | Contract tag (bảng thanh ghi) + xuất CSV/JSON |
| `mes_history.py` | Lịch sử SQLite: mẫu thô 8 h + bucket theo phút 30 ngày |
| `mes_stats.py` | Logic thống kê thuần (chuỗi, tỉ lệ, đợt quá nhiệt) |
| `tests/` | 18 test: giao diện, ghi lệnh, E2E với PC1 thật |

## 3. Lưu trữ lịch sử

| Bảng | Nội dung | Giữ |
|---|---|---|
| `samples` | mẫu **thô** (~2 s/lần) | 8 giờ → biểu đồ 6 giờ |
| `rollup` | **bucket theo phút**: nhiệt độ/độ ẩm trung bình & đỉnh, số giây bơm chạy | 30 ngày → thống kê 7 ngày |

Nếu chỉ giữ mẫu thô 30 ngày thì mỗi lần vẽ biểu đồ phải dựng lại
~300.000 dòng → treo UI. Bucket theo phút giữ mọi thứ cần cho thống kê với
~43.000 dòng, và tính **tỉ lệ bơm chạy theo giây** (không theo số mẫu).
`prune()` chạy định kỳ để dọn dữ liệu cũ.

## 4. Bảng thanh ghi (device 254)

Xem `../HMI/modbus_tcp_gateway.py` (phía PC1) — hai bên phải khớp nhau.

| Nhóm | Địa chỉ | Nội dung |
|---|---|---|
| FC04 input | 0–1 | RH×10, T×10 |
| FC04 input | 2–4 | bơm, quạt, đèn |
| FC04 input | 5–7 | mode, E‑STOP, flags |
| FC04 input | 8–9 | uptime (32 bit) |
| FC04 input | 10–11 | data_age_s, seq |
| FC03/FC06 hold | 0–5 | 6 ngưỡng điều khiển |
| FC03/FC06 hold | 6–8 | watchdog bơm/đèn, failsafe |
| FC03/FC06 hold | 9 | mode lệnh |
| FC01/FC05 coil | 0–3 | bơm, quạt, đèn, E‑STOP |

> **Thứ tự ghi ngưỡng:** MES ghi ngưỡng "tắt" trước, "bật" sau (và
> `fan_off` trước `fan_on`) để trạng thái tạm thời không vi phạm ràng buộc
> (bật ≥ tắt) — nếu không HMI sẽ **từ chối** lệnh.

## 5. An toàn

- Mọi lệnh ghi từ MES **không** đi thẳng xuống bus RS485: nó được gửi tới
  PC1 và **HMI áp dụng qua đúng logic an toàn** (E‑STOP, liên động quạt↔đèn,
  watchdog, kiểm tra ràng buộc ngưỡng). Lệnh vi phạm bị HMI từ chối.
- MES hiển thị `data_age_s` + cờ "dữ liệu cũ" để **không ghi record sai**
  khi PC1 mất kết nối.

## 6. Kiểm thử

```powershell
cd MES
python -m pytest tests -q      # 18 test
```
