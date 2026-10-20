"""账号池与任务存储（JSON 落盘，原子写）。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import date, datetime, timedelta

_LOCK = threading.Lock()
# Khóa riêng cho tasks.json: ghi task không được chặn việc thuê/trả tài khoản (_LOCK).
_TASK_LOCK = threading.Lock()

# Tiến độ (progress) đổi mỗi ~0.6s cho từng luồng render: chỉ cập nhật RAM, ghi đĩa tối đa mỗi N giây.
TASK_PERSIST_INTERVAL = 5.0
# Chỉ giữ N task đã kết thúc mới nhất trong tasks.json (task đang chạy luôn được giữ).
TASKS_KEEP = max(50, int(os.environ.get("MUSE2API_TASKS_KEEP", "500") or 500))
_TERMINAL_STATUSES = ("completed", "succeeded", "success", "done", "failed")

# Kẹt VM bao nhiêu lần liên tiếp thì cho TK nghỉ, và nghỉ bao lâu (phút, chỉnh qua .env).
STUCK_COOLDOWN_AFTER = 2
STUCK_COOLDOWN_SECONDS = max(60, int(float(os.environ.get("MUSE2API_STUCK_COOLDOWN_MIN", "60") or 60) * 60))

# 决定账号生死的核心 cookie（与 engine.ESSENTIAL_COOKIES 保持一致）
ESSENTIAL_COOKIES = ("hatch_sess", "hatch_gw", "hatch_vml",
                     "hatch_native_auth_device")


def _is_pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


# ---- Hạn mức token của tài khoản Muse (đọc từ Settings → Usage, lưu trong account["quota"]) ----
_TOKEN_RE = re.compile(r"([\d.,]+)\s*([KMBT]?)", re.I)
_TOKEN_MULT = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def parse_token_count(text) -> float | None:
    """'488M tokens left' -> 488e6; '0 tokens left' -> 0; không đọc được -> None."""
    m = _TOKEN_RE.search(str(text or ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "")) * _TOKEN_MULT[m.group(2).upper()]
    except (ValueError, KeyError):
        return None


def _reset_date(text, today: date) -> date | None:
    """'Oct 14' hoặc '17 tháng 10' -> ngày reset hạn mức tuần gần nhất (năm hiện tại, hoặc năm sau nếu đã lùi quá xa)."""
    s = str(text or "").strip()
    if not s:
        return None
    d = None
    m_vi = re.search(r"(\d{1,2})\s*(?:tháng|thg)\s*(\d{1,2})", s, re.I)
    if m_vi:
        try:
            d = date(today.year, int(m_vi.group(2)), int(m_vi.group(1)))
        except ValueError:
            d = None
    if d is None:
        try:
            d = datetime.strptime(f"{s} {today.year}", "%b %d %Y").date()
        except (ValueError, TypeError):
            return None
    if d < today - timedelta(days=180):
        d = d.replace(year=d.year + 1)
    return d


def account_exhausted(acc: dict, today: date | None = None) -> bool:
    """Tài khoản đã hết SẠCH hạn mức theo lần kiểm tra gần nhất: token phụ = 0 và hạn mức tuần đã dùng 100%,
    và ngày reset tuần CHƯA tới. Qua ngày reset thì coi như có hạn mức tuần mới (cho dùng lại để thử)."""
    q = acc.get("quota") or {}
    if not q.get("found"):
        return False
    tokens = parse_token_count(q.get("extra_left"))
    if tokens is None or tokens > 0:
        return False
    weekly = q.get("weekly_used_pct")
    if weekly is not None and weekly < 100:
        return False
    today = today or date.today()
    reset = _reset_date(q.get("weekly_reset"), today)
    return not (reset and today >= reset)


# hatch_vml 固定约 2 天，且不因使用而延长 —— 它就是决定账号寿命的那条 cookie。
# 实测对照：生成前后 synced_at 变了、也捞到新 cookie，但 4 条核心 cookie 的
# expires 一动不动。所以拿不到 hatch_vml 的 expires 时，按「导入时间 + 2 天」估算，
# 而不是退回到 hatch_native_auth_device 的 30 天（那个不决定生死，会误导用户）。
VML_TTL = 2 * 86400


def min_expiry(cookies_exp: dict | None) -> int | None:
    """核心 cookie 里最早过期的那个（通用兜底，不区分哪条决定寿命）。"""
    if not cookies_exp:
        return None
    vals = []
    for name in ESSENTIAL_COOKIES:
        v = cookies_exp.get(name)
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v)
    if not vals:
        for v in cookies_exp.values():
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v > 0:
                vals.append(v)
    return min(vals) if vals else None


def account_expiry(cookies_exp: dict | None,
                   base_ts: int | None = None) -> int | None:
    """账号实际失效时间。

    优先级：
      1) hatch_vml 的真实 expires（最准）；
      2) 拿不到就用 base_ts(导入/同步时刻) + VML_TTL 估算 —— 因为寿命就是 2 天；
      3) 都没有再退回核心 cookie 最早过期。
    """
    ce = cookies_exp or {}
    vml = ce.get("hatch_vml")
    if _is_pos(vml):
        return int(vml)
    if base_ts:
        return int(base_ts) + VML_TTL
    return min_expiry(ce)


def _read(path: str, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write(path: str, obj, compact: bool = False):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    dump_kw = {"separators": (",", ":")} if compact else {"indent": 1}
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, **dump_kw)
            # Ép dữ liệu xuống đĩa TRƯỚC khi đổi tên: nếu mất điện đúng lúc này, file cũ vẫn còn nguyên
            # (trước đây không fsync nên mất điện có thể để lại file đủ dung lượng nhưng toàn byte 0).
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(obj, f, ensure_ascii=False, **dump_kw)
                    break
                time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass


# ---------------- Sao lưu & tự khôi phục accounts.json (mất điện đột ngột) ----------------
BACKUP_KEEP = 10            # chỉ sao lưu 1 bản mỗi lần mở phần mềm; giữ tối đa bấy nhiêu bản mới nhất
_last_backup_at = 0.0


def _backup_dir(accounts_path: str) -> str:
    return os.path.join(os.path.dirname(accounts_path), "backups")


def _list_backups(accounts_path: str) -> list[str]:
    d = _backup_dir(accounts_path)
    try:
        names = [n for n in os.listdir(d) if n.startswith("accounts-") and n.endswith(".json")]
    except OSError:
        return []
    return [os.path.join(d, n) for n in sorted(names, reverse=True)]   # mới nhất trước


def backup_accounts(accounts_path: str, force: bool = False) -> str | None:
    """Sao lưu accounts.json sang data/backups/accounts-YYYYmmdd-HHMMSS.json. Chỉ gọi 1 lần khi mở phần mềm.
    Chỉ sao lưu khi file đọc được và có TK (không bao giờ ghi đè bản tốt bằng bản hỏng), và bỏ qua nếu nội dung
    giống hệt bản mới nhất. Giữ BACKUP_KEEP bản mới nhất. Trả về đường dẫn bản mới, hoặc None."""
    global _last_backup_at
    now = time.time()
    try:
        data = _read(accounts_path, None)
        if not isinstance(data, list) or not data:
            return None
        newest = _list_backups(accounts_path)[:1]
        if newest and _read(newest[0], None) == data:
            return None
        d = _backup_dir(accounts_path)
        dest = os.path.join(d, time.strftime("accounts-%Y%m%d-%H%M%S.json", time.localtime(now)))
        _write(dest, data)
        _last_backup_at = now
        for old in _list_backups(accounts_path)[BACKUP_KEEP:]:
            try:
                os.unlink(old)
            except OSError:
                pass
        return dest
    except Exception:  # noqa: BLE001
        return None


def load_accounts_with_recovery(accounts_path: str) -> list:
    """Đọc accounts.json. Nếu file hỏng/rỗng/toàn byte 0 (mất điện) thì tự khôi phục từ bản sao lưu hợp lệ mới nhất
    và ghi lại file chính."""
    if not os.path.isfile(accounts_path):
        data = None
    else:
        data = _read(accounts_path, None)
    if isinstance(data, list):
        return data
    import logging
    log = logging.getLogger("muse2api")
    for b in _list_backups(accounts_path):
        cand = _read(b, None)
        if isinstance(cand, list) and cand:
            log.warning("🛟 [Sao lưu] accounts.json bị hỏng/mất -> tự khôi phục %d TK từ %s", len(cand), os.path.basename(b))
            try:
                _write(accounts_path, cand)
            except Exception:  # noqa: BLE001
                pass
            return cand
    if os.path.isfile(accounts_path):
        log.error("⚠ [Sao lưu] accounts.json bị hỏng và KHÔNG có bản sao lưu nào dùng được (thư mục data/backups).")
    return []


_STORE_INSTANCES: dict[str, "Store"] = {}
ACCOUNTS_PERSIST_INTERVAL = 3.0


class Store:
    def __new__(cls, cfg):
        key = os.path.abspath(getattr(cfg, "accounts_file", "") or "")
        if key and key in _STORE_INSTANCES:
            return _STORE_INSTANCES[key]
        inst = super().__new__(cls)
        if key:
            _STORE_INSTANCES[key] = inst
        return inst

    def __init__(self, cfg):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.cfg = cfg
        self._accounts_mtime = 0
        self._accounts_written_at = 0.0
        self.accounts: list[dict] = []
        # Mỗi lần mở phần mềm: nếu accounts.json lành thì sao lưu 1 bản; nếu hỏng thì tự khôi phục từ bản sao lưu.
        load_accounts_with_recovery(cfg.accounts_file)
        backup_accounts(cfg.accounts_file, force=True)
        self._sync_from_disk()
        self.tasks: dict[str, dict] = _read(cfg.tasks_file, {})
        self._tasks_written_at = 0.0
        self._archive_tasks_if_oversized()

    def _sync_from_disk(self):
        try:
            if not os.path.isfile(self.cfg.accounts_file):
                return
            mtime = os.path.getmtime(self.cfg.accounts_file)
            if mtime == getattr(self, "_accounts_mtime", 0):
                return
            disk_accs = load_accounts_with_recovery(self.cfg.accounts_file)
            mtime = os.path.getmtime(self.cfg.accounts_file)
            for a in disk_accs:
                # TK cũ chưa có cờ muse_locked rõ ràng: chỉ suy ra từ note khi trường muse_locked chưa tồn tại
                if "Muse is disconnected" in (a.get("note") or "") and "muse_locked" not in a:
                    a["muse_locked"] = True
                if "success_count" not in a:
                    a["success_count"] = 0
                    a["success_since_use"] = int(a.get("use_count") or 0)
                if "last_success_at" not in a:
                    a["last_success_at"] = int(a["last_used"]) if (int(a.get("success_count") or 0) > 0 and a.get("last_used")) else None
                ce = a.get("cookies_exp") or {}
                if _is_pos(ce.get("hatch_vml")):
                    a["expires_at"] = int(ce["hatch_vml"])
                # TK thiếu cookie lõi hatch_vml (kẹt bước đăng nhập OAuth/checkpoint): không thể mở ô tải ảnh -> khoá ngay
                cks = a.get("cookies") or {}
                if cks and not cks.get("hatch_vml"):
                    a["ok"] = False
                    a["muse_locked"] = True
                    a["note"] = "Thiếu cookie máy ảo (hatch_vml) do chưa hoàn tất đăng nhập — cần chạy Tự động đăng nhập lại (Relogin)"
                # Nếu TK được reset/mở khóa từ bên ngoài (ok=True, muse_locked=False), xóa luôn cooldown trong RAM
                aid = a.get("id")
                if aid and not a.get("muse_locked") and a.get("ok") is True and "Đã reset" in (a.get("note") or ""):
                    self._COOLDOWN_UNTIL.pop(aid, None)
                    self._STUCK_STREAK.pop(aid, None)
                    self._TIMEOUT_STREAK.pop(aid, None)
                    self._ATTACH_STREAK.pop(aid, None)
                    self._OUTCOMES.pop(aid, None)
                    self._OUTCOME_TS.pop(aid, None)
            self.accounts = disk_accs
            self._accounts_mtime = mtime
        except Exception:
            pass

    def _save_accounts(self, force: bool = True):
        now = time.time()
        if not force and (now - getattr(self, "_accounts_written_at", 0.0)) < ACCOUNTS_PERSIST_INTERVAL:
            return
        _write(self.cfg.accounts_file, self.accounts)
        self._accounts_written_at = now
        try:
            self._accounts_mtime = os.path.getmtime(self.cfg.accounts_file)
        except Exception:
            pass

    # ---------- 账号 ----------
    def add_account(self, cookies: dict, label: str = "",
                    cookies_exp: dict | None = None) -> dict:
        with _LOCK:
            self._sync_from_disk()
            exp = {k: int(v) for k, v in (cookies_exp or {}).items()
                   if _is_pos(v)}
            now = int(time.time())
            clean_label = (label or "").strip()

            # Nếu trong Studio đã có tài khoản (trùng email/label) thì ghi đè chứ không ghi mới
            matched = [
                a for a in self.accounts
                if clean_label and a.get("label", "").strip().lower() == clean_label.lower()
            ]
            if matched:
                target = matched[0]
                # Nếu trước đó có bản sao trùng lặp thì loại bỏ các bản sao thừa
                if len(matched) > 1:
                    extra_ids = {a["id"] for a in matched[1:]}
                    self.accounts = [a for a in self.accounts if a["id"] not in extra_ids]
                target["cookies"] = cookies
                target["cookies_exp"] = exp
                target["expires_at"] = account_expiry(exp, now)
                target["expiry_anchor"] = None
                target["enabled"] = True
                target["muse_locked"] = False      # cookie mới -> mở khoá TK (đã bị khoá vì phiên hết hạn)
                target["ok"] = None
                target["note"] = "Đã cập nhật cookie (ghi đè)"
                target["synced_at"] = now
                self._save_accounts()
                return target

            aid = uuid.uuid4().hex[:12]
            acc = {"id": aid, "label": clean_label or aid, "cookies": cookies,
                   "cookies_exp": exp, "expires_at": account_expiry(exp, now),
                   "expiry_anchor": None,
                   "enabled": True, "created_at": now,
                   "last_used": None, "last_keepalive": None, "use_count": 0,
                   "success_count": 0, "success_since_use": 0, "last_success_at": None,
                   "ok": None, "note": "", "synced_at": None}
            self.accounts.append(acc)
            self._save_accounts()
            return acc

    def list_accounts(self) -> list[dict]:
        with _LOCK:
            self._sync_from_disk()
            out = []
            for a in self.accounts:
                item = {k: v for k, v in a.items() if k != "cookies"}
                ce = a.get("cookies_exp") or {}
                if _is_pos(ce.get("hatch_vml")):
                    item["expires_at"] = int(ce["hatch_vml"])
                item["cookie_count"] = len(a.get("cookies", {}))
                item["essential_ok"] = all(
                    a.get("cookies", {}).get(n) for n in ESSENTIAL_COOKIES)
                # 有效期是 hatch_vml 的实测 expires，还是按「2 天寿命」推算的
                item["expires_estimated"] = not _is_pos(ce.get("hatch_vml"))
                now_ts = int(time.time())
                item["is_alive"] = bool(
                    a.get("enabled", True) and a.get("cookies")
                    and a.get("ok") is not False
                    and (not a.get("expires_at") or a["expires_at"] > now_ts)
                )
                item["quota_exhausted"] = account_exhausted(a)  # một nguồn sự thật cho giao diện
                out.append(item)
            return out

    def get_alive_accounts_count(self) -> int:
        now_ts = int(time.time())
        with _LOCK:
            self._sync_from_disk()
            return sum(1 for a in self.accounts
                       if a.get("enabled", True) and a.get("cookies")
                       and a.get("ok") is not False
                       and (not a.get("expires_at") or a["expires_at"] > now_ts))

    def delete_account(self, aid: str) -> bool:
        with _LOCK:
            self._sync_from_disk()
            n = len(self.accounts)
            self.accounts = [a for a in self.accounts if a["id"] != aid]
            self._save_accounts()
            return len(self.accounts) != n

    def pick_account(self, preferred_id: str | None = None, rotate: bool = True,
                     force_rotate: bool = False, exclude_id: str | None = None) -> dict | None:
        """选择可用账号：优先复用当前已预热的健康账号（减少切号重连耗时），每满 15 次或遇错自动轮转。"""
        with _LOCK:
            self._sync_from_disk()
            live = [a for a in self.accounts if a.get("enabled", True) and a.get("cookies")]
            if not live:
                return None
            if exclude_id and len(live) > 1:
                live = [a for a in live if a["id"] != exclude_id] or live
            if preferred_id and not force_rotate:
                for a in live:
                    if a["id"] == preferred_id and a.get("ok") is not False:
                        sticky_n = getattr(self, "_sticky_count", 0) if getattr(self, "_sticky_id", None) == preferred_id else 0
                        if not rotate or sticky_n < 15:
                            self._sticky_id = preferred_id
                            self._sticky_count = sticky_n + 1
                            a["last_used"] = time.time()
                            a["use_count"] = a.get("use_count", 0) + 1
                            self._save_accounts(force=False)
                            return a
            healthy = [a for a in live if a.get("ok") is not False]
            candidates = healthy if healthy else live
            candidates.sort(key=lambda a: (a.get("last_used") or 0.0, a.get("use_count") or 0))
            acc = candidates[0]
            self._sticky_id = acc["id"]
            self._sticky_count = 1
            acc["last_used"] = time.time()
            acc["use_count"] = acc.get("use_count", 0) + 1
            self._save_accounts(force=False)
            return acc

    # Số việc đang chạy của từng TK; mỗi TK nhận tối đa MAX_JOBS_PER_ACCOUNT việc cùng lúc (ô "Số video cùng lúc / tài khoản": 1 hoặc 2).
    _BUSY_COUNT: dict[str, int] = {}
    _LAST_ACQUIRE_TS: dict[str, float] = {}
    MAX_JOBS_PER_ACCOUNT = 2
    # Khoảng cách tối thiểu (giây) trước khi giao luồng thứ 2 vào cùng 1 tài khoản đang bận để tránh nghẽn máy ảo Muse.
    SAME_ACCOUNT_STAGGER_SECONDS = 15.0
    # Bỏ qua TK hết sạch cả gói tuần (100%) lẫn token bổ sung (0 token) khi giao việc.
    SKIP_EXHAUSTED = True
    _last_all_exhausted_warn = 0.0
    _COND = threading.Condition(_LOCK)

    def set_skip_exhausted(self, on) -> bool:
        with self._COND:
            Store.SKIP_EXHAUSTED = bool(on)
            self._COND.notify_all()
        return Store.SKIP_EXHAUSTED

    def block_until(self, aid: str, seconds: float):
        """Cho TK nghỉ `seconds` giây (trong RAM). Dùng khi Muse TỪ CHỐI thật vì hết hạn ngạch."""
        with self._COND:
            self._COOLDOWN_UNTIL[aid] = time.time() + seconds
            self._COND.notify_all()

    def set_max_jobs_per_account(self, n) -> int:
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 2
        with self._COND:
            Store.MAX_JOBS_PER_ACCOUNT = max(1, min(2, n))
            self._COND.notify_all()
        return Store.MAX_JOBS_PER_ACCOUNT
    # Cooldown (trong RAM, mở lại app là xóa): TK kẹt VM liên tiếp thì cho nghỉ, hết giờ nghỉ được thử lại 1 lần.
    _STUCK_STREAK: dict[str, int] = {}
    _COOLDOWN_UNTIL: dict[str, float] = {}

    def in_cooldown(self, aid: str) -> bool:
        return self._COOLDOWN_UNTIL.get(aid, 0.0) > time.time()

    def record_vm_stuck(self, aid: str) -> bool:
        """Ghi nhận 1 lần TK không kết nối được VM. Trả về True nếu TK vừa bị cho nghỉ."""
        with self._COND:
            n = self._STUCK_STREAK.get(aid, 0) + 1
            self._STUCK_STREAK[aid] = n
            if n >= STUCK_COOLDOWN_AFTER:
                self._COOLDOWN_UNTIL[aid] = time.time() + STUCK_COOLDOWN_SECONDS
                return True
            return False

    _ATTACH_STREAK: dict[str, int] = {}

    def record_attach_fail(self, aid: str, limit: int = 3) -> bool:
        """Ghi 1 lần TK không đính được ảnh (no-file-input). Đủ `limit` lần liên tiếp -> True (app cho TK nghỉ)."""
        with self._COND:
            n = self._ATTACH_STREAK.get(aid, 0) + 1
            self._ATTACH_STREAK[aid] = n
            return n >= limit

    _TIMEOUT_STREAK: dict[str, int] = {}

    def record_timeout(self, aid: str, limit: int = 3) -> int:
        """Ghi 1 lần TK "im lặng" (nhận việc nhưng Muse không trả video, hết giờ chờ). Đủ `limit` lần liên tiếp (không xen
        lần thành công nào) -> trả về số giây TK cần nghỉ (TK chưa từng ra video nghỉ 3 giờ, TK từng ra video nghỉ 1 giờ), chưa đủ -> 0.
        Hết giờ nghỉ TK được thử lại; còn timeout thì chuỗi vẫn >= limit nên nghỉ tiếp ngay."""
        with self._COND:
            n = self._TIMEOUT_STREAK.get(aid, 0) + 1
            self._TIMEOUT_STREAK[aid] = n
            if n < limit:
                return 0
            acc = next((a for a in self.accounts if a["id"] == aid), None)
            never_ok = not int((acc or {}).get("success_count") or 0)
            seconds = 3 * 3600 if never_ok else 3600
            self._COOLDOWN_UNTIL[aid] = time.time() + seconds
            return seconds

    # Chất lượng TK theo kết quả các lượt gần nhất (trong RAM): TK hay lỗi/im lặng được giao ÍT việc hơn thay vì chia đều.
    QUALITY_WINDOW = 12          # số lượt gần nhất được tính
    QUALITY_MIN_SAMPLES = 6      # chưa đủ số lượt này thì coi như TK tốt (chưa đủ dữ liệu để kết luận)
    QUALITY_PROBE_AFTER = 1200   # TK bị cho 0 việc: sau chừng này giây (kể từ lượt cuối) được thử lại 1 việc
    _OUTCOMES: dict[str, deque] = {}
    _OUTCOME_TS: dict[str, float] = {}

    def record_outcome(self, aid: str, ok: bool):
        """Ghi kết quả 1 lượt tạo video của TK (True = ra video, False = lỗi do TK/Muse/proxy)."""
        with self._COND:
            dq = self._OUTCOMES.get(aid)
            if dq is None:
                dq = self._OUTCOMES[aid] = deque(maxlen=self.QUALITY_WINDOW)
            dq.append(bool(ok))
            self._OUTCOME_TS[aid] = time.time()

    def quality_rate(self, aid: str) -> tuple[float | None, int]:
        """(tỉ lệ thành công, số lượt đã ghi) trong cửa sổ gần nhất; tỉ lệ None nếu chưa đủ dữ liệu."""
        dq = self._OUTCOMES.get(aid)
        n = len(dq) if dq else 0
        if n < self.QUALITY_MIN_SAMPLES:
            return None, n
        return sum(dq) / n, n

    def effective_limit(self, aid: str, base_limit: int) -> int:
        """Số việc tối đa TK được nhận cùng lúc: TK tốt = base_limit; thành công 15-50% = 1 việc; dưới 15% = 0 (nghỉ),
        quá QUALITY_PROBE_AFTER giây không có lượt mới thì cho thử lại 1 việc để biết TK đã hồi chưa."""
        rate, _n = self.quality_rate(aid)
        if rate is None or rate >= 0.5:
            return base_limit
        if rate >= 0.15:
            return min(base_limit, 1)
        if time.time() - self._OUTCOME_TS.get(aid, 0.0) >= self.QUALITY_PROBE_AFTER:
            return min(base_limit, 1)
        return 0

    def capacity_snapshot(self) -> dict:
        """Số TK dùng được và tổng số 'ô' việc song song hiện có (để gợi ý số luồng hợp lý)."""
        with self._COND:
            base = Store.MAX_JOBS_PER_ACCOUNT
            live = [a for a in self.accounts if a.get("enabled", True) and (a.get("cookies") or {}).get("hatch_vml")]
            usable = [a for a in live if not a.get("muse_locked") and not self.in_cooldown(a["id"])]
            slots = sum(self.effective_limit(a["id"], base) for a in usable)
            return {"accounts_total": len(self.accounts), "accounts_usable": len([a for a in usable if self.effective_limit(a["id"], base) > 0]),
                    "slots": slots, "jobs_per_account": base}

    def record_vm_ok(self, aid: str):
        """TK vừa render thành công: xóa chuỗi kẹt và giờ nghỉ."""
        self.record_outcome(aid, True)
        with self._COND:
            self._TIMEOUT_STREAK.pop(aid, None)
            self._ATTACH_STREAK.pop(aid, None)
            self._STUCK_STREAK.pop(aid, None)
            self._COOLDOWN_UNTIL.pop(aid, None)
            self._COND.notify_all()

    def clear_cooldown(self, aid: str):
        """Xoá giờ nghỉ + chuỗi kẹt VM của TK (không ghi kết quả thành công). Dùng sau khi TK vừa được đổi sang proxy sống."""
        with self._COND:
            self._STUCK_STREAK.pop(aid, None)
            self._TIMEOUT_STREAK.pop(aid, None)
            self._COOLDOWN_UNTIL.pop(aid, None)
            self._COND.notify_all()

    def acquire_account(self, preferred_id: str | None = None, exclude_id: str | None = None,
                        timeout: float = 60.0) -> dict | None:
        """Thuê tài khoản cho luồng render worker. Mỗi tài khoản phục vụ tối đa MAX_JOBS_PER_ACCOUNT luồng cùng lúc,
        ưu tiên TK đang ít việc nhất (chia đều trước khi chồng việc), bỏ qua TK đang nghỉ (cooldown) vì kẹt VM."""
        deadline = time.monotonic() + timeout
        with self._COND:
            while True:
                self._sync_from_disk()
                busy = self._BUSY_COUNT
                limit = Store.MAX_JOBS_PER_ACCOUNT
                live = [a for a in self.accounts if a.get("enabled", True) and (a.get("cookies") or {}).get("hatch_vml")]
                available = [a for a in live if busy.get(a["id"], 0) < self.effective_limit(a["id"], limit)
                             and not self.in_cooldown(a["id"]) and not a.get("muse_locked")]
                if exclude_id and len(available) > 1:
                    available = [a for a in available if a["id"] != exclude_id] or available

                if Store.SKIP_EXHAUSTED and available:
                    # Bỏ qua TK hết token: TK còn token đang bận thì luồng CHỜ (không tràn sang TK hết token).
                    # Chỉ khi TOÀN BỘ TK đều hết token mới dùng như cũ (không để hệ thống tự kẹt hoặc đẩy hàng loạt SP vào
                    # trạng thái lỗi) và cảnh báo.
                    if all(account_exhausted(a) for a in live):
                        if time.time() - Store._last_all_exhausted_warn > 300:
                            Store._last_all_exhausted_warn = time.time()
                            import logging
                            logging.getLogger("muse2api").warning(
                                "⚠ [Hạn mức] Tất cả TK đều hết token -> vẫn giao việc như cũ. Kiểm tra lại hạn mức ở tab Tài Khoản Muse.")
                    else:
                        available = [a for a in available if not account_exhausted(a)]

                now_ts = time.time()
                wait_hint = 2.0
                if available:
                    # Nếu TK đang có 1 luồng chạy (busy >= 1), bắt buộc chờ cách luồng trước tối thiểu SAME_ACCOUNT_STAGGER_SECONDS
                    # để 2 luồng không lao vào cùng 1 máy ảo Muse cùng lúc gây nghẽn.
                    stagger = self.SAME_ACCOUNT_STAGGER_SECONDS
                    ready = [
                        a for a in available
                        if busy.get(a["id"], 0) == 0
                        or (now_ts - self._LAST_ACQUIRE_TS.get(a["id"], 0.0)) >= stagger
                    ]
                    if not ready:
                        soonest = min(
                            max(0.2, stagger - (now_ts - self._LAST_ACQUIRE_TS.get(a["id"], 0.0)))
                            for a in available
                        )
                        wait_hint = min(2.0, soonest)
                    else:
                        # Ưu tiên tuyệt đối TK đang rảnh hoàn toàn (busy == 0) trước khi chồng việc thứ 2 lên cùng 1 VM Muse
                        least_busy = lambda a: (busy.get(a["id"], 0), 0 if a.get("ok") is not False else 1, a.get("last_used") or 0.0, a.get("use_count") or 0)  # noqa: E731
                        pref = next((a for a in ready if a["id"] == preferred_id and a.get("ok") is not False), None) if preferred_id else None
                        acc = pref if pref else min(ready, key=least_busy)

                        busy[acc["id"]] = busy.get(acc["id"], 0) + 1
                        self._LAST_ACQUIRE_TS[acc["id"]] = now_ts
                        acc["last_used"] = now_ts
                        acc["use_count"] = acc.get("use_count", 0) + 1
                        self._save_accounts(force=False)
                        return acc

                rem = deadline - time.monotonic()
                if rem <= 0:
                    return None
                self._COND.wait(timeout=min(wait_hint, rem))

    def set_locked(self, aid: str, locked: bool, note: str = ""):
        """Khoá/mở khoá TK bị Muse ngắt kết nối. TK khoá KHÔNG được giao việc (kể cả sau khi khởi động lại phần mềm) cho tới
        khi người dùng mở khoá. Cờ riêng 'muse_locked' (không dùng ghi chú vì tác vụ giữ phiên tự ghi đè ghi chú)."""
        with _LOCK:
            self._sync_from_disk()
            for a in self.accounts:
                if a["id"] == aid:
                    a["muse_locked"] = bool(locked)
                    if note:
                        a["note"] = note[:300]
                    if not locked:
                        a["ok"] = None
                    break
            else:
                return
            self._save_accounts()
        with self._COND:
            self._COND.notify_all()

    def reset_all_accounts(self) -> int:
        """Reset toàn bộ tài khoản về trạng thái sẵn sàng hoạt động:
        - Mở khoá muse_locked = False, enabled = True, ok = True
        - Xóa mọi bộ đếm cooldown, kẹt VM, timeout, lịch sử chất lượng trong RAM
        - Gia hạn lại mốc ước tính expires_at (+48h) cho các TK bị đánh dấu quá hạn để cho phép thử lại."""
        now_ts = int(time.time())
        with self._COND:
            self._sync_from_disk()
            count = 0
            for a in self.accounts:
                a["enabled"] = True
                a["checked_at"] = now_ts
                cks = a.get("cookies") or {}
                if not cks.get("hatch_vml"):
                    a["muse_locked"] = True
                    a["ok"] = False
                    a["note"] = "Thiếu cookie máy ảo (hatch_vml) do chưa hoàn tất đăng nhập — cần chạy Tự động đăng nhập lại (Relogin)"
                    continue
                a["muse_locked"] = False
                a["ok"] = True
                a["note"] = "Đã reset — sẵn sàng chạy lại"
                ce = dict(a.get("cookies_exp") or {})
                if _is_pos(ce.get("hatch_vml")) and int(ce["hatch_vml"]) <= now_ts:
                    ce.pop("hatch_vml", None)
                    a["cookies_exp"] = ce
                if not a.get("expires_at") or int(a.get("expires_at") or 0) <= now_ts:
                    a["expiry_anchor"] = now_ts
                    a["expires_at"] = account_expiry(ce, now_ts)
                if a.get("proxy_status") == "dead":
                    a["proxy_status"] = "unknown"
                    a.pop("proxy_error", None)
                count += 1
            self._COOLDOWN_UNTIL.clear()
            self._STUCK_STREAK.clear()
            self._TIMEOUT_STREAK.clear()
            self._ATTACH_STREAK.clear()
            self._OUTCOMES.clear()
            self._OUTCOME_TS.clear()
            self._save_accounts(force=True)
            self._COND.notify_all()
            return count

    def record_success(self, aid: str):
        """Cộng 1 vào 'Video thành công' của TK: chỉ gọi khi video đã ra VÀ ghép/lưu xong (khác 'Số lần dùng' = số lượt
        được giao việc, kể cả lượt lỗi)."""
        with _LOCK:
            self._sync_from_disk()
            now_ts = int(time.time())
            for a in self.accounts:
                if a["id"] == aid:
                    a["success_count"] = int(a.get("success_count") or 0) + 1
                    a["last_success_at"] = now_ts
                    break
            else:
                return
            self._save_accounts()

    def release_account(self, aid: str):
        """Trả tài khoản lại cho Pool sau khi worker hoàn thành tác vụ."""
        with self._COND:
            n = self._BUSY_COUNT.get(aid, 0) - 1
            if n > 0:
                self._BUSY_COUNT[aid] = n
            else:
                self._BUSY_COUNT.pop(aid, None)
            self._COND.notify_all()

    def touch_keepalive(self, aid: str, ok: bool | None = True, note: str = ""):
        """ok=None 仅记录未确认的检测；保留上次账号状态、成功保活时间和有效期。"""
        with _LOCK:
            self._sync_from_disk()
            now_ts = int(time.time())
            for a in self.accounts:
                if a["id"] == aid:
                    if ok is not None:
                        a["ok"] = ok
                    if ok is True:
                        a["last_keepalive"] = now_ts
                        # ponytail: 只信实际 Cookie 到期时间或原导入锚点，不凭检测成功虚推 48h。
                        a["expires_at"] = account_expiry(
                            a.get("cookies_exp"), a.get("expiry_anchor") or a.get("created_at"))
                    if note:
                        cur_note = a.get("note") or ""
                        if ("Tự động duy trì" in note or "Phiên hợp lệ (VM:" in note) and any(
                            k in cur_note for k in ("hết hạn ngạch", "hết sạch", "Tác vụ gián đoạn", "403 Region", "chặn khu vực")
                        ):
                            pass
                        else:
                            a["note"] = note[:300]
                    a["checked_at"] = now_ts
            self._save_accounts()

    def mark(self, aid: str, ok: bool, note: str = ""):
        with _LOCK:
            self._sync_from_disk()
            for a in self.accounts:
                if a["id"] == aid:
                    a["ok"] = ok
                    a["note"] = note[:300]
                    a["checked_at"] = int(time.time())
            self._save_accounts()

    def get_account(self, aid: str) -> dict | None:
        with _LOCK:
            self._sync_from_disk()
            for a in self.accounts:
                if a["id"] == aid:
                    return a
            return None

    def update_account(self, aid: str, **kw) -> dict | None:
        """更新标签 / 启用状态 / cookies / 有效期等；值为 None 的字段跳过。

        传了 cookies_exp 会自动重算 expires_at，不用调用方自己算。
        估算锚点 expiry_anchor 的维护规则（避免有效期被反复虚推）：
          - 读到了 hatch_vml 的真实 expires  -> 清空锚点，直接用真值；
          - 读不到且锚点为空                -> 把锚点钉在当下（只钉一次）；
          - 补入全新会话 cookie（重登）      -> 调用方显式传 expiry_anchor=now 重置。
        """
        with _LOCK:
            self._sync_from_disk()
            for a in self.accounts:
                if a["id"] == aid:
                    for k, v in kw.items():
                        if v is not None:
                            a[k] = v
                    if "cookies_exp" in kw and kw["cookies_exp"] is not None:
                        ce = a["cookies_exp"]
                        if _is_pos(ce.get("hatch_vml")):
                            a["expiry_anchor"] = None
                        elif not a.get("expiry_anchor"):
                            a["expiry_anchor"] = int(time.time())
                        a["expires_at"] = account_expiry(
                            ce, a.get("expiry_anchor") or a.get("created_at"))
                    self._save_accounts()
                    return a
        return None

    def stats(self) -> dict:
        with _LOCK:
            self._sync_from_disk()
            now = int(time.time())
            total = len(self.accounts)
            enabled = sum(1 for a in self.accounts if a.get("enabled", True))
            bad = sum(1 for a in self.accounts
                      if a.get("enabled", True) and a.get("ok") is False)
            healthy = sum(1 for a in self.accounts
                          if a.get("enabled", True) and a.get("ok") is True)
            expiring = sum(1 for a in self.accounts
                           if a.get("enabled", True)
                           and a.get("expires_at")
                           and a["expires_at"] - now < 3 * 86400)
        expired = sum(1 for a in self.accounts
                      if a.get("enabled", True)
                      and a.get("expires_at") and a["expires_at"] <= now)
        return {"total": total, "enabled": enabled, "disabled": total - enabled,
                "healthy": healthy, "error": bad,
                "expiring": expiring, "expired": expired}

    # ---------- 任务 ----------
    def _pruned_tasks(self) -> dict[str, dict]:
        """Giữ mọi task đang chạy + TASKS_KEEP task đã kết thúc mới nhất."""
        finished = [t for t in self.tasks.values() if t.get("status") in _TERMINAL_STATUSES]
        if len(finished) <= TASKS_KEEP:
            return self.tasks
        finished.sort(key=lambda t: t.get("created_at") or 0, reverse=True)
        drop = {t["id"] for t in finished[TASKS_KEEP:]}
        return {k: v for k, v in self.tasks.items() if k not in drop}

    def _archive_tasks_if_oversized(self):
        """tasks.json phình to (hàng nghìn task) làm mỗi lần ghi tốn hàng trăm ms CPU.
        Lần đầu gặp: sao lưu nguyên bản ra tasks.archive-<thời gian>.json rồi chỉ giữ phần mới nhất."""
        with _TASK_LOCK:
            pruned = self._pruned_tasks()
            if len(pruned) == len(self.tasks):
                return
            archive = os.path.join(os.path.dirname(self.cfg.tasks_file),
                                   f"tasks.archive-{time.strftime('%Y%m%d-%H%M%S')}.json")
            try:
                _write(archive, self.tasks, compact=True)
            except Exception:  # noqa: BLE001
                return  # không sao lưu được thì giữ nguyên, không xóa lịch sử
            self.tasks = pruned
            self._flush_tasks_locked()

    def _flush_tasks_locked(self):
        """Ghi tasks.json (gọi khi đang giữ _TASK_LOCK)."""
        self.tasks = self._pruned_tasks()
        _write(self.cfg.tasks_file, self.tasks, compact=True)
        self._tasks_written_at = time.time()

    def create_task(self, kind: str, prompt: str) -> dict:
        with _TASK_LOCK:
            tid = "task_" + uuid.uuid4().hex[:20]
            t = {"id": tid, "object": "task", "kind": kind, "prompt": prompt,
                 "status": "queued", "created_at": int(time.time()),
                 "updated_at": int(time.time()), "result": None, "error": None}
            self.tasks[tid] = t
            self._flush_tasks_locked()
            return t

    def update_task(self, tid: str, **kw):
        with _TASK_LOCK:
            t = self.tasks.get(tid)
            if not t:
                return None
            t.update(kw)
            t["updated_at"] = int(time.time())
            # Chỉ đổi progress: RAM đã cập nhật (API đọc từ RAM), ghi đĩa tối đa mỗi TASK_PERSIST_INTERVAL giây.
            if set(kw) <= {"progress"} and time.time() - self._tasks_written_at < TASK_PERSIST_INTERVAL:
                return t
            self._flush_tasks_locked()
            return t

    def get_task(self, tid: str) -> dict | None:
        return self.tasks.get(tid)

    def list_tasks(self, limit: int = 50) -> list[dict]:
        with _TASK_LOCK:
            items = list(self.tasks.values())
        items.sort(key=lambda t: t.get("created_at") or 0, reverse=True)
        return items[:limit]

    def delete_task(self, tid: str) -> bool:
        with _TASK_LOCK:
            if tid in self.tasks:
                del self.tasks[tid]
                self._flush_tasks_locked()
                return True
            return False

    def clear_tasks(self, keep: int = 0) -> int:
        with _TASK_LOCK:
            items = sorted(self.tasks.values(),
                           key=lambda t: t.get("created_at") or 0, reverse=True)
            keep_ids = {t["id"] for t in items[:keep]}
            removed = len(self.tasks) - len(keep_ids)
            self.tasks = {k: v for k, v in self.tasks.items() if k in keep_ids}
            self._flush_tasks_locked()
            return max(0, removed)
