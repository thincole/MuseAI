"""极简 Chrome DevTools Protocol 客户端（仅依赖 websocket-client / requests）。"""
from __future__ import annotations

import json
import socket
import threading
import time
from urllib.parse import urlsplit

import requests
import websocket


class CDPError(RuntimeError):
    pass


def _origin_for(ws_url: str) -> str | None:
    """由 ws://host:port/... 推导出 http://host:port，作为显式 Origin。

    Chromium 以 --remote-allow-origins=http://127.0.0.1:<port>,http://localhost:<port>
    启动，websocket-client 默认也会按 URL 生成同样的 Origin；这里显式传入，
    避免不同版本的默认行为差异导致握手被拒（403）。
    """
    try:
        p = urlsplit(ws_url)
        if not p.hostname:
            return None
        scheme = "https" if p.scheme == "wss" else "http"
        host = p.hostname
        if ":" in host:  # IPv6 字面量
            host = f"[{host}]"
        return f"{scheme}://{host}:{p.port}" if p.port else f"{scheme}://{host}"
    except Exception:  # noqa: BLE001
        return None


class CDP:
    def __init__(self, ws_url: str, timeout: float = 90.0, max_size: int = 256 << 20):
        origin = _origin_for(ws_url)
        if origin:
            self.ws = websocket.create_connection(ws_url, timeout=timeout, max_size=max_size,
                                                  origin=origin)
        else:
            self.ws = websocket.create_connection(ws_url, timeout=timeout, max_size=max_size)
        self.timeout = timeout
        self._id = 0
        # 同一 CDP 对象可能被多个线程同时调用：send+recv 必须串行，
        # 否则一个线程会把另一个线程的响应读走（并丢弃）。
        self._lock = threading.RLock()

    def send(self, method: str, params: dict | None = None, timeout: float | None = None):
        with self._lock:
            self._id += 1
            mid = self._id
            try:
                self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            except (websocket.WebSocketException, OSError) as exc:
                raise CDPError(f"{method}: CDP 连接已断开 ({type(exc).__name__})") from None
            deadline = time.time() + (timeout or self.timeout)
            while time.time() < deadline:
                try:
                    msg = json.loads(self.ws.recv())
                except (websocket.WebSocketTimeoutException, socket.timeout):
                    continue
                except (websocket.WebSocketException, OSError) as exc:
                    # 连接已关闭/损坏：立即失败，避免 100% CPU 空转到 deadline
                    raise CDPError(f"{method}: CDP 连接已断开 ({type(exc).__name__})") from None
                except ValueError:  # 非 JSON 帧（例如 close 帧返回的空串）
                    continue
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise CDPError(f"{method}: {msg['error']}")
                    return msg
            raise TimeoutError(f"CDP {method} 超时")

    def js(self, expr: str, await_promise: bool = False, timeout: float | None = None):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True,
                       "awaitPromise": await_promise}, timeout)
        res = r.get("result", {}).get("result", {})
        if "value" in res:
            return res["value"]
        return r.get("result")

    def pump(self, seconds: float, on_event=None, sock_timeout: float = 2.0):
        """在 seconds 秒内持续读取事件，交给回调处理。"""
        with self._lock:
            end = time.time() + seconds
            self.ws.settimeout(sock_timeout)
            while time.time() < end:
                try:
                    ev = json.loads(self.ws.recv())
                except (websocket.WebSocketTimeoutException, socket.timeout):
                    continue
                except (websocket.WebSocketException, OSError) as exc:
                    raise CDPError(f"pump: CDP 连接已断开 ({type(exc).__name__})") from None
                except ValueError:
                    continue
                if on_event and "method" in ev:
                    on_event(ev)

    def close(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def http_json(url: str, timeout: float = 5.0):
    return requests.get(url, timeout=timeout).json()
