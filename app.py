"""muse2api — 把 Muse(muse.ai) 网页免费账号的对话/生图/生视频额度反代成 API。

OpenAI 兼容:
  POST /v1/chat/completions          （文本 / 代码对话，支持 stream，可接 Codex）
  POST /v1/images/generations
  POST /v1/videos  +  GET /v1/videos/{task_id}
  GET  /v1/models
  GET  /v1/media/{name}

管理（前端账号池管理页面在 GET /）:
  GET    /admin/status
  GET    /admin/accounts
  POST   /admin/accounts            （单条 / 批量文本 / 批量数组）
  PATCH  /admin/accounts/{id}       （改标签、启用/禁用）
  DELETE /admin/accounts/{id}
  POST   /admin/accounts/{id}/test  （真实打开 muse.ai 验证会话是否有效）
  POST   /admin/accounts/{id}/relogin
  GET    /admin/tasks               （任务记录）
  DELETE /admin/tasks/{id}  |  POST /admin/tasks/clear
  GET    /admin/media  |  POST /admin/media/delete  |  DELETE /admin/media/{name}
  GET    /admin/extension           （浏览器扩展 zip，用来取 cookie）
  GET    /admin/cookie-helper       （命令行取 cookie 脚本，进阶）
"""
from __future__ import annotations

from typing import Any

import asyncio
import base64
import io
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import threading
import time
import uuid
import zipfile
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)
from pydantic import BaseModel, Field

from config import CFG
from engine import ESSENTIAL_COOKIES, MuseAuthError, MuseEngine, MuseGenerationError
from store import Store, account_expiry, min_expiry

import sys
import logger_setup
logger_setup.setup_logging()

log = logging.getLogger("muse2api")
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(_h)

CFG.ensure_dirs()
app = FastAPI(title="muse2api", version="1.5.4")

# Cookie 助手脚本从 muse.ai 页面发起导入请求，需要放行该来源；
# 浏览器扩展从 chrome-extension:// 发起，也一并放行。
#
# 这里直接放行所有来源：本服务用 Bearer Key 鉴权、不依赖 Cookie，
# 放行来源不会带来越权风险；反之如果把来源限死，浏览器端的智能体
# （Open WebUI / LobeChat / 各种 Web 客户端）会被 CORS 拦住用不了。
_origins = [o.strip() for o in (CFG.cors_origins or "").split(",") if o.strip()]
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(CORSMiddleware,
                   allow_origins=["*"],
                   allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["*"],
                   expose_headers=["*"],
                   max_age=600)

# Các API nội bộ của Studio (Shopee / proxy / trình duyệt) đọc-ghi file và cấu hình trên máy,
# nên bắt buộc Bearer key giống /admin/*. Đặt ở middleware để endpoint mới thêm sau cũng được bảo vệ.
_PROTECTED_PREFIXES = ("/api/shopee/", "/api/proxy/", "/api/browser/")


@app.middleware("http")
async def _require_key_for_internal_api(request: Request, call_next):
    if request.method != "OPTIONS" and request.url.path.startswith(_PROTECTED_PREFIXES):
        try:
            # Nếu đặt MUSE2API_ADMIN_KEY thì các API nội bộ chỉ nhận admin key (xem admin_auth).
            admin_auth(request.headers.get("authorization"))
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


store = Store(CFG)
engine = MuseEngine(CFG)
GEN_LOCK = threading.Lock()
IMAGE_TASK_LOCK = threading.Lock()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ------------------------- OpenAI 风格的错误响应 -------------------------
# 各类智能体基本都按 OpenAI 的 {"error": {"message": ...}} 取错误信息；
# FastAPI 默认返回的是 {"detail": ...}，客户端会读不到原因、只显示"未知错误"。
# 所以 /v1/* 统一转成 OpenAI 格式，管理接口保持原样（前端依赖 detail）。
def _err_type(status: int) -> str:
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "server_error"
    return "invalid_request_error"


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    if request.url.path.startswith("/v1/"):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": str(exc.detail),
                               "type": _err_type(exc.status_code),
                               "param": None, "code": exc.status_code}},
            headers=getattr(exc, "headers", None))
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_exc(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/v1/"):
        return JSONResponse(status_code=422, content={"error": {
            "message": "请求参数校验失败：" + str(exc.errors())[:400],
            "type": "invalid_request_error", "param": None, "code": 422}})
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

MODELS = [
    {"id": "muse-spark", "object": "model", "owned_by": "muse",
     "description": "Muse Spark —— 文本 / 代码对话（网页免费额度，支持流式）"},
    {"id": "muse-image", "object": "model", "owned_by": "muse",
     "description": "Muse Image —— 文生图 / 图像编辑（网页免费额度）"},
    {"id": "muse-video", "object": "model", "owned_by": "muse",
     "description": "Muse Video —— 文生视频 / 图生视频（网页免费额度）"},
]

# 下游（Codex / Cline / 各种客户端）习惯按 OpenAI、Anthropic 的名字传模型，
# 这里统一映射到 muse 的真实能力上。
#
# 说明：muse.ai 网页是自动路由的 agent，对外**没有可枚举的模型清单**，
# 能稳定调用的就是三条真实能力 —— Muse Spark（语言/代码）、
# Muse Image（生图）、Muse Video（生视频）。别名只是让下游不用改配置。
MODEL_ALIASES = {
    # ---- 文本 / 代码 → muse-spark ----
    "muse-text": "muse-spark", "muse-chat": "muse-spark", "muse-llm": "muse-spark",
    "koda": "muse-spark",
    "gpt-3.5-turbo": "muse-spark", "gpt-4": "muse-spark", "gpt-4-turbo": "muse-spark",
    "gpt-4o": "muse-spark", "gpt-4o-mini": "muse-spark", "gpt-4.1": "muse-spark",
    "gpt-4.1-mini": "muse-spark", "gpt-5": "muse-spark", "gpt-5-codex": "muse-spark",
    "o1": "muse-spark", "o1-mini": "muse-spark", "o3": "muse-spark",
    "o3-mini": "muse-spark", "o4-mini": "muse-spark",
    "codex": "muse-spark", "codex-mini-latest": "muse-spark",
    "claude-3-5-sonnet": "muse-spark", "claude-3-5-sonnet-latest": "muse-spark",
    "claude-3-7-sonnet": "muse-spark", "claude-sonnet-4": "muse-spark",
    "claude-opus-4": "muse-spark", "claude-3-opus": "muse-spark",
    "claude-3-haiku": "muse-spark",
    "deepseek-chat": "muse-spark", "deepseek-coder": "muse-spark",
    "deepseek-reasoner": "muse-spark", "qwen-coder": "muse-spark",
    "gemini-2.5-pro": "muse-spark", "gemini-2.5-flash": "muse-spark",
    # ---- 生图 → muse-image ----
    "muse-img": "muse-image", "dall-e": "muse-image", "dall-e-3": "muse-image",
    "gpt-image-1": "muse-image", "flux": "muse-image", "midjourney": "muse-image",
    # ---- 生视频 → muse-video ----
    "muse-vid": "muse-video", "muse-videos": "muse-video",
    "sora": "muse-video", "sora-2": "muse-video", "veo": "muse-video",
    "veo-3": "muse-video", "kling": "muse-video", "runway": "muse-video",
}


def resolve_model(name: str | None, default: str = "muse-image") -> str:
    n = (name or "").strip().lower()
    return MODEL_ALIASES.get(n, n or default)


# ------------------------- 鉴权 -------------------------
def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "缺少 Authorization: Bearer <key>")
    parts = authorization.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _token_matches(token: str, key: str | None) -> bool:
    return bool(token and key) and secrets.compare_digest(token.encode(), str(key).encode())


def _admin_key() -> str:
    return (getattr(CFG, "admin_key", "") or "").strip()


def auth(authorization: str | None = Header(default=None)):
    """Key cho /v1/*: nhận API key hoặc admin key (nếu có đặt MUSE2API_ADMIN_KEY)."""
    if not CFG.api_key and not _admin_key():
        return True
    token = _bearer_token(authorization)
    if not (_token_matches(token, CFG.api_key) or _token_matches(token, _admin_key())):
        raise HTTPException(401, "API key 无效")
    return True


def admin_auth(authorization: str | None = Header(default=None)):
    """Key cho /admin/* và /api/{shopee,proxy,browser}/*.

    MUSE2API_ADMIN_KEY rỗng -> giống hệt auth (hành vi cũ). Có đặt -> chỉ nhận admin key,
    để API key phát cho client bên ngoài (/v1) không dùng được vào trang quản trị / đọc-ghi file.
    """
    admin_key = _admin_key()
    if not admin_key:
        return auth(authorization)
    token = _bearer_token(authorization)
    if not _token_matches(token, admin_key):
        raise HTTPException(401, "Admin key 无效")
    return True


def _is_admin_request(request: Request) -> bool:
    try:
        admin_auth(request.headers.get("authorization"))
        return True
    except HTTPException:
        return False


# ------------------------- 请求模型 -------------------------

def _renew_and_persist(acc_id: str, wake_vm: bool = True, force: bool = False) -> dict | None:
    """调用 /api/session 续签账号 cookie 并写回 store，返回最新账号 dict。
    近期（10分钟内）已续签且状态正常的账号直接复用，避免每次请求阻塞 2~3 秒 HTTP 往返。"""
    acc = store.get_account(acc_id)
    if not acc or not acc.get("cookies"):
        return acc
    now = time.time()
    last_sync = max(
        int(acc.get("synced_at") or 0),
        int(acc.get("last_keepalive") or 0),
        int(engine._last_http_renew.get(acc_id, 0)),
    )
    if not force and acc.get("ok") is True and (now - last_sync) < 600:
        engine._last_http_renew[acc_id] = last_sync
        return acc
    try:
        p_url = None
        try:
            import proxy_manager
            p_url = proxy_manager.get_forwarder_url_for_account(acc_id)
        except Exception:
            pass
        res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=wake_vm, proxy_url=p_url)
        engine._last_http_renew[acc_id] = now
        if res.get("cookies"):
            store.update_account(acc_id, cookies=res["cookies"],
                                 cookies_exp=res.get("cookies_exp"),
                                 ok=True if res.get("ok") else acc.get("ok"),
                                 synced_at=int(now))
            store.touch_keepalive(acc_id, True, f"Phiên hợp lệ (VM: {res.get('vm_state') or 'RUNNING'})")
    except MuseAuthError as exc:
        store.mark(acc_id, False, str(exc))
        raise
    except Exception as exc:
        log.warning("HTTP 预续签账号 %s 异常: %s", acc_id, exc)
    return store.get_account(acc_id)


def safe_chat_stream(cookies: dict, prompt: str, expires: dict | None,
                     timeout: int, account_id: str | None):
    """在独立线程中执行 chat_stream 并持有 GEN_LOCK，通过 Queue 往外吐。
    无论下游客户端何时断连、异常或超时，stop_event + finally 块保证 100% 立即释放 GEN_LOCK，绝不死锁。
    若首字前遇到单账号 VM 卡死或会话异常，自动切换下一个健康账号重试一次。"""
    import queue
    q = queue.Queue(maxsize=100)
    stop_event = threading.Event()

    def worker():
        cur_id = account_id
        cur_cookies = cookies
        cur_exp = expires
        last_exc = None
        try:
            for attempt in range(2):
                if stop_event.is_set():
                    return
                if attempt > 0:
                    alt = store.pick_account(rotate=True, force_rotate=True, exclude_id=cur_id)
                    if not alt or alt["id"] == cur_id:
                        break
                    cur_id = alt["id"]
                    cur_cookies = alt["cookies"]
                    cur_exp = alt.get("cookies_exp")
                    log.info("【对话自动切号】切换到备用账号 %s (%s) 重试...", alt.get("label"), cur_id)
                yielded = False
                try:
                    if cur_id:
                        refreshed = _renew_and_persist(cur_id, wake_vm=True, force=(attempt > 0))
                        if refreshed:
                            cur_cookies = refreshed["cookies"]
                            cur_exp = refreshed.get("cookies_exp")
                    with GEN_LOCK:
                        if stop_event.is_set():
                            return
                        engine.start()
                        for chunk in engine.chat_stream(
                            cur_cookies, prompt, cur_exp, timeout,
                            account_id=cur_id, stop_event=stop_event
                        ):
                            yielded = True
                            q.put(("data", chunk))
                            if stop_event.is_set():
                                return
                    if cur_id:
                        store.mark(cur_id, True, "")
                        _sync_cookies(cur_id)
                    return
                except MuseAuthError as exc:
                    last_exc = exc
                    if cur_id:
                        store.mark(cur_id, False, str(exc))
                    if yielded:
                        break
                except Exception as exc:
                    last_exc = exc
                    try:
                        engine.reset_thread()
                    except Exception:
                        pass
                    if yielded:
                        break
            if last_exc is not None:
                q.put(("error", last_exc))
        finally:
            q.put(("done", None))

    threading.Thread(target=worker, daemon=True).start()

    try:
        while True:
            kind, val = q.get()
            if kind == "data":
                yield val
            elif kind == "error":
                raise val
            else:
                break
    finally:
        stop_event.set()


class ImageRequest(BaseModel):
    prompt: str
    model: str = "muse-image"
    n: int = 1
    size: str | None = None
    aspect_ratio: str | None = None
    response_format: str = "url"      # url | b64_json
    timeout: int | None = Field(default=None, ge=1, le=600)
    extra: str | None = None
    image: Any = None
    images: list | None = None
    reference_image: str | None = None
    async_: bool = Field(False, alias="async")


class VideoRequest(BaseModel):
    prompt: str
    model: str = "muse-video"
    duration: int | None = None
    size: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    timeout: int | None = None
    extra: str | None = None
    image: Any = None
    image_url: Any = None
    reference_image: str | None = None


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list | None = None


class ChatRequest(BaseModel):
    """OpenAI Chat Completions 请求。

    对第三方客户端要尽量宽松：不认识字段一律忽略（Pydantic 默认行为），
    只挑我们真正用得上的读 —— 否则各种智能体各传各的参数就会 422。
    """
    model: str = "muse-spark"
    messages: list[ChatMessage] = []
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    prompt: str | None = None       # 兼容把 prompt 直接放顶层的客户端
    # 下面这些声明出来只是为了「能读到」，muse.ai 端不做对应处理
    tools: list | None = None
    tool_choice: object | None = None
    response_format: object | None = None
    stream_options: object | None = None


class ResponsesRequest(BaseModel):
    """OpenAI Responses API（新版 Codex 默认走这个）。"""
    model: str = "muse-spark"
    input: str | list | None = None
    instructions: str | None = None
    stream: bool = False
    max_output_tokens: int | None = None
    timeout: int | None = None
    tools: list | None = None
    store: bool | None = None


class AccountRequest(BaseModel):
    label: str = ""
    cookies: dict[str, str] = Field(default_factory=dict)
    cookie_header: str | None = None
    batch: str | None = None          # 多行文本，每行一个账号
    expires: dict[str, int] = Field(default_factory=dict)   # cookie 名 -> 过期时间戳


class AccountPatch(BaseModel):
    label: str | None = None
    enabled: bool | None = None


# ------------------------- 提示词构造 -------------------------
def build_image_prompt(r: ImageRequest) -> str:
    has_ref = bool(r.reference_image or r.image or r.images)
    if has_ref:
        parts = [f"基于我本次上传附带的参考图片进行生图/编辑：{r.prompt.strip()}"]
    else:
        parts = [f"全新文生图创作（当前未提供任何参考图，请勿查找历史相册或向用户索要原图，直接根据文字描述从零绘制生成一张全新图片）：{r.prompt.strip()}"]
    ar = (r.aspect_ratio or "").strip().lower()
    sz = (r.size or "").strip().lower()

    if any(k in ar or k in sz for k in ("9:16", "9/16", "portrait", "竖屏", "720x1280", "1080x1920")):
        parts.append("【画面构图与比例要求】：严格 9:16 竖屏满屏画幅（9:16 vertical portrait aspect ratio，高大于宽的手机全屏竖版画面），绝对不要生成横屏，保持垂直构图")
    elif any(k in ar or k in sz for k in ("16:9", "16/9", "landscape", "横屏", "1280x720", "1920x1080")):
        parts.append("【画面构图与比例要求】：16:9 宽屏横屏画幅（16:9 widescreen landscape aspect ratio）")
    elif any(k in ar or k in sz for k in ("1:1", "square", "正方形", "1024x1024")):
        parts.append("【画面构图与比例要求】：1:1 正方形画幅（1:1 square aspect ratio）")
    elif any(k in ar or k in sz for k in ("4:3", "4/3")):
        parts.append("【画面构图与比例要求】：4:3 比例画幅")
    elif any(k in ar or k in sz for k in ("3:4", "3/4")):
        parts.append("【画面构图与比例要求】：3:4 竖向画幅")
    elif r.aspect_ratio:
        parts.append(f"【画面构图与比例要求】：{r.aspect_ratio} 画面比例")
    elif r.size:
        parts.append(f"尺寸/比例：{r.size}")

    if has_ref:
        parts.append("【纯净画面要求】：彻底清除并去除参考图中的所有文字、水印、签名、角标及Logo标记（clean image without any watermark, text, or logo），输出绝对纯净无字画面")

    if r.extra:
        parts.append(r.extra)
    return "，".join(parts)


