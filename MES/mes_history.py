# -*- coding: utf-8 -*-
"""Lịch sử telemetry của MES (SQLite).

Hai bảng, tách theo mức chi tiết:
  • ``samples`` — mẫu THÔ (~2 s/lần), giữ 8 giờ  → dựng biểu đồ 6 giờ.
  • ``rollup``  — bucket THEO PHÚT (nhiệt độ/độ ẩm trung bình & đỉnh,
    số giây bơm chạy), giữ 30 ngày → thống kê 7 ngày.

Nếu chỉ giữ mẫu thô 30 ngày thì mỗi lần vẽ biểu đồ phải dựng lại
~300.000 dòng → treo UI. Bucket theo phút giữ mọi thứ cần cho thống kê với
~43.000 dòng, và tính **tỉ lệ bơm chạy theo giây** (không theo số mẫu).
"""

import os
import sqlite3
import threading
import time

#: Giữ mẫu thô bao lâu (giây) — đủ cho biểu đồ 6 giờ + biên an toàn.
RAW_KEEP_S = 8 * 3600
#: Giữ bucket bao lâu (ngày).
KEEP_DAYS = 30
#: Chu kỳ bucket (giây).
BUCKET_S = 60
#: Số mẫu thô tối đa cho 1 bucket (để chặn số liệu rác).
MAX_N_PER_BUCKET = 600


class History:
    """Lịch sử telemetry lưu SQLite (thread-safe)."""

    def __init__(self, path=None):
        self.path = path or os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "mes_history.db")
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                temp REAL, rh REAL,
                pump INTEGER, fan INTEGER, lamp INTEGER,
                mode TEXT, estop INTEGER, sensor_ok INTEGER, flags INTEGER
            )""")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS rollup (
                ts INTEGER PRIMARY KEY,
                n INTEGER NOT NULL,
                temp_sum REAL, temp_max REAL,
                rh_sum REAL, rh_max REAL,
                pump_s REAL, dt_total REAL
            )""")
        self._conn.commit()
        row = self._conn.execute("SELECT MAX(ts) FROM samples").fetchone()
        self._last_ts = int(row[0]) if row and row[0] else None

    # ------------------------------------------------------------------
    def add(self, s):
        """Ghi 1 mẫu mới (mẫu thô + cập nhật bucket theo phút)."""
        ts = int(s["ts"])
        temp = s.get("temp")
        rh = s.get("rh")
        pump = 1 if s.get("pump") else 0
        bucket = ts - (ts % BUCKET_S)
        with self._lock:
            if self._last_ts is not None and ts > self._last_ts:
                # khoảng cách THỰC giữa 2 mẫu (không dùng hằng số chu kỳ:
                # khi thiết bị dừng lâu rồi kết nối lại, không được gán
                # hàng chục ngày "bơm chạy" vào 1 bucket).
                dt = float(min(ts - self._last_ts, BUCKET_S * 2))
            else:
                dt = 2.0
            self._last_ts = ts
            self._conn.execute(
                "INSERT INTO samples (ts,temp,rh,pump,fan,lamp,mode,estop,"
                "sensor_ok,flags) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, temp, rh, pump,
                 1 if s.get("fan") else 0, 1 if s.get("lamp") else 0,
                 s.get("mode", "AUTO"), 1 if s.get("estop") else 0,
                 1 if s.get("sensor_ok", True) else 0,
                 int(s.get("flags", 0))))
            self._conn.execute(
                "INSERT INTO rollup (ts,n,temp_sum,temp_max,rh_sum,rh_max,"
                "pump_s,dt_total) VALUES (?,1,?,?,?,?,?,?) "
                "ON CONFLICT(ts) DO UPDATE SET "
                "n=n+1, "
                "temp_sum=COALESCE(temp_sum,0)+COALESCE(excluded.temp_sum,0), "
                "temp_max=MAX(COALESCE(temp_max,-1e9), "
                "             COALESCE(excluded.temp_max,-1e9)), "
                "rh_sum=COALESCE(rh_sum,0)+COALESCE(excluded.rh_sum,0), "
                "rh_max=MAX(COALESCE(rh_max,-1e9), "
                "           COALESCE(excluded.rh_max,-1e9)), "
                "pump_s=COALESCE(pump_s,0)+COALESCE(excluded.pump_s,0), "
                "dt_total=COALESCE(dt_total,0)+COALESCE(excluded.dt_total,0)",
                (bucket, temp, temp, rh, rh,
                 dt if pump else 0.0, dt))
            self._conn.commit()

    # ------------------------------------------------------------------
    def raw(self, hours=6):
        """Mẫu thô trong `hours` giờ gần nhất (cho biểu đồ)."""
        cutoff = int(time.time()) - hours * 3600
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,temp,rh,pump,fan,lamp,mode,estop,sensor_ok,flags "
                "FROM samples WHERE ts>=? ORDER BY ts", (cutoff,)).fetchall()
        return [{"ts": r[0], "temp": r[1], "rh": r[2], "pump": bool(r[3]),
                 "fan": bool(r[4]), "lamp": bool(r[5]), "mode": r[6],
                 "estop": bool(r[7]), "sensor_ok": bool(r[8]),
                 "flags": r[9]} for r in rows]

    def buckets(self, days=7):
        """Bucket theo phút trong `days` ngày (cho thống kê dài hạn).

        Trả về mẫu có cùng khoá với mẫu thô để `mes_stats` xử lý chung:
        ``dt`` = số giây bucket phủ, ``pump_seconds`` = số giây bơm chạy.
        """
        cutoff = int(time.time()) - days * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,n,temp_sum,temp_max,rh_sum,rh_max,pump_s,dt_total "
                "FROM rollup WHERE ts>=? ORDER BY ts", (cutoff,)).fetchall()
        out = []
        for ts, n, tsum, tmax, rsum, rmax, pump_s, dt in rows:
            n = max(1, int(n or 1))
            out.append({
                "ts": int(ts),
                "dt": float(dt or 0.0),
                "temp": (tsum / n) if tsum is not None else None,
                "temp_max": tmax,
                "rh": (rsum / n) if rsum is not None else None,
                "rh_max": rmax,
                "pump": (pump_s or 0) > (dt or 0) / 2.0,
                "pump_seconds": float(pump_s or 0.0),
            })
        return out

    # ------------------------------------------------------------------
    def prune(self):
        """Dọn dữ liệu cũ (mẫu thô + bucket) và rút gọn DB."""
        now = int(time.time())
        raw_cut = now - RAW_KEEP_S
        roll_cut = now - KEEP_DAYS * 86400
        with self._lock:
            self._conn.execute("DELETE FROM samples WHERE ts<?", (raw_cut,))
            self._conn.execute("DELETE FROM rollup WHERE ts<?", (roll_cut,))
            self._conn.commit()
            try:
                self._conn.execute("VACUUM")
            except sqlite3.OperationalError:
                pass          # VACUUM không chạy được trong transaction
            self._conn.commit()

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
