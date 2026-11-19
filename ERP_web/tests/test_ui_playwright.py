# -*- coding: utf-8 -*-
"""Test UI web ERP bằng trình duyệt thật (Playwright + Edge/Chrome).

Kiểm tra như người dùng thật: mở trang, chuyển 7 tab, bấm E-STOP rồi kiểm tra
mọi nút điều khiển tải bị khoá (kể cả ở trang Thiết bị).

Bỏ qua (skip) nếu máy không có Playwright hoặc trình duyệt Edge/Chrome.
"""
import os
import socket
import sys
import threading
import time

import pytest

ERP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ERP)

sync_playwright = pytest.importorskip("playwright.sync_api",
                                      reason="chưa cài playwright").sync_playwright


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def site():
    """Phục vụ thư mục ERP_web qua HTTP (không cần Firebase → chế độ DEMO)."""
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=ERP, **kw)

        def log_message(self, *a):
            pass

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.5)
    yield f"http://127.0.0.1:{port}/"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = None
        for channel in ("msedge", "chrome"):
            try:
                b = p.chromium.launch(channel=channel)
                break
            except Exception:
                b = None
        if b is None:
            pytest.skip("không có Edge/Chrome cho Playwright")
        yield b
        b.close()


def test_erp_ui_navigation_and_estop_lock(site, browser):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(m.text)
            if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

    # ép chế độ DEMO (không cần Firebase) trước khi app chạy
    page.add_init_script(
        "try{localStorage.setItem('agri_erp_cfg_v1', JSON.stringify("
        "{url:'',secret:'',device:'vuon-1',display:'light'}));}catch(e){}")
    page.goto(site, wait_until="load")
    page.wait_for_timeout(1500)

    assert not errors, f"lỗi console: {errors[:2]}"

    # 7 tab chuyển được và hiện đúng trang
    for tab in ("dashboard", "monitor", "alerts", "devices", "connection",
                "reports", "settings"):
        page.click(f'.nav[data-tab="{tab}"]')
        page.wait_for_timeout(200)
        assert page.eval_on_selector(
            f"#page-{tab}", "el => el.classList.contains('active')"), tab

    # E-STOP phải khoá MỌI nút điều khiển tải (dashboard + trang Thiết bị)
    page.click('.nav[data-tab="dashboard"]')
    page.wait_for_timeout(300)
    page.click("#btnEstop")
    page.wait_for_timeout(1200)

    assert page.eval_on_selector(
        "#estopBanner", "el => !el.classList.contains('hidden')")
    for tid in ("fanToggle", "pumpToggle", "lampToggle"):
        assert page.eval_on_selector(f"#{tid}", "el => el.disabled"), tid
    segs = page.eval_on_selector_all("#modeSeg .seg-btn", "els => els.map(e => e.disabled)")
    assert segs and all(segs), segs

    page.click('.nav[data-tab="devices"]')
    page.wait_for_timeout(400)
    for cid in ("ch1Toggle", "ch2Toggle", "ch3Toggle"):
        assert page.eval_on_selector(f"#{cid}", "el => el.disabled"), cid

    # id HTML phải duy nhất (bug cũ: 2 thẻ cùng id="syncAck")
    assert page.eval_on_selector_all("[id='syncAck']", "e => e.length") == 1
    assert page.eval_on_selector_all("[id='syncAckSettings']", "e => e.length") == 1

    assert not errors, f"lỗi console phát sinh sau thao tác: {errors[:2]}"
    page.close()