def _content_text(content) -> str:
    """把 OpenAI 的 content 归一成纯文本（兼容多模态 list 形式）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("type")
                if t in (None, "text", "input_text", "output_text"):
                    parts.append(str(item.get("text") or ""))
                elif t == "image_url":
                    parts.append("[图片]")
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(content)


def build_chat_prompt(messages: list[ChatMessage]) -> str:
    """把 messages 拼成发给 muse.ai 的一段提示词。

    muse.ai 网页本身是个带上下文的会话，但本 API 是无状态的（每次可能落到
    不同账号/页面），所以把历史拼进 prompt 最可控 —— 这正好匹配 Codex 这类
    「每轮都带全量历史」的客户端。
    """
    system, turns = [], []
    for m in messages:
        role = (m.role or "").strip().lower()
        text = _content_text(m.content).strip()
        if role == "tool":
            # 工具执行结果 → 当成"用户提供的信息"发过去
            if text:
                turns.append(("user", "【工具执行结果】\n" + text))
            continue
        if not text:
            # 部分客户端的 assistant 消息只带 tool_calls、没有正文
            if role == "assistant" and m.tool_calls:
                turns.append(("assistant", "【请求调用工具】" + json.dumps(
                    m.tool_calls, ensure_ascii=False)[:600]))
            continue
        if role in ("system", "developer"):
            system.append(text)
        else:
            turns.append((role, text))

    # 单轮且无系统指令 → 直接发原文，最贴近自然对话
    if len(turns) == 1 and not system and turns[0][0] == "user":
        return turns[0][1]

    parts = []
    if system:
        sys_text = "\n\n".join(system)
        sys_text = sys_text.replace("danger-full-access", "standard-workspace-access")
        parts.append(f"背景与任务设定：\n{sys_text}")
    for role, text in turns:
        label = "助手" if role == "assistant" else "用户"
        parts.append(f"{label}：\n{text}")
    return "\n\n".join(parts)


# ------------------------- 工具调用（function calling）适配 -------------------------
# muse.ai 的网页模型**不会**返回结构化的 tool_calls，所以这里做一层协议适配：
#   1) 请求带 tools 时，把工具定义翻译成提示词里的【工具调用协议】；
#   2) 模型按协议输出 ```json {"tool": "...", "arguments": {...}} ```；
#   3) 我们把这段解析回 OpenAI 的 tool_calls 交给下游 agent。
#
# 注意：这是"尽力适配"而非保证 —— 目标模型是通用对话模型，没有针对
# function calling 做微调，遵守协议的程度需要实测观察。
_TOOL_PROTOCOL_HEAD = """你可以根据需要调用以下工具来协助用户完成任务。
若需调用工具，请直接输出如下格式的 JSON 代码块（不要包含其他多余解释）：
```json
{"name": "<工具名>", "arguments": {<参数>}}
```
如果需要调用多个工具，请输出包含多个对象的 JSON 数组。
如果无需调用工具，请直接用自然语言回答。

