# -*- coding: utf-8 -*-
"""Đồng bộ HMI <-> Firebase Realtime Database (REST, không cần SDK).

Thread nền, cách ly hoàn toàn với vòng lặp Modbus. Giao thức đầy đủ dùng
chung với web ERP (xem ERP_HMI/README.md):

    hmi/<device>/live          HMI ghi ~2 s/lần
        { humidity, temp, pump, fan, lamp, mode, estop, sensor_ok,
          modbus_connected, rh_on, rh_off, fan_on, fan_off, heat_on,
          heat_off, pump_max_s, heat_max_s, failsafe_cycles,
          uptime_s, ts }

    hmi/<device>/status        HMI ghi ~2 s/lần (heartbeat)
        { online, ts, version, device, mode, estop, modbus_connected,
          sensor_ok, uptime_s, latency_ms, interval_s }

    hmi/<device>/control       ERP ghi, HMI đọc
        { pump?, fan?, lamp?, mode?, estop?, by, ts, seq }

    hmi/<device>/command_ack   HMI ghi sau khi thực thi lệnh
        { seq, by, ok, message, applied{...}, ts }

    hmi/<device>/config        ERP ghi, HMI đọc
        { rh_on, rh_off, fan_on, fan_off, heat_on, heat_off,
          pump_max_s, heat_max_s, failsafe_cycles, by, ts, seq }

    hmi/<device>/config_ack    HMI ghi sau khi áp dụng ngưỡng
        { seq, ok, message, applied{...}, ts }

    hmi/<device>/logs/<id>     HMI POST { level, msg, ts }
    hmi/<device>/history/<id>  HMI POST mẫu telemetry ~20 s/lần

    hmi/<device>/modbus_trace/<id>  HMI POST khung Modbus RTU/ASCII
        { ts, dir, slave, fc, fc_name, rtu, ascii, crc16, lrc, ok, note }

Chỉ dùng thư viện chuẩn (urllib) để không phải pip install gì thêm.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

VERSION = "hmi-2.0"


class FirebaseSync(threading.Thread):
    """Thread nền: đẩy dữ liệu lên Firebase và nhận lệnh điều khiển về."""

    def __init__(self, on_control=None, on_config=None, on_status=None,
                 last_control_ts=0, last_config_ts=0, device="vuon-1"):
        super().__init__(daemon=True)
        self._url = ""
        self._auth = ""
        self._device = device or "vuon-1"
        self._enabled = False
        self._running = False
        self._stop_evt = threading.Event()

        self._lock = threading.Lock()
        self._logq = deque(maxlen=200)
        self._ackq = deque(maxlen=40)
        self._live = None
        self._last_status = None
        self._last_ctrl_seq = None
        self._last_cfg_seq = None
        self._last_ctrl_ts = int(last_control_ts or 0)
        self._last_cfg_ts = int(last_config_ts or 0)
        self._last_hist_ts = 0.0
        #: Hàng đợi khung Modbus chờ đẩy lên ERP.
        self._traceq = deque(maxlen=200)
        #: Khoá các bản ghi đã gửi (ts, dir, fc, rtu) để không gửi trùng.
        self._trace_sent = {}
        self._trace_posts = 0

        self.on_control = on_control        # callback(dict) khi web gửi lệnh
        self.on_config = on_config          # callback(dict) khi web gửi ngưỡng
        self.on_status = on_status          # callback(text, ok)
        self.device = self._device

        self.interval = 2.0
        self.history_interval = 20.0
        self.history_max = 900
        self.stale_control_s = 300
        #: Giữ tối đa bao nhiêu khung Modbus trên Firebase.
        self.trace_max = 300

        self.latency_ms = None
        self.last_ok_ts = 0.0
        self.online = False

    # ------------------------------------------------------------------
    def configure(self, db_url, auth="", device="vuon-1", enabled=True,
                  interval=2.0, history_interval=20.0, history_max=900,
                  stale_control_s=300):
        with self._lock:
            self._url = (db_url or "").strip().rstrip("/")
            self._auth = (auth or "").strip()
            self._device = (device or "vuon-1").strip() or "vuon-1"
            self.device = self._device
            self._enabled = bool(enabled) and bool(self._url)
        self.interval = max(0.5, float(interval or 2.0))
        self.history_interval = max(5.0, float(history_interval or 20.0))
        self.history_max = max(60, int(history_max or 900))
        self.stale_control_s = max(10, int(stale_control_s or 300))
        self._last_status = None
        with self._lock:
            # Cấu hình mới = phiên mới → cho phép gửi lại các khung đã có.
            self._trace_sent.clear()
        self._emit_status("đã cấu hình" if self._enabled else "tắt", False)

    def stop(self):
        self._running = False
        self._stop_evt.set()

    @property
    def base(self):
        return f"hmi/{self._device}"

    # ------------------------------------------------------------------
    # Dữ liệu do HMI đẩy lên
    # ------------------------------------------------------------------
    def push_live(self, payload):
        """Cập nhật ảnh chụp realtime; thread sẽ PUT lên Firebase mỗi chu kỳ."""
        with self._lock:
            self._live = dict(payload or {})
            self._live["ts"] = int(time.time() * 1000)

    def push_log(self, level, msg):
        if level not in ("INFO", "OK", "WARN", "ERROR", "REMOTE"):
            return
        item = {
            "level": str(level)[:8],
            "msg": str(msg)[:400],
            "ts": int(time.time() * 1000),
        }
        with self._lock:
            self._logq.append(item)

    def push_command_ack(self, seq, ok, message, applied=None, by="hmi"):
        """HMI áp dụng lệnh xong → web đọc và đối chiếu seq."""
        item = {
            "seq": seq,
            "by": str(by)[:40],
            "ok": bool(ok),
            "message": str(message)[:300],
            "applied": applied or {},
            "ts": int(time.time() * 1000),
        }
        with self._lock:
            self._ackq.append(("command_ack", item))

    def push_config_ack(self, seq, ok, message, applied=None, by="hmi"):
        item = {
            "seq": seq,
            "by": str(by)[:40],
            "ok": bool(ok),
            "message": str(message)[:300],
            "applied": applied or {},
            "ts": int(time.time() * 1000),
        }
        with self._lock:
            self._ackq.append(("config_ack", item))

    def push_trace(self, records):
        """Đẩy khung Modbus (RTU + ASCII) mới lên ERP.

        Nhận TOÀN BỘ buffer trace của HMI (vòng lặp gọi mỗi chu kỳ 2 s)
        nên phải bỏ qua bản ghi đã gửi. Khoá so khớp theo
        (ts, hướng, mã hàm, khung RTU) để TX và RX cùng mili-giây vẫn giữ
        cả hai.
        """
        items = []
        with self._lock:
            for r in records or []:
                ts = int(r.get("ts") or 0)
                if not ts:
                    continue
                key = (ts, str(r.get("dir") or ""),
                       int(r.get("fc") or 0), str(r.get("rtu") or ""))
                if key in self._trace_sent:
                    continue
                self._trace_sent[key] = True
                items.append({
                    "ts": ts,
                    "dir": str(r.get("dir") or "")[:4],
                    "slave": int(r.get("slave") or 0),
                    "fc": int(r.get("fc") or 0),
                    "fc_name": str(r.get("fc_name") or "")[:48],
                    "rtu": str(r.get("rtu") or "")[:120],
                    "ascii": str(r.get("ascii") or "")[:120],
                    "crc16": str(r.get("crc16") or "")[:8],
                    "lrc": str(r.get("lrc") or "")[:8],
                    "ok": bool(r.get("ok", True)),
                    "note": str(r.get("note") or "")[:160],
                })
            if len(self._trace_sent) > 4000:
                for k in list(self._trace_sent)[:2000]:
                    self._trace_sent.pop(k, None)
            self._traceq.extend(items)

    def note_control_ts(self, ts):
        self._last_ctrl_ts = max(self._last_ctrl_ts, int(ts or 0))

    def note_config_ts(self, ts):
        self._last_cfg_ts = max(self._last_cfg_ts, int(ts or 0))

    # ------------------------------------------------------------------
    # HTTP thô
    # ------------------------------------------------------------------
    def _endpoint(self, path, query=None):
        url = f"{self._url}/{path}.json"
        params = dict(query or {})
        if self._auth:
            params["auth"] = self._auth
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _request(self, path, method="GET", payload=None, timeout=5, query=None):
        data = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self._endpoint(path, query), data=data,
                                     headers=headers, method=method)
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
        finally:
            self.latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return json.loads(body) if body.strip() else None

    def _emit_status(self, text, ok):
        self.online = bool(ok)
        if text != self._last_status:
            self._last_status = text
            if self.on_status:
                try:
                    self.on_status(text, bool(ok))
                except Exception:
                    pass

    # ------------------------------------------------------------------
    def _send_status(self):
        with self._lock:
            live = dict(self._live or {})
        payload = {
            "online": True,
            "ts": int(time.time() * 1000),
            "version": VERSION,
            "device": self._device,
            "mode": live.get("mode", "AUTO"),
            "estop": bool(live.get("estop", False)),
            "modbus_connected": bool(live.get("modbus_connected", False)),
            "sensor_ok": bool(live.get("sensor_ok", False)),
            "uptime_s": int(live.get("uptime_s", 0) or 0),
            "latency_ms": self.latency_ms if self.latency_ms is not None else 0,
            "interval_s": self.interval,
        }
        self._request(f"{self.base}/status", "PUT", payload)

    def _flush_acks(self):
        with self._lock:
            items = list(self._ackq)
            self._ackq.clear()
        for idx, (path, payload) in enumerate(items):
            try:
                self._request(f"{self.base}/{path}", "PUT", payload)
            except Exception:
                with self._lock:
                    # Đẩy lại TOÀN BỘ phần chưa gửi, đúng thứ tự. Nếu chỉ
                    # đẩy lại item đang lỗi thì các item sau đã bị xoá khỏi
                    # hàng đợi và mất vĩnh viễn (mất command_ack/ngưỡng).
                    self._ackq.extendleft(reversed(items[idx:]))
                raise

    def _flush_logs(self):
        with self._lock:
            items = list(self._logq)
            self._logq.clear()
        for idx, item in enumerate(items):
            try:
                self._request(f"{self.base}/logs", "POST", item, timeout=4)
            except Exception:
                with self._lock:
                    # Giữ lại toàn bộ log chưa gửi (xem _flush_acks).
                    self._logq.extendleft(reversed(items[idx:]))
                raise

    def _flush_trace(self):
        """Đẩy hết khung Modbus đang chờ lên nhánh modbus_trace."""
        with self._lock:
            items = list(self._traceq)
            self._traceq.clear()
        for idx, item in enumerate(items):
            try:
                self._request(f"{self.base}/modbus_trace", "POST", item,
                              timeout=4)
            except Exception:
                with self._lock:
                    # Giữ toàn bộ khung chưa gửi (xem _flush_acks).
                    self._traceq.extendleft(reversed(items[idx:]))
                raise
        self._trace_posts += len(items)
        if items and self._trace_posts % 25 == 0:
            self._prune_trace()

    def _prune_trace(self):
        """Xoá khung cũ, giữ tối đa `trace_max` khung mới nhất."""
        try:
            keys = self._request(f"{self.base}/modbus_trace", "GET",
                                 query={"shallow": "true"}, timeout=5) or {}
            old = sorted(keys.keys())
            excess = len(old) - self.trace_max
            for key in old[:max(0, min(40, excess))]:
                self._request(f"{self.base}/modbus_trace/{key}", "DELETE",
                              timeout=4)
        except Exception:
            pass

    def _push_history(self, live):
        if not live:
            return
        now = time.time()
        if now - self._last_hist_ts < self.history_interval:
            return
        item = {
            "humidity": live.get("humidity"),
            "temp": live.get("temp"),
            "pump": bool(live.get("pump", False)),
            "fan": bool(live.get("fan", False)),
            "lamp": bool(live.get("lamp", False)),
            "mode": live.get("mode", "AUTO"),
            "estop": bool(live.get("estop", False)),
            "sensor_ok": bool(live.get("sensor_ok", False)),
            "ts": int(live.get("ts") or now * 1000),
        }
        self._request(f"{self.base}/history", "POST", item, timeout=4)
        self._last_hist_ts = now
        # Dọn bớt key cũ, mỗi lần tối đa 40 key để không chặn vòng lặp.
        try:
            keys = self._request(f"{self.base}/history", "GET",
                                 query={"shallow": "true"}, timeout=5) or {}
            old = sorted(keys.keys())
            excess = len(old) - self.history_max
            for key in old[:max(0, min(40, excess))]:
                self._request(f"{self.base}/history/{key}", "DELETE", timeout=4)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Nhận lệnh / cấu hình từ ERP
    # ------------------------------------------------------------------
    def _poll_control(self):
        ctl = self._request(f"{self.base}/control", "GET", timeout=4) or {}
        seq = ctl.get("seq")
        if seq is None:
            seq = ctl.get("ts")
        if seq is None or seq == self._last_ctrl_seq:
            return
        ts = int(ctl.get("ts") or 0)
        now_ms = int(time.time() * 1000)
        if ts and ts <= self._last_ctrl_ts:
            self._last_ctrl_seq = seq
            return
        # Lệnh quá cũ (HMI tắt lâu) không thực thi để tránh replay nguy hiểm.
        if ts and now_ms - ts > self.stale_control_s * 1000:
            self._last_ctrl_seq = seq
            self.push_log("WARN", f"Bỏ qua lệnh web quá cũ ({(now_ms - ts) // 1000}s)")
            return
        self._last_ctrl_seq = seq
        if self.on_control:
            try:
                self.on_control(dict(ctl))
            except Exception as e:
                self.push_log("ERROR", f"Lỗi xử lý lệnh web: {type(e).__name__}: {e}")

    def _poll_config(self):
        cfg = self._request(f"{self.base}/config", "GET", timeout=4) or {}
        seq = cfg.get("seq")
        if seq is None:
            seq = cfg.get("ts")
        if seq is None or seq == self._last_cfg_seq:
            return
        ts = int(cfg.get("ts") or 0)
        if ts and ts <= self._last_cfg_ts:
            self._last_cfg_seq = seq
            return
        if ts and int(time.time() * 1000) - ts > 7 * 86400 * 1000:
            self._last_cfg_seq = seq
            return
        self._last_cfg_seq = seq
        if self.on_config:
            try:
                self.on_config(dict(cfg))
            except Exception as e:
                self.push_log("ERROR", f"Lỗi áp dụng cấu hình: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    def run(self):
        self._running = True
        if not self._enabled:
            return
        self._emit_status("đang kết nối…", False)
        backoff = 0.0

        while self._running:
            try:
                with self._lock:
                    live = dict(self._live or {}) if self._live else None

                self._send_status()
                if live is not None:
                    self._request(f"{self.base}/live", "PUT", live)
                    self._push_history(live)
                self._flush_acks()
                self._flush_logs()
                self._flush_trace()

                self._poll_control()
                self._poll_config()

                self.last_ok_ts = time.time()
                backoff = 0.0
                self._emit_status("trực tuyến", True)
            except urllib.error.HTTPError as e:
                self._emit_status(
                    f"lỗi Firebase {e.code} (kiểm tra URL / rules)", False)
                backoff = min(10.0, backoff + 1.0) if backoff else 2.0
            except urllib.error.URLError:
                self._emit_status("lỗi mạng: không tới được Firebase", False)
                backoff = min(10.0, backoff + 1.0) if backoff else 2.0
            except Exception as e:
                self._emit_status(f"lỗi: {type(e).__name__}", False)
                backoff = min(10.0, backoff + 1.0) if backoff else 2.0

            delay = self.interval + backoff
            self._stop_evt.wait(delay)
            self._stop_evt.clear()

        try:
            self._request(f"{self.base}/status", "PUT", {
                "online": False,
                "ts": int(time.time() * 1000),
                "version": VERSION,
                "device": self._device,
            }, timeout=3)
        except Exception:
            pass
        self._emit_status("đã ngắt", False)


# ----------------------------------------------------------------------
# Chạy độc lập để kiểm tra cấu hình:  python firebase_sync.py <db_url> [auth]
if __name__ == "__main__":
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else ""
    auth = sys.argv[2] if len(sys.argv) > 2 else ""
    if not url:
        print("Cách dùng: python firebase_sync.py "
              "https://<project>-default-rtdb.firebaseio.com [auth]")
        sys.exit(1)

    seen = []

    def _status(text, ok):
        seen.append(text)
        print("[firebase]", "OK" if ok else "--", text)

    fb = FirebaseSync(on_status=_status)
    fb.configure(url, auth, "vuon-1", True, interval=1.0,
                 history_interval=5.0)
    fb.push_live({
        "humidity": 42.5, "temp": 28.3,
        "pump": False, "fan": True, "lamp": False,
        "mode": "MANUAL", "estop": False, "sensor_ok": True,
        "modbus_connected": True,
        "rh_on": 40, "rh_off": 45, "fan_on": 32, "fan_off": 30,
        "heat_on": 22, "heat_off": 25,
        "pump_max_s": 600, "heat_max_s": 1800, "failsafe_cycles": 1,
        "uptime_s": 12,
    })
    fb.push_log("OK", "Kiểm tra kết nối từ dòng lệnh")
    fb.start()
    time.sleep(8)
    fb.stop()
    fb.join(3)
    if fb.last_ok_ts:
        print("Đã ghi thử lên Firebase. Mở Console → Realtime Database để xem.")
    else:
        print("CHƯA ghi được lên Firebase. Xem lỗi ở trên:")
        print("  - 401/403  → mở Rules cho nhánh hmi/ và erp/ (xem README.md)")
        print("  - mạng/URL → kiểm tra lại databaseURL của project")
