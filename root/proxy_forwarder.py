"""Tiến trình forwarder proxy riêng cho 1 HomeProxy (chạy ngoài app chính).

Chrome không tự đăng nhập được proxy có user/pass, nên mỗi proxy có 1 forwarder trên 127.0.0.1 tự chèn
Proxy-Authorization. Trước đây forwarder chạy bằng luồng Python ngay trong app (2 luồng/kết nối, ~400+ luồng
khi chạy 30 trình duyệt) -> giành GIL với các luồng điều khiển trình duyệt, cả app phản hồi chậm. Bản này chạy
asyncio trong tiến trình riêng: 1 luồng gánh hàng trăm kết nối và không đụng tới GIL của app.

Giao tiếp với app: đọc 1 dòng JSON cấu hình từ stdin {"host","port","user","pass"}, in "PORT <n>" ra stdout.
stdin bị đóng (app tắt/chết) -> tự thoát, không để lại tiến trình mồ côi.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys

BUF = 65536
HANDSHAKE_TIMEOUT = 20


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
    try:
        while True:
            data = await src.read(BUF)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            dst.close()
        except Exception:  # noqa: BLE001
            pass


def _close(*writers):
    for w in writers:
        if w is not None:
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass


def make_handler(up_host: str, up_port: int, auth_b64: str):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        r_writer = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HANDSHAKE_TIMEOUT)
            first, _, rest = head.partition(b"\r\n")
            words = first.split()
            if len(words) < 2:
                _close(writer)
                return
            method, target = words[0].upper(), words[1].decode("latin1")
            r_reader, r_writer = await asyncio.wait_for(asyncio.open_connection(up_host, up_port), HANDSHAKE_TIMEOUT)
            auth = f"Proxy-Authorization: Basic {auth_b64}\r\n".encode("latin1") if auth_b64 else b""
            if method == b"CONNECT":
                r_writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n".encode("latin1") + auth
                               + b"Proxy-Connection: Keep-Alive\r\n\r\n")
                await r_writer.drain()
                resp = await asyncio.wait_for(r_reader.readuntil(b"\r\n\r\n"), HANDSHAKE_TIMEOUT)
                if b" 200" not in resp.split(b"\r\n", 1)[0]:
                    writer.write(resp)
                    await writer.drain()
                    _close(writer, r_writer)
                    return
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                # HTTP thường qua proxy: chèn Proxy-Authorization ngay sau dòng request
                r_writer.write(first + b"\r\n" + auth + rest)
                await r_writer.drain()
            await asyncio.gather(_pipe(reader, r_writer), _pipe(r_reader, writer))
        except Exception:  # noqa: BLE001
            _close(writer, r_writer)
    return handle


async def main():
    loop = asyncio.get_running_loop()
    line = await loop.run_in_executor(None, sys.stdin.readline)
    if not line:
        return
    cfg = json.loads(line)
    auth_b64 = ""
    if cfg.get("user") and cfg.get("pass"):
        auth_b64 = base64.b64encode(f"{cfg['user']}:{cfg['pass']}".encode()).decode()
    server = await asyncio.start_server(make_handler(cfg["host"], int(cfg["port"]), auth_b64),
                                        "127.0.0.1", 0, backlog=512)
    port = server.sockets[0].getsockname()[1]
    sys.stdout.write(f"PORT {port}\n")
    sys.stdout.flush()
    # App đóng stdin (tắt hoặc chết) -> thoát theo
    await loop.run_in_executor(None, sys.stdin.read)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
