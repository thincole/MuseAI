"""账号池与任务存储（JSON 落盘，原子写）。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid

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


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self._accounts_mtime = 0
        self.accounts: list[dict] = []
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
            disk_accs = _read(self.cfg.accounts_file, [])
            for a in disk_accs:
                ce = a.get("cookies_exp") or {}
                if _is_pos(ce.get("hatch_vml")):
                    a["expires_at"] = int(ce["hatch_vml"])
            self.accounts = disk_accs
            self._accounts_mtime = mtime
        except Exception:
            pass

    def _save_accounts(self):
        _write(self.cfg.accounts_file, self.accounts)
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
                            self._save_accounts()
                            return a
            healthy = [a for a in live if a.get("ok") is not False]
            candidates = healthy if healthy else live
            candidates.sort(key=lambda a: (a.get("last_used") or 0.0, a.get("use_count") or 0))
            acc = candidates[0]
            self._sticky_id = acc["id"]
            self._sticky_count = 1
            acc["last_used"] = time.time()
            acc["use_count"] = acc.get("use_count", 0) + 1
            self._save_accounts()
            return acc

    # Số việc đang chạy của từng TK; mỗi TK nhận tối đa MAX_JOBS_PER_ACCOUNT việc cùng lúc (ô "Số video cùng lúc / tài khoản").
    _BUSY_COUNT: dict[str, int] = {}
    MAX_JOBS_PER_ACCOUNT = 2
    _COND = threading.Condition(_LOCK)

    def set_max_jobs_per_account(self, n) -> int:
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 2
        with self._COND:
            Store.MAX_JOBS_PER_ACCOUNT = max(1, min(4, n))
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

    def record_vm_ok(self, aid: str):
        """TK vừa render thành công: xóa chuỗi kẹt và giờ nghỉ."""
        with self._COND:
            self._STUCK_STREAK.pop(aid, None)
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
                live = [a for a in self.accounts if a.get("enabled", True) and a.get("cookies")]
                available = [a for a in live if busy.get(a["id"], 0) < limit and not self.in_cooldown(a["id"])]
                if exclude_id and len(available) > 1:
                    available = [a for a in available if a["id"] != exclude_id] or available

                if available:
                    healthy = [a for a in available if a.get("ok") is not False]
                    candidates = healthy if healthy else available
                    least_busy = lambda a: (busy.get(a["id"], 0), a.get("last_used") or 0.0, a.get("use_count") or 0)  # noqa: E731
                    pref = next((a for a in candidates if a["id"] == preferred_id), None) if preferred_id else None
                    acc = pref if pref else min(candidates, key=least_busy)

                    busy[acc["id"]] = busy.get(acc["id"], 0) + 1
                    acc["last_used"] = time.time()
                    acc["use_count"] = acc.get("use_count", 0) + 1
                    self._save_accounts()
                    return acc

                rem = deadline - time.monotonic()
                if rem <= 0:
                    return None
                self._COND.wait(timeout=min(2.0, rem))

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