可用工具列表：
"""


def _describe_params(params) -> str:
    """把 JSON Schema 的参数描述成易读的多行文本。"""
    if not isinstance(params, dict):
        return "      （无参数）"
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    if not props:
        return "      （无参数）"
    lines = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        lines.append("      - %s (%s, %s)%s" % (
            name, spec.get("type") or "any",
            "必填" if name in required else "可选",
            (" " + spec["description"]) if spec.get("description") else ""))
    return "\n".join(lines)


def build_tools_prompt(tools: list | None) -> str:
    """把 tools 定义翻译成提示词片段（没有工具时返回空串）。

    同时兼容 Chat Completions 的 `{"type":"function","function":{...}}`
    和 Responses API 的 `{"type":"function","name":...,"parameters":...}`。
    """
    if not tools:
        return ""
    items = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if not fn.get("name"):
            continue
        desc = fn.get("description") or ""
        items.append("%d. %s%s\n   参数：\n%s" % (
            len(items) + 1, fn["name"], (" — " + desc) if desc else "",
            _describe_params(fn.get("parameters"))))
    if not items:
        return ""
    return _TOOL_PROTOCOL_HEAD + "\n".join(items) + "\n"


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*([\s\S]*?)```")


def _as_tool_calls(obj) -> list[dict] | None:
    """把解析出的 JSON 转成 OpenAI tool_calls；不像工具调用就返回 None。"""
    raw = obj if isinstance(obj, list) else [obj]
    if not raw:
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        name = item.get("tool") or item.get("name") or item.get("function")
        if not isinstance(name, str) or not name:
            return None
        args = item.get("arguments")
        if args is None:
            args = item.get("parameters") or item.get("args") or {}
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({"id": "call_" + uuid.uuid4().hex[:20], "type": "function",
                    "function": {"name": name, "arguments": args}})
    return out or None


def parse_tool_calls(text: str) -> tuple[list[dict] | None, str]:
    """从模型输出里抽出工具调用。

    返回 (tool_calls, 剩余文本)；抽不到就返回 (None, 原文)。
    支持多种形态：
    1) ```json ... ``` 代码块；
    2) 裸 JSON（或带有前导 json/JSON 关键字）；
    3) 文本中内嵌的完整 JSON 对象或数组。
    """
    if not text:
        return None, text
    for m in reversed(list(_FENCE_RE.finditer(text))):
        try:
            calls = _as_tool_calls(json.loads(m.group(1).strip()))
        except Exception:
            continue
        if calls:
            return calls, (text[:m.start()] + text[m.end():]).strip()

    stripped = text.strip()
    clean_stripped = re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", stripped).rstrip("`").strip()
    if clean_stripped.startswith(("{", "[")):
        try:
            calls = _as_tool_calls(json.loads(clean_stripped))
            if calls:
                return calls, ""
        except Exception:
            pass

    m_json = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if m_json:
        try:
            calls = _as_tool_calls(json.loads(m_json.group(1).strip()))
            if calls:
                rest = (text[:m_json.start()] + text[m_json.end():]).strip()
                if re.sub(r"^(?:```)?(?:json|JSON)?\s*", "", rest).strip("`").strip() == "":
                    rest = ""
                return calls, rest
        except Exception:
            pass

    return None, text


def build_video_prompt(r: VideoRequest) -> str:
    user_prompt = r.prompt.strip()
    ar = (r.aspect_ratio or "").strip().lower()
    sz = (r.size or "").strip().lower()
    dur = r.duration or 8
    has_ref = bool(r.reference_image or r.image or r.image_url)

    is_vertical = any(k in ar or k in sz for k in ("9:16", "9/16", "portrait", "竖屏", "720x1280", "1080x1920"))

    parts = []
    if is_vertical:
        if has_ref:
            parts.append(f"基于我本次上传的参考图片附件作为第一帧参考图（严禁使用历史图片或任何其他图像，必须严格以我当前刚刚上传并附带在此处的这张图片为起始帧）：生成一个严格9:16竖屏手机满屏的动态图生视频（9:16 vertical portrait video，720x1280，高大于宽的手机全屏竖版画面，严格以附带的参考图为起始第一帧延续动作，严禁生成横屏或黑边，时长严格为 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成，严禁参考任何历史图片或上下文）：生成一个严格9:16竖屏手机满屏视频（9:16 vertical portrait video，720x1280，高大于宽的手机全屏竖版画面，严禁生成横屏或带有左右黑边，保持垂直构图，时长严格为 {dur} 秒）：{user_prompt}")
    elif any(k in ar or k in sz for k in ("16:9", "16/9", "landscape", "横屏", "1280x720", "1920x1080")):
        if has_ref:
            parts.append(f"基于我本次上传的参考图片附件作为第一帧参考图（严禁使用历史图片或任何其他图像，必须严格以我当前刚刚上传并附带在此处的这张图片为起始帧）：生成一个16:9宽屏横屏图生视频（16:9 widescreen landscape video，严格以附带的参考图为起始第一帧延续动作，时长严格为 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成，严禁参考任何历史图片或上下文）：生成一个16:9横屏宽屏视频（16:9 widescreen landscape video，时长严格为 {dur} 秒）：{user_prompt}")
    else:
        if has_ref:
            parts.append(f"基于我本次上传的参考图片附件作为第一帧参考图（严禁使用历史图片或任何其他图像，必须严格以我当前刚刚上传并附带在此处的这张图片为起始帧）：生成动态图生视频（严格以附带的参考图为起始第一帧延续动作，时长严格为 {dur} 秒）：{user_prompt}")
        else:
            parts.append(f"全新文生视频创作（纯文本全新生成，严禁参考任何历史图片或上下文）：生成一个视频（时长严格为 {dur} 秒）：{user_prompt}")

    if r.resolution:
        parts.append(f"画质规格：{r.resolution}")
    if r.extra:
        parts.append(r.extra)
    return "，".join(parts)


def media_url(name: str) -> str:
    """媒体地址。

    配了 public_base 就返回**绝对 URL** —— OpenAI 兼容客户端（以及各类智能体
    平台）拿到 data[].url 后一般会直接渲染或下载，相对路径会被解析到客户端
    自己的域名上，导致 404。public_base 为空时退回相对路径。
    """
    base = _public_base()
    return f"{base}/v1/media/{name}" if base else f"/v1/media/{name}"


# ------------------------- cookie 解析 -------------------------
def parse_cookie_text(text: str) -> dict[str, str]:
    """把 `a=1; b=2` / JSON / JSON Array / Set-Cookie 行解析成 dict。"""
    text = (text or "").strip()
    if not text:
        return {}
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        import json
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                if "cookies" in obj and isinstance(obj["cookies"], dict):
                    return {str(k).strip(): str(v).strip().strip('"').strip("'") for k, v in obj["cookies"].items() if str(k).strip()}
                return {str(k).strip(): str(v).strip().strip('"').strip("'") for k, v in obj.items() if str(k).strip()}
            elif isinstance(obj, list):
                out = {}
                for item in obj:
                    if isinstance(item, dict):
                        k = item.get("name") or item.get("key")
                        v = item.get("value") or item.get("content")
                        if k and v is not None:
                            out[str(k).strip()] = str(v).strip().strip('"').strip("'")
                if out:
                    return out
        except Exception:  # noqa: BLE001
            pass
    out: dict[str, str] = {}
    for part in re.split(r"[;\r\n]+", text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        if part.lower().startswith("cookie:"):
            part = part[7:].strip()
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def parse_batch(text: str) -> list[tuple[str, dict]]:
    """批量导入：每行 `标签 | cookie串`，标签可省略。"""
    out: list[tuple[str, dict]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        label = ""
        body = line
        if "|" in line:
            head, tail = line.split("|", 1)
            if "=" not in head:          # `|` 出现在 cookie 里时不算分隔符
                label, body = head.strip(), tail.strip()
        cookies = parse_cookie_text(body)
        if cookies:
            out.append((label, cookies))
    return out


# ------------------------- 核心生成 -------------------------
def _sync_cookies(acc_id: str) -> dict:
    """生成完从浏览器读回 cookie 并写回账号池。

    实测结论：**核心 cookie 的 expires 不会被使用行为续期**（hatch_vml 固定
    约 2 天就到期），这里同步回来的主要是那些每次访问都会重新下发的非核心
    cookie（_fbp / wd / dpr 等）以及可能新增的条目，避免账号信息比实际更旧。

    注意：这个函数**绝不能把生成结果搞失败** —— 生成已经成功了，
    同步只是锦上添花，出任何问题都只记一条日志。
    """
    try:
        live = engine.read_cookies()
        if not live:
            return {"synced": 0}
        acc = store.get_account(acc_id)
        if not acc:
            return {"synced": 0}
        cur = dict(acc.get("cookies") or {})
        new_vals = {k: v["value"] for k, v in live.items() if v.get("value")}
        # 有效期要「合并」而不是「覆盖」：会话 cookie 读回的 expires 是 -1，
        # 若整体覆盖会把原有有效期记录清空（实测踩过这个坑）。
        exps = dict(acc.get("cookies_exp") or {})
        exps.update({k: v["expires"] for k, v in live.items()
                     if _pos(v.get("expires"))})
        changed = sum(1 for k, v in new_vals.items() if cur.get(k) != v)
        store.update_account(acc_id, cookies={**cur, **new_vals},
                             cookies_exp=exps, synced_at=int(time.time()))
        updated = store.get_account(acc_id) or {}
        return {"synced": len(new_vals), "changed": changed,
                "expires_at": updated.get("expires_at")}
    except Exception as exc:  # noqa: BLE001
        log.warning("cookie 同步失败（不影响本次生成）: %s", exc)
        return {"synced": 0, "error": str(exc)[:200]}


def _pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


def _run_generation(prompt: str, kind: str, timeout: int,
                    account_id: str | None = None, on_progress=None,
                    reference_image: str | None = None) -> tuple[dict, str | None]:
    """Tạo video/ảnh song song đa luồng bằng Isolated Worker Session.
    Mỗi luồng thuê 1 tài khoản từ Account Pool, chạy hoàn toàn độc lập, không bị khóa chặn lẫn nhau."""
    deadline = time.monotonic() + max(1, timeout)
    cur_acc = store.acquire_account(preferred_id=account_id, timeout=max(1, timeout))
    if not cur_acc:
        raise MuseAuthError("Không có tài khoản nào khả dụng hoặc tất cả tài khoản đang bận xử lý")

    last_exc = None
    try:
        for attempt in range(2):
            if deadline is not None and time.monotonic() >= deadline:
                raise MuseGenerationError("Đã hết thời gian chờ tác vụ")
            if attempt > 0:
                alt = store.acquire_account(exclude_id=cur_acc["id"], timeout=10)
                if not alt:
                    break
                store.release_account(cur_acc["id"])
                cur_acc = alt
                log.info("Chuyển sang tài khoản dự phòng: %s (%s)", cur_acc.get("label"), cur_acc["id"])

            session = None
            try:
                remaining = int(deadline - time.monotonic()) if deadline is not None else timeout
                if remaining <= 0:
                    raise MuseGenerationError("Đã hết thời gian chờ tác vụ")

                session = engine.acquire_session(
                    account_id=cur_acc["id"],
                    cookies=cur_acc["cookies"],
                    expires=cur_acc.get("cookies_exp"),
                    timeout=40
                )
                res = session.generate(
                    prompt=prompt,
                    expect=kind,
                    timeout=remaining,
                    on_progress=on_progress,
                    reference_image=reference_image
                )
                store.touch_keepalive(cur_acc["id"], True, "Phiên hợp lệ · Sẵn sàng")
                engine.release_session(session, error=False)
                session = None
                return res, cur_acc["id"]
            except MuseAuthError as exc:
                last_exc = exc
                log.warning("✗ [Video Gen] Lỗi xác thực tài khoản %s: %s", cur_acc["id"], exc)
                store.mark(cur_acc["id"], False, str(exc))
                if session:
                    try:
                        engine.release_session(session, error=True)
                    except Exception:
                        pass
                    session = None
            except MuseGenerationError as exc:
                last_exc = exc
                log.warning("✗ [Video Gen] Lỗi tạo video với TK %s: %s", cur_acc["id"], exc)
                store.mark(cur_acc["id"], True, f"Tác vụ gián đoạn: {str(exc)[:60]}")
                if session:
                    try:
                        engine.release_session(session, error=True)
                    except Exception:
                        pass
                    session = None
            except Exception as exc:
                log.exception("✗ [Video Gen] Ngoại lệ không xác định khi tạo video với TK %s: %s", cur_acc["id"], exc)
                last_exc = MuseGenerationError(f"Tạo video thất bại: {exc}")
                if session:
                    try:
                        engine.release_session(session, error=True)
                    except Exception:
                        pass
                    session = None
            finally:
                if session:
                    try:
                        engine.release_session(session, error=True)
                    except Exception:
                        pass
                    session = None

        raise last_exc or MuseGenerationError("Tạo video thất bại sau các lần thử")
    finally:
        if cur_acc:
            store.release_account(cur_acc["id"])


# ------------------------- 基础接口 -------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": int(time.time())}


@app.get("/readyz")
def readyz():
    accs = [a for a in store.list_accounts() if a.get("enabled", True)]
    return {"status": "ready" if accs else "no_account",
            "accounts": len(accs),
            "browser_running": bool(engine.proc and engine.proc.poll() is None)}


@app.get("/v1/models")
def models(_=Depends(auth)):
    return {"object": "list", "data": MODELS}


# ------------------------- 生图 -------------------------
def _image_response(req: ImageRequest, res: dict) -> dict:
    item = {"revised_prompt": req.prompt, "url": media_url(res["filename"]),
            "size": req.size or "auto", "kind": res["kind"], "bytes": res["size"]}
    if req.response_format == "b64_json":
        fpath = res.get("path") or os.path.join(CFG.media_dir, res["filename"])
        with open(fpath, "rb") as f:
            item["b64_json"] = base64.b64encode(f.read()).decode()
        item.pop("url", None)
    return {"created": int(time.time()), "data": [item]}


def _queue_image(req: ImageRequest, prompt: str, reference_image: str | None,
                 idempotency_key: str | None = None):
    """Opt-in polling avoids reverse-proxy timeouts; no generation is repeated by polling."""
    if idempotency_key and len(idempotency_key) > 256:
        raise HTTPException(400, "Idempotency-Key too long")
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
    request_hash = hashlib.sha256(json.dumps(
        {"request": req.model_dump(by_alias=True), "reference": reference_image},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with IMAGE_TASK_LOCK:
        tasks = list(store.tasks.values())
        if key_hash:
            for existing in tasks:
                if existing.get("image_request_key") == key_hash:
                    if existing.get("image_request_hash") != request_hash:
                        raise HTTPException(409, "Idempotency-Key reused with different request")
                    return JSONResponse(status_code=202, content={
                        "id": existing["id"], "task_id": existing["id"],
                        "object": "image.task", "status": existing["status"],
                        "progress": existing.get("progress", 0),
                        "created_at": existing["created_at"]})
        # ponytail: one Chromium worker; cap admission instead of adding a broker.
        if sum(t.get("kind") == "image" and t.get("status") in ("queued", "processing")
               for t in tasks) >= 8:
            raise HTTPException(429, "Image queue is full; retry later")
        task = store.create_task("image", req.prompt)
        tid = task["id"]
        store.update_task(tid, api_prompt=prompt, size=req.size,
                          response_format=req.response_format, progress=0,
                          image_request_key=key_hash, image_request_hash=request_hash)

    def worker():
        t0 = time.time()
        store.update_task(tid, status="processing", progress=5)
        try:
            res, acc_id = _run_generation(
                prompt, "image", req.timeout or CFG.image_timeout,
                reference_image=reference_image,
                on_progress=lambda p: store.update_task(tid, progress=p))
            # Store only media metadata, not large base64 payloads or reference credentials.
            store.update_task(tid, status="completed", progress=100, account=acc_id,
                              elapsed=round(time.time() - t0, 1),
                              url=media_url(res["filename"]),
                              result={**{k: res[k] for k in ("filename", "size", "kind")},
                                      "url": media_url(res["filename"])})
        except Exception as exc:  # noqa: BLE001
            store.update_task(tid, status="failed", error=str(exc),
                              elapsed=round(time.time() - t0, 1))

    threading.Thread(target=worker, daemon=True).start()
    return JSONResponse(status_code=202, content={
        "id": tid, "task_id": tid, "object": "image.task", "status": "queued",
        "progress": 0, "created_at": task["created_at"]})


@app.post("/v1/images/tasks")
def create_image_task(req: ImageRequest,
                      idempotency_key: str | None = Header(default=None), _=Depends(auth)):
    ref_img = req.reference_image or req.image
    if isinstance(ref_img, dict):
        ref_img = ref_img.get("url") or ref_img.get("b64_json")
    if not ref_img and req.images:
        first = req.images[0]
        ref_img = first.get("image_url") or first.get("url") if isinstance(first, dict) else first
    return _queue_image(req, build_image_prompt(req), ref_img, idempotency_key)


@app.get("/v1/images/tasks/{task_id}")
def get_image_task(task_id: str, _=Depends(auth)):
    task = store.get_task(task_id)
    if not task or task.get("kind") != "image":
        raise HTTPException(404, "image task 不存在")
    out = dict(task)
    if out.get("status") == "completed":
        req = ImageRequest(prompt=out["prompt"], size=out.get("size"),
                           response_format=out.get("response_format") or "url")
        out.update(_image_response(req, out["result"]))
    return out


@app.post("/v1/images/generations")
async def images_generations(req: ImageRequest, _=Depends(auth)):
    ref_img = req.reference_image or req.image
    if isinstance(ref_img, dict):
        ref_img = ref_img.get("url") or ref_img.get("b64_json")
    if not ref_img and req.images and isinstance(req.images, list):
        first = req.images[0]
        ref_img = first.get("image_url") or first.get("url") if isinstance(first, dict) else first

    prompt = build_image_prompt(req)
    timeout = req.timeout or CFG.image_timeout
    if req.async_:
        return _queue_image(req, prompt, ref_img)
    try:
        res, _acc = await asyncio.to_thread(_run_generation, prompt, "image", timeout, reference_image=ref_img)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _image_response(req, res)


@app.post("/v1/images/edits")
async def images_edits(request: Request, _=Depends(auth)):
    """OpenAI 兼容的图生图/图像编辑接口，兼容 multipart/form-data 与 application/json。"""
    content_type = request.headers.get("content-type", "").lower()
    prompt = ""
    model = "muse-image"
    size = None
    aspect_ratio = None
    response_format = "url"
    timeout = None
    ref_image_data = None
    async_mode = False

    if "multipart/form-data" in content_type:
        form = await request.form()
        async_mode = form.get("async", False)
        prompt = form.get("prompt") or ""
        model = form.get("model") or "muse-image"
        size = form.get("size")
        aspect_ratio = form.get("aspect_ratio")
        response_format = form.get("response_format") or "url"
        timeout_val = form.get("timeout")
        if timeout_val:
            try:
                timeout = int(timeout_val)
            except ValueError:
                pass
        img_field = form.get("image")
        if img_field and hasattr(img_field, "read"):
            content = await img_field.read()
            ref_mime = getattr(img_field, "content_type", "image/png") or "image/png"
            ref_image_data = f"data:{ref_mime};base64,{base64.b64encode(content).decode('ascii')}"
        elif isinstance(img_field, str):
            ref_image_data = img_field
    else:
        body = await request.json()
        async_mode = body.get("async", False)
        prompt = body.get("prompt") or ""
        model = body.get("model") or "muse-image"
        size = body.get("size")
        aspect_ratio = body.get("aspect_ratio")
        response_format = body.get("response_format") or "url"
        timeout = body.get("timeout")
        img_val = body.get("image")
        if isinstance(img_val, dict):
            ref_image_data = img_val.get("url") or img_val.get("b64_json")
        elif isinstance(img_val, str):
            ref_image_data = img_val
        if not ref_image_data and body.get("images") and isinstance(body.get("images"), list):
            first = body["images"][0]
            if isinstance(first, dict):
                ref_image_data = first.get("image_url") or first.get("url")
            elif isinstance(first, str):
                ref_image_data = first
        if not ref_image_data:
            ref_image_data = body.get("reference_image") or body.get("image_url")

    if not prompt:
        prompt = "参考此图片并进行生图创作"

    req_obj = ImageRequest(
        prompt=prompt,
        model=model,
        size=size,
        aspect_ratio=aspect_ratio,
        response_format=response_format,
        timeout=timeout,
        reference_image=ref_image_data,
        **{"async": async_mode}
    )
    full_prompt = build_image_prompt(req_obj)
    gen_timeout = timeout or CFG.image_timeout
    if req_obj.async_:
        return _queue_image(req_obj, full_prompt, ref_image_data)
    try:
        res, _acc = await asyncio.to_thread(_run_generation, full_prompt, "image", gen_timeout, reference_image=ref_image_data)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc

    return _image_response(req_obj, res)


# ------------------------- 生视频（异步任务） -------------------------
@app.post("/v1/videos")
@app.post("/v1/videos/generations")
async def create_video(req: VideoRequest, _=Depends(auth)):
    ref_img = None
    if req.reference_image:
        ref_img = req.reference_image
    elif req.image_url:
        ref_img = req.image_url if isinstance(req.image_url, str) else (req.image_url.get("url") if isinstance(req.image_url, dict) else None)
    elif req.image:
        ref_img = req.image.get("url") if isinstance(req.image, dict) else req.image

    prompt = build_video_prompt(req)
    timeout = req.timeout or CFG.video_timeout
    task = store.create_task("video", req.prompt)
    store.update_task(task["id"], api_prompt=prompt)

    store.update_task(task["id"], progress=10)

    def worker():
        store.update_task(task["id"], status="processing", progress=15)
        t0 = time.time()
        try:
            def prog_cb(p):
                store.update_task(task["id"], progress=p)
            res, acc_id = _run_generation(prompt, "video", timeout, on_progress=prog_cb, reference_image=ref_img)
            vurl = media_url(res["filename"])
            store.update_task(task["id"], status="completed", progress=100, account=acc_id,
                              elapsed=round(time.time() - t0, 1),
                              url=vurl,
                              video={"url": vurl},
                              result={"url": vurl,
                                      "filename": res["filename"],
                                      "bytes": res["size"], "kind": res["kind"]})
        except Exception as exc:  # noqa: BLE001
            store.update_task(task["id"], status="failed",
                              elapsed=round(time.time() - t0, 1), error=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return {"id": task["id"], "task_id": task["id"], "object": "video.task", "status": "queued",
            "progress": 10, "created_at": task["created_at"]}


@app.get("/v1/videos/{task_id}")
@app.get("/v1/videos/generations/{task_id}")
def get_video(task_id: str, _=Depends(auth)):
    t = store.get_task(task_id)
    if not t:
        raise HTTPException(404, "task 不存在")
    out = dict(t)
    status = out.get("status")
    if status in ("succeeded", "success", "done"):
        out["status"] = "completed"
    if out.get("status") == "completed":
        out["progress"] = 100
    elif out.get("status") == "processing":
        elapsed = time.time() - out.get("created_at", time.time())
        calc_prog = min(92, int(20 + elapsed * 1.1))
        out["progress"] = max(out.get("progress", 0) or 0, calc_prog)
    vurl = out.get("url")
    if not vurl and isinstance(out.get("result"), dict):
        vurl = out["result"].get("url")
    if vurl:
        out["url"] = vurl
        if "video" not in out:
            out["video"] = {"url": vurl}
    return out


# ------------------------- 对话（OpenAI 兼容） -------------------------
def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _chat_chunk(cid: str, created: int, model: str, delta: dict,
                finish: str | None = None) -> dict:
    return {"id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _want_usage(stream_options) -> bool:
    """下游（LangChain、部分 SDK / 智能体框架）会带
    `stream_options.include_usage=true`，要求在 [DONE] 之前补一个
    `choices: []` + `usage` 的分片。不发的话少数框架会一直等 usage 而卡住。"""
    if isinstance(stream_options, dict):
        return bool(stream_options.get("include_usage"))
    return False


def _pace_text(text: str, chunk_size: int = 2, delay: float = 0.012):
    """把大块文本平滑切分为仿原生 LLM 打字机的微流式 Token。
    若传入文本本身微小（<= 3 字），直接秒级放行，零延迟；
    若传入文本是大块（如 DOM 批量刷新），按每 chunk_size 字间隔 delay 平滑输出。
    """
    if not text:
        return
    if len(text) <= 3 or delay <= 0:
        yield text
        return
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]
        if i + chunk_size < len(text):
            time.sleep(delay)


_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, _=Depends(auth)):
    """OpenAI 兼容的对话接口 —— 各类智能体客户端都能接。

    说明：muse.ai 网页是**自动路由**的 agent，界面上没有模型选择器，所有请求
    都会落到同一个网页助手（自称 Koda，底层是 Muse 系列语言模型）。所以
    `model` 字段只用于兼容下游，不影响路由结果。

    请求带 `tools` 时，会注入【工具调用协议】并把模型输出的 JSON 解析回
    `tool_calls`（muse.ai 没有原生 function calling，这层属于协议适配）。

    对话与生图/生视频共用同一个浏览器实例，靠 `GEN_LOCK` 串行。
    """
    prompt = build_chat_prompt(req.messages) or (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "messages 为空")

    # 工具调用协议默认不注入 —— 实测 muse.ai 的助手会明确拒绝输出"伪工具调用"，
    # 注入反而污染正常回答；详见 config.py 的 tool_protocol 注释。
    tool_note = build_tools_prompt(req.tools) if CFG.tool_protocol else ""
    if tool_note:
        # 放在末尾：开头是 agent 自己的 system 提示，夹在中间容易被忽略；
        # 紧贴用户消息之前，模型对末尾指令的遵守度明显更高。
        prompt = prompt + "\n\n" + tool_note

    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True, preferred_id=getattr(engine, "current_acc_id", None))
    if not acc:
        raise HTTPException(400, "没有可用账号，请先在管理页面导入 cookie")

    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id = acc["id"]
    cookies = acc["cookies"]
    expires = acc.get("cookies_exp")

    def tool_call_deltas(calls: list[dict]) -> list[list[dict]]:
        """按 OpenAI 习惯分两片发：先 id/name，再 arguments 全文。"""
        head = [{"index": i, "id": c["id"], "type": "function",
                 "function": {"name": c["function"]["name"], "arguments": ""}}
                for i, c in enumerate(calls)]
        body = [{"index": i, "function": {"arguments": c["function"]["arguments"]}}
                for i, c in enumerate(calls)]
        return [head, body]

    if req.stream:
        def sync_stream():
            try:
                # 握手建立瞬间立即发送 role: assistant 首包，让下游客户端秒级捕获光标
                yield _sse(_chat_chunk(cid, created, model, {"role": "assistant"}))

                stream_gen = safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id)
                if not tool_note:
                    for chunk in stream_gen:
                        for piece in _pace_text(chunk):
                            yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                    yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                else:
                    buf, mode = "", None
                    for chunk in stream_gen:
                        if mode == "text":
                            for piece in _pace_text(chunk):
                                yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            continue
                        buf += chunk
                        if mode is None:
                            head = buf.lstrip()
                            if len(head) >= 2:
                                if head[0] in "{[" or head.startswith("```"):
                                    mode = "maybe_tool"
                                else:
                                    mode = "text"
                                    for piece in _pace_text(buf):
                                        yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                                    buf = ""
                    if mode == "text":
                        yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                    else:
                        calls, rest = parse_tool_calls(buf)
                        if calls:
                            if rest:
                                for piece in _pace_text(rest):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            for piece in tool_call_deltas(calls):
                                yield _sse(_chat_chunk(cid, created, model, {"tool_calls": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="tool_calls"))
                        else:
                            if buf:
                                for piece in _pace_text(buf):
                                    yield _sse(_chat_chunk(cid, created, model, {"content": piece}))
                            yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                if _want_usage(req.stream_options):
                    yield _sse({"id": cid, "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [],
                                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
                yield "data: [DONE]\n\n"
                store.mark(acc_id, True, "")
            except MuseAuthError as exc:
                store.mark(acc_id, False, str(exc))
                yield _sse({"error": {"message": str(exc), "type": "auth_error", "code": 401}})
            except MuseGenerationError as exc:
                store.mark(acc_id, True, f"助手超时: {str(exc)[:60]}")
                try:
                    engine.reset_thread()
                except Exception:
                    pass
                yield _sse({"error": {"message": str(exc), "type": "server_error", "code": 502}})
            except Exception as exc:
                yield _sse({"error": {"message": f"内部错误: {exc}", "type": "server_error", "code": 500}})
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if tool_note:
        calls, rest = parse_tool_calls(text)
        if calls:
            return {"id": cid, "object": "chat.completion", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "finish_reason": "tool_calls",
                                 "message": {"role": "assistant",
                                             "content": rest or None,
                                             "tool_calls": calls}}],
                    "usage": usage}
    return {"id": cid, "object": "chat.completion", "created": created,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": usage}


def _responses_messages(req: ResponsesRequest) -> list[ChatMessage]:
    """把 Responses API 的 instructions / input 归一成 messages。"""
    msgs: list[ChatMessage] = []
    if req.instructions:
        msgs.append(ChatMessage(role="system", content=req.instructions))
    inp = req.input
    if isinstance(inp, str):
        msgs.append(ChatMessage(role="user", content=inp))
        return msgs
    if isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                msgs.append(ChatMessage(role="user", content=item))
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if item.get("role"):                      # message 形态
                msgs.append(ChatMessage(role=item["role"],
                                        content=item.get("content")))
            elif itype == "input_text":
                msgs.append(ChatMessage(role="user", content=item.get("text")))
            elif itype == "function_call_output":
                msgs.append(ChatMessage(role="user", content="【工具执行结果】\n"
                                        + str(item.get("output") or "")))
            elif itype == "function_call":
                msgs.append(ChatMessage(role="assistant", content="【请求调用工具】"
                                        + str(item.get("name") or "")))
    return msgs


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/v1/responses")
async def responses_api(req: ResponsesRequest, _=Depends(auth)):
    """OpenAI Responses API —— 新版 Codex 默认走这个端点。

    只实现 Codex 实际用到的子集：文本进 → 文本出（含流式事件）。
    muse.ai 不会返回结构化的 tool_calls，关于工具调用的边界见 README。
    """
    prompt = build_chat_prompt(_responses_messages(req))
    if not prompt:
        raise HTTPException(400, "input 为空")

    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True, preferred_id=getattr(engine, "current_acc_id", None))
    if not acc:
        raise HTTPException(400, "没有可用账号，请先在管理页面导入 cookie")

    rid = "resp_" + uuid.uuid4().hex[:24]
    mid = "msg_" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id, cookies, expires = acc["id"], acc["cookies"], acc.get("cookies_exp")

    def envelope(status: str, text: str = "") -> dict:
        done = status == "completed"
        return {"id": rid, "object": "response", "created_at": created,
                "status": status, "model": model,
                "output": [{"type": "message", "id": mid, "role": "assistant",
                            "status": "completed" if done else "in_progress",
                            "content": ([{"type": "output_text", "text": text,
                                          "annotations": []}] if text else [])}],
                "usage": {"input_tokens": 0, "output_tokens": 0,
                          "total_tokens": 0}}

    if req.stream:
        def sync_stream():
            try:
                yield _sse_event("response.created", {
                    "type": "response.created",
                    "response": envelope("in_progress")})
                yield _sse_event("response.output_item.added", {
                    "type": "response.output_item.added", "output_index": 0,
                    "item": {"id": mid, "type": "message", "role": "assistant",
                             "status": "in_progress", "content": []}})
                yield _sse_event("response.content_part.added", {
                    "type": "response.content_part.added", "item_id": mid,
                    "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": "",
                             "annotations": []}})
                full = ""
                for chunk in safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id):
                    full += chunk
                    yield _sse_event("response.output_text.delta", {
                        "type": "response.output_text.delta", "item_id": mid,
                        "output_index": 0, "content_index": 0, "delta": chunk})
                yield _sse_event("response.output_text.done", {
                    "type": "response.output_text.done", "item_id": mid,
                    "output_index": 0, "content_index": 0, "text": full})
                yield _sse_event("response.output_item.done", {
                    "type": "response.output_item.done", "output_index": 0,
                    "item": {"id": mid, "type": "message", "role": "assistant",
                             "status": "completed",
                             "content": [{"type": "output_text", "text": full,
                                          "annotations": []}]}})
                yield _sse_event("response.completed", {
                    "type": "response.completed",
                    "response": envelope("completed", full)})
            except Exception as exc:  # noqa: BLE001
                log.warning("responses 流式失败: %s", exc)
                yield _sse_event("response.failed", {
                    "type": "response.failed",
                    "response": envelope("failed")})
        return StreamingResponse(sync_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    def run() -> str:
        return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))

    try:
        text = await asyncio.to_thread(run)
    except MuseAuthError as exc:
        raise HTTPException(401, str(exc))
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc))

    return envelope("completed", text)


@app.get("/v1/media/{name}")
def get_media(name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    return FileResponse(p)


# ------------------------- 管理：总览 -------------------------
@app.get("/admin/status")
def admin_status(_=Depends(admin_auth)):
    base = _public_base()
    return {
        "accounts": store.list_accounts(),
        "stats": store.stats(),
        "tasks": store.list_tasks(20),
        "media_count": len(os.listdir(CFG.media_dir))
        if os.path.isdir(CFG.media_dir) else 0,
        "browser_running": bool(engine.proc and engine.proc.poll() is None),
        "essential_cookies": list(ESSENTIAL_COOKIES),
        "base_url": f"{base}/v1" if base else "",
        "config": {"site": CFG.site_url, "cdp_port": CFG.cdp_port,
                   "image_timeout": CFG.image_timeout,
                   "video_timeout": CFG.video_timeout,
                   "host": CFG.host, "port": CFG.port,
                   "public_base": base},
    }


# ------------------------- 管理：账号池 -------------------------
@app.get("/admin/accounts")
def list_accounts(_=Depends(admin_auth)):
    return {
        "accounts": store.list_accounts(),
        "stats": store.stats(),
        "alive_count": store.get_alive_accounts_count()
    }


@app.post("/admin/accounts")
def add_account(req: AccountRequest, _=Depends(admin_auth)):
    added: list[dict] = []
    seen: list[dict] = []          # 本次导入的所有 cookie，用来检查核心项是否齐全

    if req.batch:
        for label, cookies in parse_batch(req.batch):
            acc = store.add_account(cookies, label)
            seen.append(cookies)
            added.append({"id": acc["id"], "label": acc["label"],
                          "cookie_count": len(cookies),
                          "expires_at": acc.get("expires_at")})

    cookies = dict(req.cookies)
    if req.cookie_header:
        cookies.update(parse_cookie_text(req.cookie_header))
    if cookies:
        acc = store.add_account(cookies, req.label, cookies_exp=req.expires)
        seen.append(cookies)
        added.append({"id": acc["id"], "label": acc["label"],
                      "cookie_count": len(cookies),
                      "expires_at": acc.get("expires_at")})

    if not added:
        raise HTTPException(400, "未解析到任何 cookie，请检查格式")

    # 只要有一个账号把核心 cookie 凑齐就算通过（批量时按整体判断）
    missing = [n for n in ESSENTIAL_COOKIES
               if not any(n in c for c in seen)]
    return {"added": added, "count": len(added),
            "essential_missing": missing,
            "warning": (f"缺少核心 cookie：{', '.join(missing)}，该账号可能无法生成"
                        if missing else "")}


@app.patch("/admin/accounts/{aid}")
def patch_account(aid: str, req: AccountPatch, _=Depends(admin_auth)):
    acc = store.update_account(aid, label=req.label, enabled=req.enabled)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return {k: v for k, v in acc.items() if k != "cookies"} | {
        "cookie_count": len(acc.get("cookies", {}))}


@app.delete("/admin/accounts/{aid}")
def del_account(aid: str, _=Depends(admin_auth)):
    ok = store.delete_account(aid)
    if not ok:
        raise HTTPException(404, "账号不存在")
    return {"deleted": True, "id": aid}


@app.post("/admin/accounts/{aid}/test")
async def test_account(aid: str, _=Depends(admin_auth)):
    """真实打开 muse.ai 验证该账号 cookie 是否仍可登录。"""
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if not acc.get("cookies"):
        raise HTTPException(400, "该账号没有 cookie")

    def _probe():
        with GEN_LOCK:
            try:
                engine.start()
                engine.refresh(acc["cookies"], acc.get("cookies_exp"))
                synced = _sync_cookies(aid)
                quota = None
                try:  # 顺带刷新额度；读不到不影响测试结论
                    quota = engine.quota(acc["cookies"],
                                         acc.get("cookies_exp"))
                    quota["checked_at"] = int(time.time())
                    store.update_account(aid, quota=quota)
                except Exception:  # noqa: BLE001
                    quota = None
                store.mark(aid, True, "Phiên hợp lệ · Sẵn sàng")
                return {"ok": True, "message": "Phiên hợp lệ, sẵn sàng tạo video",
                        "synced": synced, "quota": quota}
            except MuseAuthError as exc:
                store.mark(aid, False, str(exc)[:200])
                return {"ok": False, "message": str(exc)[:200]}
            except Exception as exc:  # noqa: BLE001
                store.touch_keepalive(aid, None, f"Chưa xác nhận kiểm tra: {str(exc)[:200]}")
                return {"ok": False, "message": str(exc)[:200]}

    res = await asyncio.to_thread(_probe)
    if not res["ok"]:
        return JSONResponse(res, status_code=200)
    return res


@app.post("/admin/accounts/{aid}/relogin")
async def relogin_account(aid: str, _=Depends(admin_auth)):
    return await test_account(aid, _)


@app.post("/admin/accounts/{aid}/cookies")
def update_cookies(aid: str, payload: dict = Body(...), _=Depends(admin_auth)):
    """更新某个账号的 cookie（用于会话过期后补新 cookie）。"""
    cookies = dict(payload.get("cookies") or {})
    if payload.get("cookie_header"):
        cookies.update(parse_cookie_text(payload["cookie_header"]))
    if not cookies:
        raise HTTPException(400, "未解析到 cookie")
    exp = {k: int(v) for k, v in (payload.get("expires") or {}).items()
           if _pos(v)}
    # 补入的是「全新会话」的 cookie，有效期估算锚点必须重置到当下，
    # 否则会沿用旧会话的锚点，把剩余天数算少。
    acc = store.update_account(aid, cookies=cookies, ok=None,
                               note="已更新 cookie",
                               expiry_anchor=int(time.time()),
                               cookies_exp=exp or None)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return {"ok": True, "id": aid, "cookie_count": len(cookies),
            "expires_at": acc.get("expires_at")}


@app.post("/admin/relogin")
def relogin(_=Depends(admin_auth)):
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号")
    try:
        engine.start()
        engine.refresh(acc["cookies"], acc.get("cookies_exp"))
        return {"ok": True, "account": acc["id"]}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


# ------------------------- 管理：额度 -------------------------
@app.post("/admin/accounts/{aid}/quota")
async def query_quota(aid: str, _=Depends(admin_auth)):
    """打开 muse.ai 的 Settings 面板读该账号的额度（实时），并缓存到账号记录。

    返回例：{"plan":"Free plan","weekly_reset":"Sep 30",
             "weekly_used_pct":1,"extra_left":"2B tokens left",
             "extra_used_pct":0,"extra_expires":"never"}
    """
    acc = store.get_account(aid)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if not acc.get("cookies"):
        raise HTTPException(400, "该账号没有 cookie")

    def _probe():
        with GEN_LOCK:
            engine.start()
            q = engine.quota(acc["cookies"], acc.get("cookies_exp"))
            q["checked_at"] = int(time.time())
            store.update_account(aid, quota=q)
            return q

    try:
        return await asyncio.to_thread(_probe)
    except MuseAuthError as exc:
        store.mark(aid, False, str(exc)[:200])
        raise HTTPException(401, str(exc)) from exc
    except MuseGenerationError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"额度查询失败: {exc}") from exc


@app.post("/admin/quota")
async def query_any_quota(_=Depends(admin_auth)):
    """用当前最久未用的可用账号查一次额度（同池账号共享同一 muse.ai 计划的
    通常只有一人使用时够用；多账号时建议按账号查）。"""
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号")
    return await query_quota(acc["id"], _)


# ------------------------- 管理：接入信息 / API Key -------------------------
def _public_base() -> str:
    return (CFG.public_base or "").rstrip("/")


@app.get("/admin/apikey")
def get_apikey(_=Depends(admin_auth)):
    base = _public_base()
    base_url = f"{base}/v1" if base else ""
    return {"api_key": CFG.api_key, "base_url": base_url,
            "models_url": f"{base}/v1/models" if base else "",
            "media_url": f"{base}/v1/media/{{name}}" if base else "/v1/media/{name}"}


@app.post("/admin/apikey/rotate")
def rotate_apikey(_=Depends(admin_auth)):
    """生成新的 API Key，写入 .env 并立即生效（不用重启）。"""
    import secrets
    new_key = "m2a_" + secrets.token_hex(24)
    old = CFG.api_key
    CFG.api_key = new_key
    _persist_env("MUSE2API_KEY", new_key)
    return {"ok": True, "api_key": new_key, "previous": old,
            "message": "已生成新 Key 并立即生效；旧 Key 已失效，请更新下游项目"}


def _persist_env(key: str, value: str):
    """把配置写回 .env（保留其它行，原子替换）。"""
    path = os.path.join(CFG.base_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    found = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            found = True
    if not found:
        lines.append(f"{key}={value}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    os.replace(tmp, path)


# ------------------------- 管理：Cookie 获取 -------------------------
@app.get("/admin/cookie-helper")
def cookie_helper(download: int = 0):
    """返回本机取 cookie 的助手脚本（进阶方式，需要装 Python）。"""
    p = os.path.join(BASE_DIR, "tools", "get_muse_cookie.py")
    if not os.path.isfile(p):
        raise HTTPException(404, "助手脚本缺失")
    headers = {}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="get_muse_cookie.py"'
    return FileResponse(p, media_type="text/x-python", headers=headers)


@app.get("/admin/extension")
def extension_zip():
    """把浏览器扩展打包成 zip 返回（推荐方式，零命令行）。

    用户下载后解压 → chrome://extensions 开发者模式加载 → 点一下就导入 cookie。
    """
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "扩展目录缺失")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(src)):
            p = os.path.join(src, name)
            if os.path.isfile(p):
                z.write(p, os.path.join("muse2api-extension", name))
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 'attachment; filename="muse2api-extension.zip"',
                 "Cache-Control": "no-store"})


@app.get("/admin/extension/files")
def extension_files():
    """列出扩展目录内容（前端展示用）。"""
    src = os.path.join(BASE_DIR, "extension")
    if not os.path.isdir(src):
        raise HTTPException(404, "扩展目录缺失")
    return {"files": sorted(f for f in os.listdir(src)
                            if os.path.isfile(os.path.join(src, f)))}


# ------------------------- 管理：任务 / 媒体 -------------------------
@app.get("/admin/tasks")
def admin_tasks(limit: int = 50, _=Depends(admin_auth)):
    return {"tasks": store.list_tasks(limit)}


@app.delete("/admin/tasks/{tid}")
def del_task(tid: str, _=Depends(admin_auth)):
    if not store.delete_task(tid):
        raise HTTPException(404, "任务不存在")
    return {"deleted": True}


@app.post("/admin/tasks/clear")
def clear_tasks(payload: dict = Body(default={}), _=Depends(admin_auth)):
    return {"removed": store.clear_tasks(int(payload.get("keep") or 0))}


@app.get("/admin/media")
def admin_media(_=Depends(admin_auth)):
    d = CFG.media_dir
    items = []
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(name)[1].lower()
            items.append({"name": name, "url": media_url(name), "bytes": os.path.getsize(p),
                          "mtime": int(os.path.getmtime(p)),
                          "kind": "video" if ext in (".mp4", ".webm", ".mov") else "image"})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"media": items, "count": len(items)}


@app.post("/admin/media/delete")
def delete_media(payload: dict = Body(default={}), _=Depends(admin_auth)):
    names = payload.get("names", [])
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        raise HTTPException(400, "names 必须是文件名数组")
    d = CFG.media_dir
    removed = 0
    errors = []
    for name in names:
        if not isinstance(name, str) or "/" in name or "\\" in name or ".." in name:
            errors.append(f"非法文件名: {name}")
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed += 1
            except OSError as e:
                errors.append(f"{name}: {str(e)}")
    return {"removed": removed, "errors": errors}


@app.delete("/admin/media/{name}")
def delete_single_media(name: str, _=Depends(admin_auth)):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    try:
        os.remove(p)
        return {"status": "ok", "deleted": name}
    except OSError as e:
        raise HTTPException(500, f"删除失败: {e}")


# ------------------------- Dọn media tự động (opt-in) -------------------------
def cleanup_media(max_age_days: int = 0, max_files: int = 0, dry_run: bool = False) -> dict:
    """Xóa bớt file trong data/media theo tuổi (ngày) và/hoặc số lượng tối đa.

    Giữ lại file mới nhất. max_age_days<=0 và max_files<=0 -> không làm gì (an toàn mặc định).
    Chỉ đụng file trong đúng thư mục media, bỏ qua .gitkeep. Không bao giờ làm hỏng tiến trình tạo video.
    """
    d = CFG.media_dir
    if not os.path.isdir(d) or (max_age_days <= 0 and max_files <= 0):
        return {"removed": 0, "removed_files": [], "kept": None, "dry_run": dry_run}
    files = []
    for name in os.listdir(d):
        if name == ".gitkeep":
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p):
            try:
                files.append((name, os.path.getmtime(p), os.path.getsize(p)))
            except OSError:
                pass
    files.sort(key=lambda x: x[1], reverse=True)   # mới nhất trước

    now = time.time()
    to_remove = set()
    if max_age_days > 0:
        cutoff = now - max_age_days * 86400
        to_remove |= {f[0] for f in files if f[1] < cutoff}
    if max_files > 0 and len(files) > max_files:
        to_remove |= {f[0] for f in files[max_files:]}   # phần dôi ra ngoài N file mới nhất

    removed, errors, freed = [], [], 0
    for name in to_remove:
        p = os.path.join(d, name)
        size = next((f[2] for f in files if f[0] == name), 0)
        if dry_run:
            removed.append(name)
            freed += size
            continue
        try:
            os.remove(p)
            removed.append(name)
            freed += size
        except OSError as e:
            errors.append(f"{name}: {e}")
    if removed:
        log.info("🧹 Dọn media: %s %d file (giải phóng ~%.1f MB), còn giữ %d file.",
                 "sẽ xóa" if dry_run else "đã xóa", len(removed), freed / 1048576, len(files) - len(removed))
    return {"removed": len(removed), "removed_files": sorted(removed), "errors": errors,
            "freed_bytes": freed, "kept": len(files) - len(removed), "dry_run": dry_run}


@app.post("/admin/media/cleanup")
def admin_media_cleanup(payload: dict = Body(default={}), _=Depends(admin_auth)):
    """Dọn thủ công thư mục media. Body (tùy chọn): max_age_days, max_files, dry_run.
    Không truyền thì dùng cấu hình MUSE2API_MEDIA_RETENTION_DAYS / MUSE2API_MEDIA_MAX_FILES."""
    age = payload.get("max_age_days")
    cnt = payload.get("max_files")
    return cleanup_media(
        max_age_days=int(age) if age is not None else CFG.media_retention_days,
        max_files=int(cnt) if cnt is not None else CFG.media_max_files,
        dry_run=bool(payload.get("dry_run", False)),
    )


# ------------------------- 前端页面 -------------------------
def _admin_html() -> str:
    p = os.path.join(BASE_DIR, "admin.html")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ("<!doctype html><meta charset=utf-8><body style='background:#0b0f19;"
                "color:#e6e8ee;font-family:system-ui;padding:40px'>"
                "<h2>muse2api</h2><p>管理页面文件 admin.html 缺失。</p>"
                "<p>接口可用：<code>/v1/images/generations</code>、"
                "<code>/v1/videos</code></p></body>")


def _studio_html() -> str:
    p = os.path.join(BASE_DIR, "studio.html")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return _admin_html()


@app.get("/", response_class=HTMLResponse)
def index():
    return _studio_html()


@app.get("/studio", response_class=HTMLResponse)
def studio_page():
    return _studio_html()


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return _admin_html()


_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
# Các key từng được ghi cứng trong source / file mẫu -> coi như đã lộ.
_PUBLIC_DEFAULT_KEYS = {"m2a_museai_video_studio_2026", "m2a_your_secret_admin_key_here"}


def _is_local_same_origin(request: Request) -> bool:
    """Chỉ trang Studio do chính server này phục vụ (truy cập qua loopback, cùng origin) mới được nhận API key.

    CORS đang mở "*", nên nếu không chặn thì bất kỳ trang web nào bạn mở trong trình duyệt
    cũng fetch được http://127.0.0.1:18610/api/studio/config và đọc trộm key.
    """
    client = request.client.host if request.client else ""
    if client not in _LOOPBACK_HOSTS:
        return False
    host_header = (request.headers.get("host") or "").lower()
    hostname = host_header.rsplit(":", 1)[0].strip("[]") if host_header else ""
    if hostname not in _LOOPBACK_HOSTS:          # chặn DNS rebinding
        return False
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/").lower() != f"http://{host_header}":
        return False
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        return False
    return True


@app.get("/api/studio/config")
def get_studio_config(request: Request):
    cfg = {
        "media_dir": CFG.media_dir,
        "chromium": CFG.chromium,
        "host": CFG.host,
        "port": CFG.port,
        "version": "1.0.0"
    }
    if _is_local_same_origin(request):
        cfg["api_key"] = CFG.api_key
    return cfg


# ------------------------- Shopee Database & Video Batch API -------------------------
import shopee_engine

SHOPEE_SETTINGS_FILE = os.path.join(CFG.data_dir, "shopee_settings.json")

_VIDEO_EXTS = (".mp4", ".webm", ".mov")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _bare_filename(name, field: str) -> str:
    """Chỉ nhận tên file trần (không thư mục, không ..) để không ghi/đọc ra ngoài thư mục chỉ định."""
    s = str(name or "").strip()
    if not s or s != os.path.basename(s) or s in (".", "..") or "/" in s or "\\" in s:
        raise HTTPException(400, f"{field} không hợp lệ: {s!r}")
    return s


def _media_file(name, field: str = "filename") -> str:
    """File nằm trong thư mục media của server."""
    p = os.path.join(CFG.media_dir, _bare_filename(name, field))
    if not os.path.isfile(p):
        raise HTTPException(404, f"Không tìm thấy file {os.path.basename(p)}")
    return p


def _video_source(path_or_name, field: str = "filename") -> str:
    """Nguồn video: tên file trong thư mục media, hoặc đường dẫn tuyệt đối tới file video
    (clip tạm đã lưu ở <out_dir>/temp). Chặn đọc các loại file khác trên máy."""
    s = str(path_or_name or "").strip()
    if not os.path.isabs(s):
        return _media_file(s, field)
    if not s.lower().endswith(_VIDEO_EXTS):
        raise HTTPException(400, f"{field} phải là file video: {os.path.basename(s)}")
    if not os.path.isfile(s):
        raise HTTPException(404, f"Không tìm thấy file {os.path.basename(s)}")
    return s


def _get_shopee_settings() -> dict:
    default_settings = {
        "sv_server_url": shopee_engine.DEFAULT_SERVER_URL,
        "sv_api_key": shopee_engine.DEFAULT_API_KEY,
        "sv_client_id": shopee_engine.DEFAULT_CLIENT_ID,
        "aspect": "Dọc 9:16 (TikTok)",
        "scene": "🎲 Random",
        "duration": "8s",
        "lang": "Tiếng Philippines",
        "review_style": "Unboxing",
        "ai_prompt": "Template (mặc định)",
        "del_img": True,
        "ghep_anh": True,
        "naming": "Theo Item ID",
        "out_dir": shopee_engine.DEFAULT_OUT_DIR,
        "claim_limit": "2000",
        "sort_by": "Số bán cao nhất",
        "market": "PH",
        "min_item_id": "40000000000",
        "min_commission": "1",
        "min_sold": "0",
        "min_price": "0",
        "max_price": ""
    }
    if os.path.isfile(SHOPEE_SETTINGS_FILE):
        try:
            with open(SHOPEE_SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                default_settings.update(saved)
        except Exception:
            pass
    return default_settings


@app.get("/api/shopee/settings")
def get_shopee_settings():
    return _get_shopee_settings()


@app.post("/api/shopee/save-settings")
def save_shopee_settings(settings: dict):
    os.makedirs(CFG.data_dir, exist_ok=True)
    with open(SHOPEE_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    return {"success": True}


def _update_env_file(env_path: str, updates: dict[str, str]):
    lines = []
    found_keys = set()
    if os.path.isfile(env_path):
        try:
            with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and "=" in stripped:
                        k = stripped.split("=", 1)[0].strip()
                        if k in updates and updates[k] is not None:
                            lines.append(f"{k}={updates[k]}\n")
                            found_keys.add(k)
                            continue
                    lines.append(line)
        except Exception as e:
            log.warning("Không đọc được file .env: %s", e)
    for k, v in updates.items():
        if k not in found_keys and v is not None:
            lines.append(f"{k}={v}\n")
    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        log.warning("Không ghi được file .env: %s", e)


@app.get("/api/shopee/db-config")
def get_shopee_db_config():
    cfg = _get_shopee_settings()
    server_url = os.environ.get("SHOPEE_SERVER_URL", "").strip() or cfg.get("sv_server_url") or shopee_engine.DEFAULT_SERVER_URL
    api_key = os.environ.get("SHOPEE_API_KEY", "").strip() or cfg.get("sv_api_key") or shopee_engine.DEFAULT_API_KEY
    client_id = os.environ.get("SHOPEE_CLIENT_ID", "").strip() or cfg.get("sv_client_id") or shopee_engine.DEFAULT_CLIENT_ID
    return {
        "server_url": server_url,
        "api_key": api_key,
        "client_id": client_id
    }


@app.post("/api/shopee/test-db-connection")
def test_shopee_db_connection(data: dict):
    server_url = (data.get("server_url") or "").strip().rstrip("/")
    if not server_url:
        return {"success": False, "message": "Server URL không được để trống"}
    if not (server_url.startswith("http://") or server_url.startswith("https://")):
        server_url = "http://" + server_url

    # 1. Thử HTTP Request
    try:
        import urllib.request
        import urllib.error
        test_endpoint = f"{server_url}/health" if not server_url.endswith((":3000", "/api")) else server_url
        req = urllib.request.Request(
            test_endpoint,
            headers={"User-Agent": "MuseAI/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                return {"success": True, "message": f"Kết nối HTTP thành công (HTTP {resp.status})"}
        except urllib.error.HTTPError as he:
            return {"success": True, "message": f"Máy chủ phản hồi tốt (HTTP {he.code})"}
    except Exception:
        pass

    # 2. Thử TCP Socket Connect
    try:
        import urllib.parse
        import socket
        p = urllib.parse.urlsplit(server_url)
        host = p.hostname
        port = p.port or (443 if p.scheme == "https" else 80)
        if host:
            with socket.create_connection((host, port), timeout=4):
                return {"success": True, "message": f"Cổng {port} mở & kết nối mạng TCP thành công"}
    except Exception as exc:
        return {"success": False, "message": f"Không thể kết nối tới {server_url}: {exc}"}

    return {"success": False, "message": "Không thể kết nối tới máy chủ Database"}


@app.post("/api/shopee/save-db-config")
def save_shopee_db_config(data: dict):
    server_url = (data.get("server_url") or "").strip().rstrip("/")
    api_key = (data.get("api_key") or "").strip()
    client_id = (data.get("client_id") or "").strip()

    if not server_url:
        raise HTTPException(400, "Địa chỉ Server URL không được để trống")

    # 1. Cập nhật biến môi trường runtime
    os.environ["SHOPEE_SERVER_URL"] = server_url
    if api_key:
        os.environ["SHOPEE_API_KEY"] = api_key
    if client_id:
        os.environ["SHOPEE_CLIENT_ID"] = client_id

    # 2. Cập nhật module shopee_engine runtime
    shopee_engine.DEFAULT_SERVER_URL = server_url
    if api_key:
        shopee_engine.DEFAULT_API_KEY = api_key
    if client_id:
        shopee_engine.DEFAULT_CLIENT_ID = client_id

    # 3. Ghi vào file .env để lưu vĩnh viễn qua các lần khởi động lại
    env_file = os.path.join(CFG.base_dir, ".env")
    _update_env_file(env_file, {
        "SHOPEE_SERVER_URL": server_url,
        "SHOPEE_API_KEY": api_key,
        "SHOPEE_CLIENT_ID": client_id
    })

    # 4. Ghi vào data/shopee_settings.json
    try:
        cur_settings = _get_shopee_settings()
        cur_settings["sv_server_url"] = server_url
        if api_key:
            cur_settings["sv_api_key"] = api_key
        if client_id:
            cur_settings["sv_client_id"] = client_id
        with open(SHOPEE_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(cur_settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Lỗi ghi shopee_settings.json: %s", e)

    return {
        "success": True,
        "server_url": server_url,
        "client_id": client_id,
        "message": "Đã lưu cấu hình Database thành công!"
    }


@app.post("/api/shopee/claim")
def claim_shopee_jobs(req: dict):
    cfg = _get_shopee_settings()
    server_url = req.get("server_url") or cfg.get("sv_server_url") or shopee_engine.DEFAULT_SERVER_URL
    api_key = req.get("api_key") or cfg.get("sv_api_key") or shopee_engine.DEFAULT_API_KEY
    client_id = req.get("client_id") or cfg.get("sv_client_id") or shopee_engine.DEFAULT_CLIENT_ID
    if not server_url or not api_key:
        raise HTTPException(400, "Chưa cấu hình Database Novagate/Shopee. Hãy đặt SHOPEE_SERVER_URL và "
                                 "SHOPEE_API_KEY trong file .env (hoặc nhập Server/API key ở tab Shopee) rồi thử lại.")
    market = req.get("market") or "PH"
    limit = int(req.get("limit") or 2000)
    sort_by_ui = req.get("sort_by") or "Số bán cao nhất"
    sort_by = "commission" if "Hoa hồng" in sort_by_ui else "sold"
    min_item_id = int(req.get("min_item_id") or 40000000000)
    min_commission = float(req.get("min_commission") or 1.0)
    min_sold = int(req.get("min_sold") or 0)
    min_price = float(req.get("min_price") or 0.0)
    max_price = float(req.get("max_price")) if req.get("max_price") else None

    try:
        products, bl_count = shopee_engine.claim_jobs_from_server(
            server_url=server_url,
            api_key=api_key,
            client_id=client_id,
            market=market,
            limit=limit,
            sort_by=sort_by,
            min_item_id=min_item_id,
            min_commission=min_commission,
            min_sold=min_sold,
            min_price=min_price,
            max_price=max_price,
            return_stats=True
        )
        return {"success": True, "count": len(products), "products": products, "blacklisted_count": bl_count}
    except Exception as e:
        raise HTTPException(500, f"Lỗi lấy sản phẩm từ Database: {e}")


@app.post("/api/shopee/release")
def release_shopee_jobs(req: dict):
    cfg = _get_shopee_settings()
    server_url = req.get("server_url") or cfg.get("sv_server_url") or shopee_engine.DEFAULT_SERVER_URL
    api_key = req.get("api_key") or cfg.get("sv_api_key") or shopee_engine.DEFAULT_API_KEY
    client_id = req.get("client_id") or cfg.get("sv_client_id") or shopee_engine.DEFAULT_CLIENT_ID
    try:
        released = shopee_engine.release_stuck_jobs(server_url, api_key, client_id)
        return {"success": True, "released": released}
    except Exception as e:
        raise HTTPException(500, f"Lỗi giải phóng SP: {e}")


@app.post("/api/shopee/complete")
def complete_shopee_job(req: dict):
    cfg = _get_shopee_settings()
    server_url = req.get("server_url") or cfg.get("sv_server_url") or shopee_engine.DEFAULT_SERVER_URL
    api_key = req.get("api_key") or cfg.get("sv_api_key") or shopee_engine.DEFAULT_API_KEY
    item_id = req.get("item_id", "")
    status = req.get("status", "completed")
    video_path = req.get("video_path")
    res = shopee_engine.report_job_completion(server_url, api_key, item_id, status, video_path)
    return {"success": res}


@app.post("/api/shopee/import-links")
def import_shopee_links(req: dict):
    """Nhập danh sách link Shopee từ nội dung file text hoặc mảng link."""
    import shopee_scraper

    content = req.get("content", "")
    file_path = req.get("file_path", "")
    raw_links = req.get("links", [])
    prefetch = req.get("prefetch", True)

    if file_path and os.path.isfile(file_path):
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            raise HTTPException(400, f"Không đọc được tệp: {e}")

    all_links: list[str] = []
    if content:
        all_links.extend(shopee_scraper.parse_shopee_links(content))
    if raw_links:
        for u in raw_links:
            all_links.extend(shopee_scraper.parse_shopee_links(str(u)))

    # Loại bỏ trùng lặp giữ nguyên thứ tự
    seen = set()
    unique_links: list[str] = []
    for u in all_links:
        if u not in seen:
            seen.add(u)
            unique_links.append(u)

    if not unique_links:
        return {
            "success": False,
            "message": "Không tìm thấy link Shopee hợp lệ nào trong nội dung nhập vào.",
            "count": 0,
            "products": []
        }

    if prefetch:
        products = shopee_scraper.scrape_multiple_shopee_links(unique_links, max_workers=6)
    else:
        products = []
        for link in unique_links:
            _, iid = shopee_scraper.extract_ids_from_url(link)
            products.append({
                "item_id": iid or str(abs(hash(link)) % 10000000000),
                "name": f"Shopee Link ({link[-25:]})",
                "image": "",
                "images": [],
                "url": link,
                "_status": "waiting",
                "_need_fetch": True,
                "_is_imported": True
            })

    return {
        "success": True,
        "count": len(products),
        "products": products,
        "message": f"Đã nạp thành công {len(products)} link sản phẩm Shopee."
    }


@app.post("/api/shopee/fetch-single-link")
def fetch_single_shopee_link(req: dict):
    """Cào thông tin tiêu đề, ảnh từ 1 link Shopee đơn lẻ."""
    import shopee_scraper
    url = (req.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "Thiếu URL sản phẩm Shopee")
    info = shopee_scraper.fetch_shopee_product(url)
    if not info:
        raise HTTPException(500, f"Không thể lấy thông tin sản phẩm từ Shopee URL: {url}")
    return {"success": True, "product": info}


def parse_shopee_lang_code(lang_val: str, market: str = "") -> str:
    s = str(lang_val or "").strip().lower()
    if "việt" in s or "viet" in s:
        return "vi"
    if "philippines" in s or "filipino" in s or "tagalog" in s:
        return "ph"
    if "thái" in s or "thai" in s:
        return "th"
    if "indonesia" in s or "indo" in s:
        return "id"
    if "malay" in s:
        return "my"
    if "trung" in s or "đài loan" in s or "taiwan" in s or "chinese" in s:
        return "tw"
    if "singapore" in s:
        return "sg"
    if "anh" in s or "english" in s:
        return "sg" if str(market or "").upper() == "SG" else "en"
    m = str(market or "").strip().upper()
    if m == "VN": return "vi"
    if m == "PH": return "ph"
    if m == "TH": return "th"
    if m == "ID": return "id"
    if m == "MY": return "my"
    if m == "TW": return "tw"
    if m == "SG": return "sg"
    return "ph"


@app.post("/api/shopee/test-prompt")
def test_shopee_prompt(req: dict):
    product_name = req.get("product_name", "Tecno Pova 6 Neo Phone Case")
    lang_val = req.get("lang", "Tiếng Philippines")
    market = req.get("market", "")
    lang_code = parse_shopee_lang_code(lang_val, market)
    review_style = req.get("review_style", "Unboxing")
    duration = str(req.get("duration", "8s")).strip().lower()
    ai_prompt = str(req.get("ai_prompt", "")).strip()

    is_16s = "16" in duration or "a + b" in ai_prompt.lower()
    clean_title = shopee_engine.clean_product_title(product_name)

    if is_16s:
        scene = req.get("scene", "🎲 Random")
        prompts, label = shopee_engine.build_video_prompts_16s(product_name, scene_choice=scene, lang=lang_code, review_style=review_style)
        full_text = f"[Clip A (0-8s)]:\n{prompts[0]}\n\n[Clip B (8-16s)]:\n{prompts[1]}"
        return {
            "prompt": full_text,
            "prompts": prompts,
            "label": f"{label} (16s)",
            "clean_title": clean_title,
            "is_16s": True
        }
    else:
        prompt, label = shopee_engine.build_tvc_prompt(product_name, lang=lang_code, review_style=review_style)
        return {"prompt": prompt, "prompts": [prompt], "label": label, "clean_title": clean_title, "is_16s": False}


@app.post("/api/shopee/concat-videos")
def concat_shopee_videos(req: dict):
    """Ghép danh sách các clip (ví dụ Clip A + Clip B) thành 1 video hoàn chỉnh."""
    clip_filenames = req.get("clip_filenames") or []
    out_filename = req.get("out_filename") or f"concat_{uuid.uuid4().hex}.mp4"
    if not clip_filenames:
        raise HTTPException(400, "Danh sách clip trống")

    clip_paths = [_video_source(f, "clip_filenames") for f in clip_filenames]
    out_filename = _bare_filename(out_filename, "out_filename")
    if not out_filename.lower().endswith(".mp4"):
        raise HTTPException(400, "out_filename phải có đuôi .mp4")

    out_path = os.path.join(CFG.media_dir, out_filename)
    ok = shopee_engine.concat_videos(clip_paths, out_path)
    if not ok:
        raise HTTPException(500, "Lỗi ghép video bằng FFmpeg")
    return {"success": True, "filename": out_filename, "path": out_path}


@app.get("/api/shopee/clip-cache/{item_id}")
def get_shopee_clip_cache(item_id: str):
    """Lấy danh sách các clip A / B đã render thành công của sản phẩm."""
    return shopee_engine.get_cached_clips(item_id)


@app.post("/api/shopee/clip-cache/{item_id}")
def set_shopee_clip_cache(item_id: str, req: dict):
    """Lưu lại tiến độ render clip A / B của sản phẩm để không phải tạo lại."""
    clip_key = req.get("clip_key") or "clip_a"
    filename = req.get("filename") or ""
    if filename:
        shopee_engine.set_cached_clip(item_id, clip_key, filename)
    return {"success": True}


@app.post("/api/shopee/save-temp-clip")
def save_temp_clip(req: dict):
    """Lưu clip video thô từ Muse.ai vào thư mục output_dir/temp/{item_id}-{part}.mp4."""
    filename = str(req.get("filename", "")).strip()
    out_dir = req.get("out_dir") or shopee_engine.DEFAULT_OUT_DIR
    item_id = str(req.get("item_id", "")).strip()
    part = str(req.get("part", "a")).lower().strip()  # "a", "b", "raw"
    if not filename or not item_id:
        raise HTTPException(400, "Thiếu thông tin filename hoặc item_id")
    if not _SAFE_ID_RE.match(item_id):
        raise HTTPException(400, f"item_id không hợp lệ: {item_id}")
    if part not in ("a", "b", "raw"):
        raise HTTPException(400, "part phải là a, b hoặc raw")

    src_path = _video_source(filename)

    temp_dir = os.path.join(out_dir, "temp")
    os.makedirs(temp_dir, exist_ok=True)
    target_filename = f"{item_id}-{part}.mp4"
    target_path = os.path.join(temp_dir, target_filename)

    # Chuyển trực tiếp file sang out_dir/temp/{item_id}-{part}.mp4 để không chiếm gấp đôi dung lượng ổ cứng
    try:
        shutil.move(src_path, target_path)
    except Exception:
        shutil.copy2(src_path, target_path)
        try:
            os.remove(src_path)
        except Exception:
            pass

    shopee_engine.set_cached_clip(item_id, f"clip_{part}", target_path)
    log.info("💾 [Temp Clip] Đã lưu clip thành phần: temp/%s (%s)", target_filename, target_path)
    return {"success": True, "path": target_path, "filename": target_filename}


def _inspect_product_clips(target_out: str, item_id: str) -> dict:
    clean_id = str(item_id or "").strip()
    res = {
        "clip_a": None,
        "clip_b": None,
        "clip_raw": None,
        "final_video": None,
        "has_a": False,
        "has_b": False,
        "has_both": False,
        "has_final": False,
        "temp_dir": "",
    }
    if not clean_id:
        return res

    saved_cfg = _get_shopee_settings()
    if not target_out:
        target_out = saved_cfg.get("out_dir") or shopee_engine.DEFAULT_OUT_DIR
    target_out = os.path.normpath(str(target_out).strip())
    temp_dir = os.path.join(target_out, "temp")
    res["temp_dir"] = temp_dir

    candidate_temp_dirs = []
    if os.path.isdir(temp_dir):
        candidate_temp_dirs.append(temp_dir)
    def_temp = os.path.join(shopee_engine.DEFAULT_OUT_DIR, "temp")
    if os.path.isdir(def_temp) and def_temp not in candidate_temp_dirs:
        candidate_temp_dirs.append(def_temp)

    clean_id_low = clean_id.lower()

    # 1. Quét tìm clip_a, clip_b trong các folder temp
    for tdir in candidate_temp_dirs:
        try:
            files = os.listdir(tdir)
        except OSError:
            continue
        for fname in files:
            if not fname.lower().endswith((".mp4", ".mov", ".webm")):
                continue
            lower = fname.lower()
            fpath = os.path.join(tdir, fname)
            try:
                if os.path.getsize(fpath) < 10000:
                    continue
            except OSError:
                continue

            # Clip A (-a hoặc _a)
            if not res["clip_a"]:
                if (lower == f"{clean_id_low}-a.mp4" or lower == f"{clean_id_low}_a.mp4" or
                    lower.startswith(f"{clean_id_low}-a.") or lower.startswith(f"{clean_id_low}_a.")):
                    res["clip_a"] = fpath
                    res["has_a"] = True

            # Clip B (-b hoặc _b)
            if not res["clip_b"]:
                if (lower == f"{clean_id_low}-b.mp4" or lower == f"{clean_id_low}_b.mp4" or
                    lower.startswith(f"{clean_id_low}-b.") or lower.startswith(f"{clean_id_low}_b.")):
                    res["clip_b"] = fpath
                    res["has_b"] = True

            # Clip raw (8s)
            if not res["clip_raw"]:
                if (lower == f"{clean_id_low}-raw.mp4" or lower == f"{clean_id_low}_raw.mp4" or
                    lower.startswith(f"{clean_id_low}-raw.") or lower.startswith(f"{clean_id_low}_raw.")):
                    res["clip_raw"] = fpath

    # 2. Fallback kiểm tra clip cache file (clip_cache.json)
    if not res["clip_a"] or not res["clip_b"]:
        try:
            cached = shopee_engine.get_cached_clips(clean_id)
            if not res["clip_a"] and cached.get("clip_a"):
                res["clip_a"] = cached["clip_a"]
                res["has_a"] = True
            if not res["clip_b"] and cached.get("clip_b"):
                res["clip_b"] = cached["clip_b"]
                res["has_b"] = True
        except Exception:
            pass

    res["has_both"] = bool(res["clip_a"] and res["clip_b"])

    # 3. Quét kiểm tra video hoàn chỉnh trong target_out
    candidate_out_dirs = [target_out] if os.path.isdir(target_out) else []
    if os.path.isdir(shopee_engine.DEFAULT_OUT_DIR) and shopee_engine.DEFAULT_OUT_DIR not in candidate_out_dirs:
        candidate_out_dirs.append(shopee_engine.DEFAULT_OUT_DIR)

    for od in candidate_out_dirs:
        try:
            files = os.listdir(od)
        except OSError:
            continue
        for fname in files:
            if not fname.lower().endswith((".mp4", ".mov", ".webm")):
                continue
            lower = fname.lower()
            if (lower == f"{clean_id_low}.mp4" or
                lower.startswith(f"{clean_id_low}.") or
                f"_{clean_id_low}." in lower or
                f"-{clean_id_low}." in lower or
                lower.endswith(f"_{clean_id_low}.mp4") or
                lower.endswith(f"-{clean_id_low}.mp4")):
                fpath = os.path.join(od, fname)
                try:
                    if os.path.getsize(fpath) >= 50000:
                        res["final_video"] = fpath
                        res["has_final"] = True
                        break
                except OSError:
                    continue
        if res["has_final"]:
            break

    return res


@app.get("/api/shopee/check-temp-clips")
def check_temp_clips(out_dir: str = "", item_id: str = ""):
    """Kiểm tra xem các clip thô {item_id}-a.mp4 và {item_id}-b.mp4 hoặc video hoàn chỉnh đã có sẵn chưa."""
    saved_cfg = _get_shopee_settings()
    target_out = (out_dir or "").strip() or saved_cfg.get("out_dir") or shopee_engine.DEFAULT_OUT_DIR
    target_out = os.path.normpath(target_out)
    return _inspect_product_clips(target_out, item_id)


@app.post("/api/shopee/batch-check-clips")
def batch_check_shopee_clips(req: dict):
    """Kiểm tra nhanh hàng loạt sản phẩm xem đã có clip -a, -b hoặc video hoàn chỉnh chưa."""
    saved_cfg = _get_shopee_settings()
    out_dir = (req.get("out_dir") or "").strip() or saved_cfg.get("out_dir") or shopee_engine.DEFAULT_OUT_DIR
    out_dir = os.path.normpath(out_dir)
    item_ids = req.get("item_ids") or []
    results = {}
    for iid in item_ids:
        sid = str(iid).strip()
        if sid:
            results[sid] = _inspect_product_clips(out_dir, sid)
    return {"success": True, "results": results}


@app.get("/api/shopee/temp-stitch-summary")
def get_temp_stitch_summary(out_dir: str = ""):
    """Thống kê các clip trong thư mục temp: có bao nhiêu cặp -a, -b và bao nhiêu cặp chưa ghép thành 16s."""
    saved_cfg = _get_shopee_settings()
    target_out = (out_dir or "").strip() or saved_cfg.get("out_dir") or shopee_engine.DEFAULT_OUT_DIR
    target_out = os.path.normpath(target_out)
    temp_dir = os.path.join(target_out, "temp")

    if not os.path.isdir(temp_dir):
        return {
            "success": True,
            "out_dir": target_out,
            "temp_dir": temp_dir,
            "total_temp_files": 0,
            "total_pairs": 0,
            "already_stitched": 0,
            "waiting_to_stitch": 0,
            "waiting_item_ids": []
        }

    try:
        temp_files = os.listdir(temp_dir)
    except OSError:
        temp_files = []

    # Gom nhóm theo item_id
    clips_a = {}
    clips_b = {}
    for fname in temp_files:
        if not fname.lower().endswith((".mp4", ".mov", ".webm")):
            continue
        fpath = os.path.join(temp_dir, fname)
        try:
            sz = os.path.getsize(fpath)
            if sz < 10000:
                continue
        except OSError:
            continue

        lower = fname.lower()
        m_a = re.match(r"^(\d+)[-_]a\.", lower)
        if m_a:
            clips_a[m_a.group(1)] = fpath
            continue
        m_b = re.match(r"^(\d+)[-_]b\.", lower)
        if m_b:
            clips_b[m_b.group(1)] = fpath
            continue

    paired_ids = set(clips_a.keys()) & set(clips_b.keys())

    # Quét thư mục xuất video đích xem video 16s nào đã có
    try:
        out_files = os.listdir(target_out)
    except OSError:
        out_files = []

    final_ids = set()
    for of in out_files:
        if not of.lower().endswith((".mp4", ".mov", ".webm")):
            continue
        of_path = os.path.join(target_out, of)
        try:
            if os.path.getsize(of_path) < 50000:
                continue
        except OSError:
            continue
        of_low = of.lower()
        # Tìm item_id trong tên file video đích
        m_id = re.search(r"(\d{8,})", of_low)
        if m_id:
            final_ids.add(m_id.group(1))

    waiting_ids = [iid for iid in paired_ids if iid not in final_ids]

    return {
        "success": True,
        "out_dir": target_out,
        "temp_dir": temp_dir,
        "total_temp_files": len(temp_files),
        "total_pairs": len(paired_ids),
        "already_stitched": len(paired_ids) - len(waiting_ids),
        "waiting_to_stitch": len(waiting_ids),
        "waiting_item_ids": sorted(waiting_ids)
    }


@app.post("/api/shopee/auto-stitch-temp")
def auto_stitch_temp_clips(req: dict):
    """Tự động quét thư mục temp, tìm tất cả cặp clip -a và -b chưa ghép và nối thành video 16s qua FFmpeg."""
    saved_cfg = _get_shopee_settings()
    out_dir = (req.get("out_dir") or "").strip() or saved_cfg.get("out_dir") or shopee_engine.DEFAULT_OUT_DIR
    out_dir = os.path.normpath(out_dir)
    temp_dir = os.path.join(out_dir, "temp")

    if not os.path.isdir(temp_dir):
        return {"success": False, "message": f"Thư mục temp không tồn tại: {temp_dir}"}

    summary = get_temp_stitch_summary(out_dir)
    waiting_ids = summary.get("waiting_item_ids") or []
    if not waiting_ids:
        return {
            "success": True,
            "message": "Không có cặp clip A/B nào cần ghép trong temp (tất cả đã được ghép hoặc chưa đủ cặp).",
            "stitched_count": 0,
            "already_count": summary.get("already_stitched", 0)
        }

    log.info("⚡ [Auto Stitch] Bắt đầu ghép tự động %d cặp clip trong %s...", len(waiting_ids), temp_dir)
    stitched_items = []
    failed_items = []

    delete_temp = bool(req.get("delete_temp", True))

    for iid in waiting_ids:
        # Tìm file clip_a và clip_b
        f_a = None
        f_b = None
        for ext in (".mp4", ".mov", ".webm"):
            cand_a = os.path.join(temp_dir, f"{iid}-a{ext}")
            if not os.path.isfile(cand_a):
                cand_a = os.path.join(temp_dir, f"{iid}_a{ext}")
            if os.path.isfile(cand_a) and os.path.getsize(cand_a) >= 10000:
                f_a = cand_a

            cand_b = os.path.join(temp_dir, f"{iid}-b{ext}")
            if not os.path.isfile(cand_b):
                cand_b = os.path.join(temp_dir, f"{iid}_b{ext}")
            if os.path.isfile(cand_b) and os.path.getsize(cand_b) >= 10000:
                f_b = cand_b

        if not f_a or not f_b:
            failed_items.append({"item_id": iid, "reason": "Không tìm thấy file Clip A hoặc Clip B hợp lệ"})
            continue

        target_name = f"{iid}.mp4"
        out_final = os.path.join(out_dir, target_name)

        try:
            ok = shopee_engine.concat_videos([f_a, f_b], out_final)
            if ok and os.path.isfile(out_final) and os.path.getsize(out_final) >= 50000:
                stitched_items.append({
                    "item_id": iid,
                    "filename": target_name,
                    "path": out_final,
                    "size": os.path.getsize(out_final)
                })
                log.info("✓ [Auto Stitch] Ghép thành công 16s: %s (%.1f MB)", target_name, os.path.getsize(out_final)/(1024*1024))
                # Tự động xóa file clip tạm sau khi ghép thành công vào video đích
                if delete_temp:
                    for f_tmp in (f_a, f_b):
                        try:
                            if os.path.isfile(f_tmp):
                                os.remove(f_tmp)
                        except Exception as de:
                            log.warning("Không thể xóa file temp %s: %s", f_tmp, de)
            else:
                failed_items.append({"item_id": iid, "reason": "FFmpeg concat_videos trả về thất bại"})
        except Exception as err:
            log.warning("✗ [Auto Stitch] Lỗi ghép SP %s: %s", iid, err)
            failed_items.append({"item_id": iid, "reason": str(err)})

    return {
        "success": True,
        "out_dir": out_dir,
        "total_waiting": len(waiting_ids),
        "stitched_count": len(stitched_items),
        "failed_count": len(failed_items),
        "stitched": stitched_items,
        "failed": failed_items,
        "message": f"Đã ghép thành công {len(stitched_items)}/{len(waiting_ids)} video 16s và dọn sạch file tạm!"
    }


@app.post("/api/shopee/clean-temp-clips")
def clean_finished_temp_clips(req: dict):
    """Xóa tất cả các file clip tạm (-a, -b, -raw) trong thư mục temp mà video hoàn chỉnh đã tồn tại."""
    saved_cfg = _get_shopee_settings()
    out_dir = (req.get("out_dir") or "").strip() or saved_cfg.get("out_dir") or shopee_engine.DEFAULT_OUT_DIR
    out_dir = os.path.normpath(out_dir)
    temp_dir = os.path.join(out_dir, "temp")

    if not os.path.isdir(temp_dir):
        return {"success": True, "deleted_count": 0, "freed_mb": 0, "message": "Thư mục temp không tồn tại"}

    try:
        out_files = os.listdir(out_dir)
    except OSError:
        out_files = []

    final_ids = set()
    for of in out_files:
        if not of.lower().endswith((".mp4", ".mov", ".webm")):
            continue
        p = os.path.join(out_dir, of)
        try:
            if os.path.getsize(p) < 50000:
                continue
        except OSError:
            continue
        m = re.search(r"(\d{8,})", of.lower())
        if m:
            final_ids.add(m.group(1))

    try:
        temp_files = os.listdir(temp_dir)
    except OSError:
        temp_files = []

    deleted_count = 0
    freed_bytes = 0
    for tf in temp_files:
        if not tf.lower().endswith((".mp4", ".mov", ".webm")):
            continue
        lower = tf.lower()
        m_id = re.match(r"^(\d+)[-_](?:a|b|raw)\.", lower)
        if m_id and m_id.group(1) in final_ids:
            tf_path = os.path.join(temp_dir, tf)
            try:
                sz = os.path.getsize(tf_path)
                os.remove(tf_path)
                deleted_count += 1
                freed_bytes += sz
            except Exception as e:
                log.warning("Không thể xóa file temp %s: %s", tf, e)

    freed_mb = round(freed_bytes / (1024 * 1024), 1)
    freed_gb = round(freed_bytes / (1024 ** 3), 2)
    msg = f"Đã dọn dẹp {deleted_count} file clip tạm, giải phóng {freed_gb} GB ({freed_mb} MB) dung lượng ổ cứng!"
    log.info("🧹 [Clean Temp] %s", msg)
    return {
        "success": True,
        "deleted_count": deleted_count,
        "freed_bytes": freed_bytes,
        "freed_mb": freed_mb,
        "freed_gb": freed_gb,
        "message": msg
    }


@app.get("/muse-logo.svg")
def get_muse_logo_svg():
    p = os.path.join(BASE_DIR, "muse-logo.svg")
    if os.path.isfile(p):
        return FileResponse(p, media_type="image/svg+xml")
    raise HTTPException(404, "Logo không tồn tại")


@app.get("/muse-logo.ico")
def get_muse_logo_ico():
    p = os.path.join(BASE_DIR, "muse-logo.ico")
    if os.path.isfile(p):
        return FileResponse(p, media_type="image/x-icon")
    raise HTTPException(404, "Logo icon không tồn tại")


@app.get("/muse-logo.png")
def get_muse_logo_png():
    p = os.path.join(BASE_DIR, "muse-logo.png")
    if os.path.isfile(p):
        return FileResponse(p, media_type="image/png")
    raise HTTPException(404, "Logo png không tồn tại")


@app.get("/api/version")
def get_app_version():
    ver_path = os.path.join(BASE_DIR, "version.txt")
    if os.path.isfile(ver_path):
        try:
            with open(ver_path, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return {"version": v}
        except Exception:
            pass
    return {"version": "1.2.1"}


# ------------------------- Blacklist Sản Phẩm -------------------------
@app.get("/api/shopee/blacklist")
def get_shopee_blacklist():
    return shopee_engine.get_blacklist()


@app.post("/api/shopee/save-blacklist")
def save_shopee_blacklist(req: dict):
    ok = shopee_engine.save_blacklist(req)
    return {"success": ok}


@app.post("/api/shopee/add-to-blacklist")
def add_to_shopee_blacklist(req: dict):
    item_id = req.get("item_id")
    keyword = req.get("keyword")
    reason = req.get("reason", "")
    res = shopee_engine.add_to_blacklist(item_id=item_id, keyword=keyword, reason=reason)
    return {"success": True, "blacklist": res}


# ------------------------- HomeProxy từ E:\ThinAptm0707 -------------------------
@app.get("/api/proxy/status")
def get_proxy_status():
    import proxy_manager
    pool = proxy_manager.get_cached_proxy_pool()
    if not pool:
        pool = proxy_manager.load_proxies_from_thinaptm()
    accounts = store.list_accounts()
    assigned = sum(1 for a in accounts if a.get("proxy"))
    alive = sum(1 for a in accounts if a.get("proxy_status") == "alive")
    return {
        "success": True,
        "total_proxies": len(pool),
        "proxies": pool[:30],
        "assigned_accounts": assigned,
        "alive_proxies": alive,
        "accounts": accounts
    }


@app.post("/api/proxy/sync-thinaptm")
def sync_thinaptm_proxies():
    import proxy_manager
    return proxy_manager.sync_all_accounts_with_homeproxy()


@app.post("/api/proxy/test/{acc_id}")
def test_account_proxy(acc_id: str):
    import proxy_manager
    acc = store.get_account(acc_id)
    if not acc:
        raise HTTPException(404, "Tài khoản không tồn tại")
    p = acc.get("proxy")
    if not p:
        p = proxy_manager.ensure_alive_proxy_for_account(acc_id)
        if not p:
            return {"success": False, "error": "Chưa có proxy nào trong Pool"}
    ok, ip_or_err = proxy_manager.test_proxy(p)
    if ok:
        store.update_account(acc_id, proxy_status="alive", proxy_ip=ip_or_err)
    else:
        store.update_account(acc_id, proxy_status="dead", proxy_error=ip_or_err)
    return {"success": ok, "proxy": p, "ip": ip_or_err if ok else None, "error": ip_or_err if not ok else None}


@app.post("/api/proxy/rotate/{acc_id}")
def rotate_account_proxy(acc_id: str):
    import proxy_manager
    engine.close_session_for_account(acc_id)
    new_p = proxy_manager.ensure_alive_proxy_for_account(acc_id, force_check=True)
    if not new_p:
        raise HTTPException(500, "Không tìm thấy proxy khả dụng trong Pool")
    acc = store.get_account(acc_id) or {}
    return {"success": True, "proxy": new_p, "ip": acc.get("proxy_ip")}


# ------------------------- Trình duyệt Anti-Detect & Quản lý TTL -------------------------
@app.get("/api/browser/ttl")
def get_browser_ttl():
    return {
        "ttl_seconds": engine.browser_ttl_seconds,
        "ttl_minutes": round(engine.browser_ttl_seconds / 60, 1),
        "max_tasks": engine.browser_max_tasks,
        "anti_detect": getattr(CFG, "anti_detect", True),
        "active_sessions": engine.get_pool_status(),
        "total_active_sessions": len(engine._session_pool),
    }


@app.post("/api/browser/ttl")
def update_browser_ttl(req: dict):
    if "ttl_seconds" in req:
        engine.browser_ttl_seconds = max(60, int(req["ttl_seconds"]))
    elif "ttl_minutes" in req:
        engine.browser_ttl_seconds = max(60, int(float(req["ttl_minutes"]) * 60))
    if "max_tasks" in req:
        engine.browser_max_tasks = max(1, int(req["max_tasks"]))
    return {
        "success": True,
        "ttl_seconds": engine.browser_ttl_seconds,
        "ttl_minutes": round(engine.browser_ttl_seconds / 60, 1),
        "max_tasks": engine.browser_max_tasks,
    }


@app.post("/api/browser/recycle")
def recycle_browser(req: dict = Body(default={})):
    """Giải phóng toàn bộ browser contexts và làm sạch session pool để xóa sạch profile/cache chống phát hiện."""
    engine.close_all_sessions()
    restart_chrome = req.get("restart_chrome", False) if isinstance(req, dict) else False
    if restart_chrome:
        try:
            engine.stop()
            engine.start()
        except Exception:
            pass
    return {"success": True, "message": "Đã giải phóng toàn bộ phiên trình duyệt và bộ đệm (Recycled)"}



@app.post("/api/shopee/download-image")
def download_shopee_image(req: dict):
    image_url = req.get("image_url", "")
    data_url = shopee_engine.download_image_as_data_url(image_url)
    if not data_url:
        raise HTTPException(400, "Không tải được ảnh từ Shopee")
    return {"data_url": data_url}


@app.post("/api/shopee/save-rendered-video")
def save_rendered_video(req: dict):
    """Lưu video đã render từ Muse media folder sang thư mục đầu ra chỉ định.
    Nếu ghep_anh=True, thực hiện ghép ảnh sản phẩm làm Outro tạo video chuẩn 12s."""
    filename = req.get("filename", "")
    out_dir = req.get("out_dir") or shopee_engine.DEFAULT_OUT_DIR
    target_name = req.get("target_name") or filename
    ghep_anh = bool(req.get("ghep_anh", False))
    del_img = bool(req.get("del_img", True))
    image_url = req.get("image_url", "")
    image_data_url = req.get("image_data_url", "")
    duration_val = req.get("duration")

    ai_dur = None
    if duration_val:
        try:
            if isinstance(duration_val, (int, float)):
                ai_dur = float(duration_val)
            else:
                m = re.search(r"(\d+)", str(duration_val))
                if m:
                    ai_dur = float(m.group(1))
        except Exception:
            ai_dur = None

    src = _media_file(filename)
    target_name = _bare_filename(target_name, "target_name")
    if not target_name.lower().endswith(_VIDEO_EXTS):
        raise HTTPException(400, "target_name phải là file video (.mp4)")
    try:
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, target_name)

        stitched = False
        temp_img_path = None
        if ghep_anh and (image_url or image_data_url):
            try:
                temp_img_path = os.path.join(CFG.data_dir, f"temp_outro_{int(time.time()*1000)}.jpg")
                if image_data_url and ";base64," in image_data_url:
                    b64_content = image_data_url.split(";base64,", 1)[1]
                    with open(temp_img_path, "wb") as f:
                        f.write(base64.b64decode(b64_content))
                elif image_url:
                    shopee_engine.download_image_to_file(image_url, temp_img_path)

                if os.path.isfile(temp_img_path) and os.path.getsize(temp_img_path) > 100:
                    log.info("[Ghep 12s] Dang ghep anh Outro (12s, AI: %ss) vao video %s...", ai_dur or "auto", target_name)
                    ok = shopee_engine.ghep_anh_12s(src, temp_img_path, dst, ai_duration=ai_dur)
                    if ok:
                        stitched = True
                        log.info("[Ghep 12s] Thanh cong: %s", dst)
            except Exception as ge:
                log.warning("Ghép ảnh 12s không thành công, dùng video gốc: %s", ge)
            finally:
                if temp_img_path and os.path.isfile(temp_img_path) and del_img:
                    try:
                        os.remove(temp_img_path)
                    except Exception:
                        pass

        if not stitched:
            import shutil
            shutil.copy2(src, dst)

        # Tự động dọn dẹp file trung gian trong CFG.media_dir sau khi đã lưu xong video hoàn chỉnh
        if os.path.isfile(dst) and os.path.getsize(dst) > 1000:
            media_dir_abs = os.path.abspath(CFG.media_dir)
            if os.path.abspath(src).startswith(media_dir_abs):
                try:
                    os.remove(src)
                except Exception:
                    pass

            # Tự động dọn dẹp các clip tạm (-a, -b, -raw) trong out_dir/temp tương ứng với Item ID
            m_target_id = re.search(r"(\d{8,})", target_name)
            if m_target_id:
                tid = m_target_id.group(1)
                temp_dir = os.path.join(out_dir, "temp")
                if os.path.isdir(temp_dir):
                    for ext in (".mp4", ".mov", ".webm"):
                        for suffix in (f"{tid}-a{ext}", f"{tid}_a{ext}", f"{tid}-b{ext}", f"{tid}_b{ext}", f"{tid}-raw{ext}", f"{tid}_raw{ext}"):
                            tf = os.path.join(temp_dir, suffix)
                            if os.path.isfile(tf):
                                try:
                                    os.remove(tf)
                                except Exception:
                                    pass

        return {"success": True, "saved_path": dst, "stitched": stitched}
    except Exception as e:
        raise HTTPException(500, f"Lỗi lưu file video: {e}")


@app.post("/api/shopee/clean-media-cache")
def clean_media_cache():
    """Dọn dẹp sạch sẽ các file video tạm tích tụ trong thư mục data/media để giải phóng dung lượng đĩa."""
    d = CFG.media_dir
    removed_count = 0
    freed_bytes = 0
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                try:
                    sz = os.path.getsize(p)
                    os.remove(p)
                    removed_count += 1
                    freed_bytes += sz
                except Exception:
                    pass
    freed_mb = round(freed_bytes / (1024 * 1024), 2)
    log.info("🧹 Đã dọn dẹp %d file tạm trong data/media (giải phóng %.2f MB)", removed_count, freed_mb)
    return {"success": True, "removed_count": removed_count, "freed_mb": freed_mb}




# ------------------------- 账号自动保活与静默续期 -------------------------
KEEPALIVE_LOCK = asyncio.Lock()
KEEPALIVE_STATE = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "next_run": None,
}


def _probe_account_sync(aid: str, check_quota: bool = False) -> dict:
    """通过 muse.ai/api/session 触发 Meta 网关签发新 hatch_vml (+48h) / hatch_sess (+30d) 并唤醒云端 VM。"""
    acc = store.get_account(aid)
    if not acc or not acc.get("cookies"):
        return {"ok": False, "id": aid, "label": (acc or {}).get("label", aid), "error": "账号无有效 cookie"}
    try:
        p_url = None
        try:
            import proxy_manager
            p_url = proxy_manager.get_forwarder_url_for_account(aid)
        except Exception:
            pass
        res = engine.renew_session_http(acc["cookies"], acc.get("cookies_exp"), wake_vm=True, proxy_url=p_url)
        store.update_account(
            aid,
            cookies=res["cookies"],
            cookies_exp=res["cookies_exp"],
            ok=True if res.get("ok") else False,
            synced_at=int(time.time()),
        )
        vm_state = res.get("vm_state") or "RUNNING"
        store.touch_keepalive(aid, True, f"Phiên hợp lệ · Tự động duy trì (VM: {vm_state})")
        quota = acc.get("quota")
        if check_quota and GEN_LOCK.acquire(blocking=False):
            try:
                engine.start()
                quota = engine.quota(res["cookies"], res["cookies_exp"])
                quota["checked_at"] = int(time.time())
                store.update_account(aid, quota=quota)
            except Exception as qe:  # noqa: BLE001
                log.warning("Đọc định mức tài khoản %s thất bại: %s", aid, qe)
            finally:
                GEN_LOCK.release()
        updated = store.get_account(aid) or {}
        return {
            "ok": True,
            "id": aid,
            "label": acc.get("label", aid),
            "vm_id": res.get("vm_id"),
            "vm_state": vm_state,
            "wake_ok": res.get("wake_ok"),
            "expires_at": updated.get("expires_at"),
            "quota": quota,
        }
    except MuseAuthError as exc:
        store.touch_keepalive(aid, False, f"Xác thực thất bại khi duy trì: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "label": acc.get("label", aid), "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        store.touch_keepalive(aid, None, f"Chưa xác nhận duy trì: {str(exc)[:200]}")
        return {"ok": False, "id": aid, "label": acc.get("label", aid), "error": str(exc)}


async def run_keepalive_all(force: bool = False) -> dict:
    """执行账号保活：force=True 时强制刷新所有启用账号，否则刷新临期/未检账号并保持 VM 热备。"""
    async with KEEPALIVE_LOCK:
        now = int(time.time())
        KEEPALIVE_STATE["running"] = True
        KEEPALIVE_STATE["last_run"] = now
        results = []
        skipped = []

        try:
            include_disabled = getattr(CFG, "keepalive_disabled_accounts", False)
            accounts = [a for a in store.list_accounts() if include_disabled or a.get("enabled", True)]
            for a in accounts:
                aid = a["id"]
                label = a.get("label", aid)
                exp_at = a.get("expires_at")
                last_ka = a.get("last_keepalive") or 0

                needs_run = (
                    force
                    or not exp_at
                    or (exp_at - now < 36 * 3600)
                    or ((now - last_ka) > 15 * 60)
                    or (a.get("ok") is not True)
                )
                if not needs_run:
                    skipped.append({"id": aid, "label": label, "reason": "会话充足且近期已保活"})
                    continue

                log.info("【自动保活】正在为账号 %s (%s) 执行静默续期与 VM 唤醒...", label, aid)
                res = await asyncio.to_thread(_probe_account_sync, aid, False)
                results.append(res)
                log.info("【自动保活】账号 %s 执行结果: ok=%s expires_at=%s", label, res.get("ok"), res.get("expires_at"))
                await asyncio.sleep(0.5)

            summary = {
                "checked_at": now,
                "refreshed_count": len(results),
                "refreshed": results,
                "skipped_count": len(skipped),
                "skipped": skipped,
            }
            KEEPALIVE_STATE["last_result"] = summary
            KEEPALIVE_STATE["next_run"] = now + 900
            return summary
        finally:
            KEEPALIVE_STATE["running"] = False


def _warmup_browser_sync():
    """后台静默预热浏览器与首个可用账号的 WebSocket 隧道，使重启后首条请求也秒回。"""
    if getattr(engine, "current_acc_id", None) and engine.page is not None:
        return
    acc = store.pick_account(rotate=False)
    if not acc or not acc.get("cookies"):
        return
    if not GEN_LOCK.acquire(blocking=False):
        return
    try:
        log.info("【浏览器预热】正在后台预热账号 %s (%s) 的热备标签页...", acc.get("label"), acc["id"])
        refreshed = _renew_and_persist(acc["id"], wake_vm=True, force=False) or acc
        engine.start()
        engine.ensure_page(refreshed["cookies"], refreshed.get("cookies_exp"), account_id=acc["id"])
        log.info("【浏览器预热】账号 %s (%s) 热备标签页与 WebSocket 已就绪", acc.get("label"), acc["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("【浏览器预热】预热异常: %s", exc)
    finally:
        GEN_LOCK.release()


async def _keepalive_loop():
    """后台常驻守护任务：每 15 分钟轮询一次账号健康状态并保持 VM 热备。"""
    log.info("【自动保活守护进程】已启动，检测周期: 15 分钟")
    await asyncio.sleep(1)
    try:
        await asyncio.to_thread(_warmup_browser_sync)
    except Exception as e:  # noqa: BLE001
        log.warning("【浏览器预热】异常: %s", e)
    while True:
        try:
            await run_keepalive_all(force=False)
            if not getattr(engine, "current_acc_id", None) or engine.page is None:
                await asyncio.to_thread(_warmup_browser_sync)
        except Exception as e:  # noqa: BLE001
            log.error("【自动保活守护进程】轮询异常: %s", e)
        # Dọn media tự động (chỉ chạy khi người dùng bật retention/max_files qua .env)
        if CFG.media_retention_days > 0 or CFG.media_max_files > 0:
            try:
                await asyncio.to_thread(cleanup_media, CFG.media_retention_days, CFG.media_max_files, False)
            except Exception as e:  # noqa: BLE001
                log.warning("🧹 Dọn media tự động lỗi (bỏ qua): %s", e)
        await asyncio.sleep(900)


@app.post("/admin/accounts/keepalive")
async def trigger_keepalive_all(force: bool = True, _=Depends(admin_auth)):
    """管理员手动触发一次全账号保活续期。"""
    if KEEPALIVE_STATE["running"]:
        return {"status": "busy", "message": "保活任务正在执行中，请稍候"}
    return await run_keepalive_all(force=force)


@app.post("/admin/accounts/{aid}/keepalive")
async def trigger_keepalive_single(aid: str, _=Depends(admin_auth)):
    """手动针对单个账号执行保活续期。"""
    return await asyncio.to_thread(_probe_account_sync, aid, False)


@app.get("/admin/keepalive/status")
def get_keepalive_status(_=Depends(admin_auth)):
    """获取保活守护协程状态。"""
    return KEEPALIVE_STATE


# ------------------------- 仓库实时更新检测、通知与一键在线升级 -------------------------
REPO_URL = "https://github.com/czg86389-hub/muse2api"
TRACKED_REPO_PATHS = [
    "app.py", "engine.py", "store.py", "cdp.py", "config.py",
    "admin.html", "README.md", "version.json", "requirements.txt",
    "Dockerfile", "docker-compose.yml", ".env.example", ".gitignore",
    "LICENSE", "extension", "deploy", "tools",
]
_UPDATE_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


def _read_env_key(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    path = os.path.join(CFG.base_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f.read().splitlines():
                s = ln.strip()
                if s.startswith(key + "="):
                    return s.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _read_local_version() -> dict:
    p = os.path.join(BASE_DIR, "version.json")
    try:
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass
    return {"version": "1.5.0", "highlights": []}


def _installed_sha_file() -> str:
    return os.path.join(CFG.data_dir, ".installed_sha")


def _git(args: list[str], timeout: int = 30):
    import subprocess
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", "-c", f"safe.directory={BASE_DIR}", *args],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _ensure_git_repo(token: str = ""):
    """确保 BASE_DIR 已初始化为绑定 czg86389-hub/muse2api 的 Git 仓库。"""
    git_dir = os.path.join(BASE_DIR, ".git")
    remote_url = f"https://x-access-token:{token}@github.com/czg86389-hub/muse2api.git" if token else f"{REPO_URL}.git"
    if not os.path.isdir(git_dir):
        _git(["init", "-b", "main"])
        _git(["remote", "add", "origin", remote_url])
        _git(["fetch", "origin", "main"], timeout=45)
        _git(["reset", "--mixed", "origin/main"])
    else:
        _git(["remote", "set-url", "origin", remote_url])
    _git(["config", "user.name", "czg86389-hub"])
    _git(["config", "user.email", "czg86389-hub@users.noreply.github.com"])


def _check_update_sync(force: bool = False) -> dict:
    """检测 GitHub 官方仓库 (czg86389-hub/muse2api) 是否有新版本或新提交。
    默认缓存 90 秒，防止频繁刷新触发 GitHub API 速率限制。"""
    now = time.time()
    if not force and _UPDATE_CACHE["data"] and (now - _UPDATE_CACHE["ts"]) < 90:
        return _UPDATE_CACHE["data"]

    import requests
    token = _read_env_key("GITHUB_TOKEN")
    local_ver_obj = _read_local_version()
    local_version = str(local_ver_obj.get("version") or "1.5.0")

    has_git = os.path.isdir(os.path.join(BASE_DIR, ".git"))
    local_sha, local_msg, local_ts = "", "", 0
    if has_git:
        try:
            r_sha = _git(["rev-parse", "--short", "HEAD"])
            if r_sha.returncode == 0:
                local_sha = r_sha.stdout.strip()[:7]
            r_log = _git(["log", "-1", "--format=%s||%ct"])
            if r_log.returncode == 0 and "||" in r_log.stdout:
                parts = r_log.stdout.strip().split("||", 1)
                local_msg = parts[0]
                local_ts = int(parts[1])
        except Exception:
            pass

    if not local_sha and os.path.isfile(_installed_sha_file()):
        try:
            with open(_installed_sha_file(), encoding="utf-8") as f:
                local_sha = f.read().strip()[:7]
        except OSError:
            pass

    remote_version = local_version
    highlights = list(local_ver_obj.get("highlights") or [])
    try:
        rv = requests.get(
            f"https://raw.githubusercontent.com/czg86389-hub/muse2api/main/version.json?t={int(now)}",
            timeout=6,
        )
        if rv.status_code == 200:
            rvj = rv.json()
            if isinstance(rvj, dict):
                remote_version = str(rvj.get("version") or remote_version)
                if rvj.get("highlights"):
                    highlights = list(rvj["highlights"])
    except Exception:
        pass

    remote_sha, remote_msg, remote_time = "", "", ""
    recent_commits = []
    try:
        headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "muse2api-updater"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = requests.get(
            "https://api.github.com/repos/czg86389-hub/muse2api/commits?sha=main&per_page=5",
            headers=headers,
            timeout=6,
        )
        if resp.status_code == 200 and isinstance(resp.json(), list):
            commits = resp.json()
            for idx, c in enumerate(commits):
                sha7 = (c.get("sha") or "")[:7]
                c_msg = ((c.get("commit") or {}).get("message") or "").splitlines()[0]
                c_date = (((c.get("commit") or {}).get("committer") or {}).get("date") or "")
                c_url = c.get("html_url") or f"{REPO_URL}/commit/{sha7}"
                if idx == 0:
                    remote_sha = sha7
                    remote_msg = c_msg
                    remote_time = c_date
                recent_commits.append({
                    "sha": sha7,
                    "message": c_msg,
                    "date": c_date,
                    "url": c_url,
                })
    except Exception:
        pass

    # 若用户通过 Docker/ZIP 部署（无 .git）且首次运行版本一致，记录初始基准 SHA
    if not local_sha and remote_sha and local_version == remote_version:
        local_sha = remote_sha
        try:
            with open(_installed_sha_file(), "w", encoding="utf-8") as f:
                f.write(remote_sha)
        except OSError:
            pass

    def _parse_v(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in re.findall(r"\d+", v))
        except Exception:
            return (0,)

    has_update = False
    if remote_sha and local_sha:
        has_update = (remote_sha != local_sha)
    elif remote_version and local_version:
        has_update = _parse_v(remote_version) > _parse_v(local_version)

    data = {
        "repo_url": REPO_URL,
        "has_git": has_git,
        "local_version": local_version,
        "remote_version": remote_version,
        "local_sha": local_sha,
        "local_msg": local_msg,
        "local_ts": local_ts,
        "remote_sha": remote_sha,
        "remote_msg": remote_msg,
        "remote_time": remote_time,
        "has_update": has_update,
        "up_to_date": not has_update and bool(remote_sha or remote_version),
        "highlights": highlights,
        "recent_commits": recent_commits,
        "checked_at": int(now),
    }
    _UPDATE_CACHE["ts"] = now
    _UPDATE_CACHE["data"] = data
    return data


def _upgrade_from_github_sync() -> dict:
    """从 GitHub 拉取最新代码覆盖核心文件（兼容 Git 与无 Git 的 Docker/ZIP 环境），绝不触碰 .env 与 data/。"""
    import requests
    import tarfile

    token = _read_env_key("GITHUB_TOKEN")
    upgraded_via = ""
    try:
        _ensure_git_repo(token)
        f_res = _git(["fetch", "origin", "main"], timeout=45)
        if f_res.returncode == 0:
            _git(["checkout", "-f", "origin/main", "--", "."])
            _git(["reset", "--mixed", "origin/main"])
            upgraded_via = "git"
    except Exception as e:
        log.warning("Git 拉取更新失败，将使用 Tarball 方式更新: %s", e)

    if not upgraded_via:
        resp = requests.get(
            "https://codeload.github.com/czg86389-hub/muse2api/tar.gz/refs/heads/main",
            timeout=60,
        )
        if resp.status_code != 200:
            raise HTTPException(502, f"下载 GitHub 更新包失败 (HTTP {resp.status_code})")
        protected_files = {".env", "data/accounts.json", "data/tasks.json"}
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            for member in tar.getmembers():
                parts = member.name.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                rel = parts[1].replace("\\", "/")
                if ".." in rel or rel in protected_files:
                    continue
                target_path = os.path.join(BASE_DIR, rel)
                if member.isdir():
                    os.makedirs(target_path, exist_ok=True)
                elif member.isfile():
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    fobj = tar.extractfile(member)
                    if fobj is not None:
                        with open(target_path, "wb") as out_f:
                            out_f.write(fobj.read())
        upgraded_via = "tarball"

    _UPDATE_CACHE["ts"] = 0.0
    status = _check_update_sync(force=True)
    if status.get("remote_sha"):
        try:
            with open(_installed_sha_file(), "w", encoding="utf-8") as f:
                f.write(status["remote_sha"])
            status["local_sha"] = status["remote_sha"]
            status["has_update"] = False
            status["up_to_date"] = True
        except OSError:
            pass
    return {
        "ok": True,
        "via": upgraded_via,
        "message": f"已成功更新至最新版本 {status.get('remote_version')} ({status.get('remote_sha')})",
        "status": status,
    }


@app.get("/admin/update/check")
@app.get("/admin/repo/status")
async def admin_check_update(force: bool = False):
    """供所有已部署节点实时检测 GitHub 官方仓库是否有新版本更新。"""
    data = dict(await asyncio.to_thread(_check_update_sync, force))
    if not _upstream_sync_enabled():
        # Bản này đã sửa nhiều so với repo gốc -> không gợi ý "nâng cấp" vì nâng cấp sẽ ghi đè code local.
        data.update(has_update=False, up_to_date=True, upgrade_disabled=True)
    return data


def _upstream_sync_enabled() -> bool:
    return os.environ.get("MUSE2API_ALLOW_UPSTREAM_SYNC", "").strip() == "1"


def _require_upstream_sync():
    """Chặn pull/push với repo gốc czg86389-hub/muse2api.

    Bản cài này có nhiều thay đổi local (Studio, Shopee, worker session...) chưa có trên repo gốc.
    `git checkout -f origin/main -- .` hoặc tải tarball sẽ GHI ĐÈ toàn bộ các thay đổi đó,
    còn push sẽ đẩy code lên repo của người khác. Chỉ bật lại khi biết rõ hậu quả:
    đặt MUSE2API_ALLOW_UPSTREAM_SYNC=1 trong .env.
    """
    if not _upstream_sync_enabled():
        raise HTTPException(403, "Đã tắt cập nhật/đẩy code với repo gốc vì sẽ ghi đè các thay đổi local. "
                                 "Muốn bật lại: đặt MUSE2API_ALLOW_UPSTREAM_SYNC=1 trong .env và khởi động lại.")


@app.post("/admin/update/upgrade")
@app.post("/admin/repo/pull")
async def admin_upgrade_now(payload: dict = Body(default={}), _=Depends(admin_auth)):
    """一键从 GitHub 官方仓库拉取最新更新并自动平滑重启服务。"""
    _require_upstream_sync()
    res = await asyncio.to_thread(_upgrade_from_github_sync)
    restart = payload.get("restart", True) if isinstance(payload, dict) else True
    if restart:
        def _delayed_restart():
            time.sleep(0.5)
            try:
                engine.stop()
            except Exception:
                pass
            os._exit(0)
        threading.Thread(target=_delayed_restart, daemon=True).start()
    return res


@app.post("/admin/repo/push")
async def admin_repo_push(payload: dict = Body(default={}), _=Depends(admin_auth)):
    """维护者专用：将当前节点核心代码推送到 GitHub 仓库（自动过滤 .env 与 data 目录）。"""
    _require_upstream_sync()
    msg =(payload.get("message") or "").strip() or f"chore: sync update ({time.strftime('%Y-%m-%d %H:%M:%S')})"
    new_token = (payload.get("github_token") or "").strip()
    if new_token:
        _persist_env("GITHUB_TOKEN", new_token)
        os.environ["GITHUB_TOKEN"] = new_token
    token = new_token or _read_env_key("GITHUB_TOKEN")

    def _do_push():
        _ensure_git_repo(token)
        existing_paths = [p for p in TRACKED_REPO_PATHS if os.path.exists(os.path.join(BASE_DIR, p))]
        _git(["add", "--", *existing_paths])
        st = _git(["status", "--porcelain", "--", *existing_paths])
        committed = False
        if st.stdout.strip():
            c_res = _git(["commit", "-m", msg])
            if c_res.returncode != 0:
                raise HTTPException(500, f"Git commit 失败: {c_res.stderr or c_res.stdout}")
            committed = True
        p_res = _git(["push", "origin", "HEAD:main"], timeout=60)
        if p_res.returncode != 0:
            err = (p_res.stderr or p_res.stdout or "").strip()
            raise HTTPException(500, f"Git push 失败: {err[:300]}")
        _UPDATE_CACHE["ts"] = 0.0
        status = _check_update_sync(force=True)
        return {
            "ok": True,
            "committed": committed,
            "message": "已成功提交并推送到 GitHub 仓库",
            "status": status,
        }

    return await asyncio.to_thread(_do_push)


@app.on_event("startup")
async def _startup():
    for task in list(store.tasks.values()):
        if task.get("kind") == "image" and task.get("status") in ("queued", "processing"):
            store.update_task(task["id"], status="failed", error="服务重启中断了任务，请重新提交")
    # Key rỗng hoặc trùng key mặc định công khai (ai đọc source cũng biết) -> tự sinh key ngẫu nhiên.
    if not CFG.api_key or CFG.api_key in _PUBLIC_DEFAULT_KEYS:
        new_key = "m2a_" + secrets.token_hex(24)
        CFG.api_key = new_key
        os.environ["MUSE2API_KEY"] = new_key
        _persist_env("MUSE2API_KEY", new_key)
        log.warning("🔑 API key cũ trống hoặc là key mặc định công khai -> đã tạo key ngẫu nhiên mới và ghi vào .env "
                    "(MUSE2API_KEY). Cập nhật key này cho extension / userscript / client bên ngoài.")
    asyncio.create_task(_keepalive_loop())


@app.on_event("shutdown")
def _shutdown():
    engine.stop()
