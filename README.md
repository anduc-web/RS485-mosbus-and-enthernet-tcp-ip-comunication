# RS485 Modbus RTU ↔ Modbus TCP/IP — HMI · MES · ERP

Hệ thống giám sát & điều khiển nhà kính 3 lớp, dùng chung bus trường **RS485 / Modbus RTU**
và mở rộng lên **Modbus TCP/IP** cho các lớp phần mềm phía trên.

| Lớp | Thư mục | Công nghệ | Vai trò |
|---|---|---|---|
| **HMI SCADA** | [`HMI/`](HMI/README.md) | Python + PyQt5, pymodbus | Chủ bus RS485 (RTU master), logic AUTO, đồng bộ Firebase, mở cổng Modbus TCP :502 |
| **MES** | [`MES/`](MES/README.md) | Python + PyQt5, SQLite | PC2 đọc/ghi qua Modbus TCP: thống kê, lịch sử cảnh báo, bảo trì, công thức vận hành |
| **ERP Web** | [`ERP_web/`](ERP_web/README.md) | HTML/CSS/JS + Firebase RTDB | Giao diện web: giám sát realtime, điều khiển thiết bị, kho vật tư, mùa vụ, đơn hàng |

## Kiến trúc

```
┌──────────────┐   HTTPS/REST    ┌────────────────────┐
│  ERP Web     │◄───────────────►│  Firebase RTDB     │
└──────────────┘                 └────────────────────┘
                                          ▲ poll 2s
┌─────────────────────────────────────────┴───────────────────────────┐
│ PC1 — HMI SCADA (PyQt5)                                             │
│   ModbusWorker (RTU master) · FirebaseSync · ModbusTcpGateway :502   │
└──────┬──────────────────────────────────────────┬───────────────────┘
       │ USB–RS485 (Modbus RTU 9600 8N1)          │ Modbus TCP :502
┌──────┴──────────────┐                  ┌────────┴─────────────┐
│ SHTC30 (slave 1)    │                  │ PC2 — MES (PyQt5)    │
│ Relay 4 kênh (255)  │                  │ FC01/03/04/05/06     │
└─────────────────────┘                  └──────────────────────┘
```

Bus trường giữ nguyên Modbus RTU (không đổi phần cứng, dây, địa chỉ slave);
chỉ lớp giám sát mở rộng sang Modbus TCP — PC1 đóng vai gateway RTU ↔ TCP
(tương tự Moxa MGate / Advantech).

## Thanh ghi & contract

Contract tag dùng chung giữa HMI và MES nằm trong [`HMI/mes_tag_contract.csv`](HMI/mes_tag_contract.csv)
(`.json` cho bên MES import) và [`MES/mes_contract.py`](MES/mes_contract.py).

## Chạy nhanh

```powershell
# PC1 — HMI (mở cổng TCP :502 cho PC2)
pip install pyserial pymodbus pyqtgraph
python HMI/hmi_shtc30_3kenh.py

# PC2 — MES
python MES/mes_app.py --host 192.168.1.50 --port 502

# ERP web — mở trực tiếp hoặc qua HTTP server
python -m http.server 8000 --directory ERP_web
```

## Test

```powershell
python -m pytest HMI/tests MES/tests -q
```

Chi tiết từng lớp xem README trong thư mục tương ứng.
