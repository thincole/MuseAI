"""SSL context dùng chung cho các lệnh tải HTTPS bằng urllib.

urllib kiểm tra chứng chỉ bằng kho chứng chỉ của Windows. Một số máy (Windows ít cập nhật) thiếu chứng chỉ gốc mà
Shopee CDN dùng -> CERTIFICATE_VERIFY_FAILED, không tải được ảnh sản phẩm (09/10: máy 2 vì vậy mà ~1000 video không có
ảnh). requests/curl_cffi vẫn tải được vì có bộ CA riêng -> nạp thêm bộ CA của certifi (đi kèm requests) vào context.
Vẫn kiểm tra chứng chỉ đầy đủ, chỉ là có thêm nguồn chứng chỉ gốc tin cậy.
"""
from __future__ import annotations

import logging
import ssl

log = logging.getLogger("ssl_ctx")


def _build() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception as exc:  # noqa: BLE001
        log.warning("Không nạp được bộ chứng chỉ certifi (%s) -> chỉ dùng kho chứng chỉ Windows", exc)
    return ctx


SSL_CONTEXT = _build()


def https_handler() -> "urllib.request.HTTPSHandler":
    import urllib.request
    return urllib.request.HTTPSHandler(context=SSL_CONTEXT)
