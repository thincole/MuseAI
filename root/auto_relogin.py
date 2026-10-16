"""Tự động đăng nhập lại (lấy cookie mới) cho các tài khoản Muse hết phiên, kèm kiểm tra hạn mức.

Chạy được 2 cách:
  - Dòng lệnh:  python auto_relogin.py --dry-run     (chỉ in kế hoạch, không đăng nhập)
                python auto_relogin.py --run [--max 10]
  - Do app gọi định kỳ (vòng việc nền) khi bật "Tự động đăng nhập lại" trong tab Tài khoản Muse.

Cách làm:
  1. Đọc thông tin đăng nhập (email|mật khẩu|refresh_token|client_id) từ các file/thư mục .txt trong cấu hình "sources".
  2. Chọn TK cần đăng nhập lại: TK bị khoá vì phiên hết hạn (lỗi 401 / không nhận được phiên) hoặc cookie sắp hết hạn.
     Bỏ qua TK Muse đã khoá hẳn ("Muse is disconnected" / VM bị tắt), TK đã ghi vào file chết/checkpoint, TK vừa thử lỗi nhiều lần,
     và TK mà lần kiểm tra hạn mức trước cho thấy đã cạn token + tuần 100% + chưa tới ngày reset (đăng nhập lại cũng vô ích).
  3. (Tuỳ chọn) khi số TK dùng được thấp hơn "min_active", bổ sung email mới chưa có trong hệ thống.
  4. Đăng nhập bằng đúng bộ máy của công cụ Lấy Cookie (không cửa sổ, proxy riêng mỗi luồng, hồ sơ trình duyệt ngẫu nhiên).
  5. Sau khi lấy được cookie: gọi kiểm tra hạn mức; TK mới (chưa từng ra video) mà đã cạn token thì khoá tới ngày reset.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from config import CFG  # noqa: E402

DATA_DIR = CFG.data_dir
CONFIG_FILE = os.path.join(DATA_DIR, "auto_relogin.json")
STATE_FILE = os.path.join(DATA_DIR, "auto_relogin_status.json")
LOG_FILE = os.path.join(DATA_DIR, "auto_relogin.log")
DEAD_FILE = os.path.join(DATA_DIR, "auto_login_dead.txt")
CHECKPOINT_FILE = os.path.join(DATA_DIR, "auto_login_checkpoint.txt")

DEFAULT_CONFIG = {
    "enabled": False,            # bật/tắt chạy tự động định kỳ (chạy tay luôn dùng được)
    "interval_hours": 6,         # chu kỳ chạy tự động
    "sources": [],               # thư mục/file .txt chứa email|mật khẩu|token|client_id (trống = tự tìm E:\\Acc muse)
    "max_per_run": 20,           # tối đa số TK đăng nhập lại mỗi lượt
    "threads": 3,                # số trình duyệt chạy song song
    "expiring_hours": 12,        # cookie còn dưới chừng này giờ thì đăng nhập lại trước
    "headless": True,            # chạy ngầm (không hiện cửa sổ Chrome)
    "proxy_source": "app",       # app | homeproxy | webshare
    "auto_add_new": False,       # bổ sung email mới khi thiếu TK dùng được
    "min_active": 40,            # số TK dùng được tối thiểu (chỉ dùng khi auto_add_new)
    "max_new_per_run": 10,       # tối đa email mới bổ sung mỗi lượt
    "retry_hours": 6,            # cùng 1 email không thử lại sớm hơn chừng này giờ
    "max_fails": 3,              # thất bại chừng này lần thì tạm bỏ (xem give_up_hours)
    "give_up_hours": 72,
}

# Ghi chú khoá cho biết TK bị Muse khoá hẳn / VM tắt: đăng nhập lại không cứu được
_HARD_LOCK_MARKERS = ("muse is disconnected", "ngắt kết nối", "vm: disabled", "tk chết", "acc chết", "checkpoint")
# Ghi chú khoá cho biết phiên hết hạn / không nhận được phiên / thiếu cookie hatch_vml: đăng nhập lại là đúng việc
_SESSION_MARKERS = ("xác thực thất bại", "401", "chưa xác nhận duy trì", "assigned", "phiên tk hết hạn", "hết hạn",
                    "hatch_vml", "thiếu cookie", "no-file-input", "relogin", "会话认证失败", "未返回")

_state_lock = threading.RLock()


# --------------------------------------------------------------------------------------------
# Cấu hình / trạng thái
# --------------------------------------------------------------------------------------------
def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in _read_json(CONFIG_FILE, {}).items() if k in DEFAULT_CONFIG})
    return cfg


def save_config(changes: dict) -> dict:
    cfg = load_config()
    for k, v in (changes or {}).items():
        if k not in DEFAULT_CONFIG:
            continue
        default = DEFAULT_CONFIG[k]
        try:
            if isinstance(default, bool):
                v = bool(v)
            elif isinstance(default, int):
                v = int(v)
            elif isinstance(default, list):
                v = [str(x).strip() for x in (v if isinstance(v, list) else re.split(r"[\n;]+", str(v))) if str(x).strip()]
            else:
                v = str(v)
        except (TypeError, ValueError):
            continue
        cfg[k] = v
    _write_json(CONFIG_FILE, cfg)
    return cfg


def load_state() -> dict:
    st = _read_json(STATE_FILE, {})
    st.setdefault("attempts", {})        # email -> {"ts": ..., "fails": n}
    st.setdefault("results", {})         # email -> {"status", "msg", "ts"}
    return st


def save_state(st: dict) -> None:
    with _state_lock:
        _write_json(STATE_FILE, st)


def is_running(st: dict | None = None) -> bool:
    st = st or load_state()
    if not st.get("running"):
        return False
    return time.time() - float(st.get("heartbeat") or 0) < 20 * 60     # quá 20 phút không báo = coi như đã chết


# --------------------------------------------------------------------------------------------
# Thông tin đăng nhập
# --------------------------------------------------------------------------------------------
MAILAPI_DOMAIN = "@nguyenmaucole.io.vn"
_SKIP_FILE_RE = re.compile(r"cookie|success_cookies|backup", re.I)


def default_sources() -> list[str]:
    out = []
    for d in ("E", "D", "F", "C"):
        p = f"{d}:\\Acc muse"
        if os.path.isdir(p):
            out.append(p)
    return out


def _source_files(sources: list[str]) -> list[str]:
    files: list[str] = []
    for s in sources or default_sources():
        if os.path.isdir(s):
            files += glob.glob(os.path.join(s, "*.txt"))
        elif os.path.isfile(s):
            files.append(s)
    files = [f for f in files if not _SKIP_FILE_RE.search(os.path.basename(f))]
    return sorted(set(files), key=os.path.getmtime)        # cũ -> mới: dòng ở file mới ghi đè dòng cũ


def read_credentials(sources: list[str]) -> dict[str, str]:
    """{email_thường: dòng gốc} — chỉ nhận dòng đủ 4 trường (email|mật khẩu|refresh_token|client_id) hoặc email hộp thư tạm."""
    creds: dict[str, str] = {}
    for fp in _source_files(sources):
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("|")
                    email = parts[0].strip().lower()
                    if "@" not in email:
                        continue
                    if len(parts) >= 4 or email.endswith(MAILAPI_DOMAIN):
                        creds[email] = line
        except OSError:
            continue
    return creds


def _email_set(path: str, max_age_hours: float | None = None) -> set[str]:
    out: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                parts = ln.strip().split("|")
                if not parts or "@" not in parts[0]:
                    continue
                if max_age_hours is not None and len(parts) >= 3:
                    try:
                        ts = datetime.strptime(parts[-1].strip(), "%Y-%m-%d %H:%M:%S")
                        if datetime.now() - ts > timedelta(hours=max_age_hours):
                            continue
                    except ValueError:
                        pass
                out.add(parts[0].strip().lower())
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------------------------
# Hạn mức đã lưu
# --------------------------------------------------------------------------------------------
def parse_tokens(s) -> float | None:
    m = re.match(r"\s*([\d\.]+)\s*([KMB]?)", str(s or ""))
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)]


def parse_reset_date(text: str, now: datetime | None = None):
    """'Oct 17' -> ngày reset sắp tới (năm hiện tại, nếu đã qua quá xa thì sang năm sau). None nếu không đọc được."""
    now = now or datetime.now()
    try:
        d = datetime.strptime(f"{str(text).strip()} {now.year}", "%b %d %Y")
    except ValueError:
        return None
    if d < now - timedelta(days=200):
        d = d.replace(year=now.year + 1)
    return d


def quota_exhausted(q: dict | None) -> bool:
    """Hạn mức đã đọc cho thấy cạn: tuần dùng 100% và token phụ không còn."""
    if not isinstance(q, dict) or not q.get("found", True):
        return False
    weekly = q.get("weekly_used_pct")
    left = parse_tokens(q.get("extra_left"))
    if weekly is None or left is None:
        return False
    return weekly >= 100 and left <= 0


def quota_hold_until(q: dict | None, now: datetime | None = None):
    """Nếu hạn mức cạn và ngày reset còn ở tương lai: trả về mốc đó (datetime). Ngược lại None."""
    now = now or datetime.now()
    if not quota_exhausted(q):
        return None
    d = parse_reset_date(q.get("weekly_reset"), now)
    return d if d and d > now else None


# --------------------------------------------------------------------------------------------
# Lập kế hoạch
# --------------------------------------------------------------------------------------------
def _needs_relogin(acc: dict, now: float, expiring_hours: float) -> str | None:
    """Lý do cần đăng nhập lại, hoặc None."""
    note = str(acc.get("note") or "").lower()
    if any(m in note for m in _HARD_LOCK_MARKERS):
        return None
    cks = acc.get("cookies")
    if acc.get("essential_ok") is False or (isinstance(cks, dict) and cks and not cks.get("hatch_vml")):
        return "thiếu cookie máy ảo (hatch_vml)"
    if "no-file-input" in note:
        return "lỗi không có ô tải ảnh (no-file-input)"
    if acc.get("muse_locked"):
        if any(m in note for m in _SESSION_MARKERS):
            return "TK khoá vì phiên hết hạn"
        return None
    exp = acc.get("expires_at")
    if exp and now > float(exp) - expiring_hours * 3600:
        return "cookie sắp/đã hết hạn" if now < float(exp) else "cookie đã hết hạn"
    return None


def make_plan(cfg: dict, accounts: list[dict], creds: dict[str, str], state: dict | None = None,
              now: float | None = None) -> dict:
    """Trả về {"items": [{email, line, reason, kind, quota_note}], "skipped": {email: lý do}, "active": n}."""
    now = now or time.time()
    state = state or load_state()
    dead = _email_set(DEAD_FILE)
    checkpoint = _email_set(CHECKPOINT_FILE, max_age_hours=24)
    attempts = state.get("attempts", {})
    labels = {str(a.get("label") or "").strip().lower(): a for a in accounts}
    skipped: dict[str, str] = {}
    items: list[dict] = []

    def blocked(email: str) -> str | None:
        if email in dead:
            return "TK chết (file auto_login_dead.txt)"
        if email in checkpoint:
            return "Meta chuyển sang Instagram/xác minh (thử lại sau 24 giờ)"
        at = attempts.get(email) or {}
        fails = int(at.get("fails") or 0)
        ts = float(at.get("ts") or 0)
        if fails >= int(cfg["max_fails"]) and now - ts < float(cfg["give_up_hours"]) * 3600:
            return f"đã thất bại {fails} lần, tạm bỏ"
        if ts and now - ts < float(cfg["retry_hours"]) * 3600:
            return "vừa thử gần đây"
        return None

    # A) TK đang có trong hệ thống cần đăng nhập lại
    for email, acc in labels.items():
        if not email:
            continue
        reason = _needs_relogin(acc, now, float(cfg["expiring_hours"]))
        if not reason:
            continue
        if email not in creds:
            skipped[email] = "không có thông tin đăng nhập trong file nguồn"
            continue
        why = blocked(email)
        if why:
            skipped[email] = why
            continue
        hold = quota_hold_until(acc.get("quota"))
        if hold:
            skipped[email] = f"hết hạn mức, đợi reset {hold.strftime('%d/%m')}"
            continue
        left = parse_tokens((acc.get("quota") or {}).get("extra_left")) if acc.get("quota") else None
        items.append({"email": email, "line": creds[email], "reason": reason, "kind": "relogin",
                      "tokens": left if left is not None else -1.0})
    # ưu tiên TK còn nhiều token, rồi TK chưa biết hạn mức
    items.sort(key=lambda it: (-(it["tokens"]), it["email"]))

    # B) bổ sung email mới nếu thiếu TK dùng được
    active = sum(1 for a in accounts if a.get("enabled", True) and (a.get("cookies") or a.get("cookie_count"))
                 and not a.get("muse_locked") and not (a.get("expires_at") and float(a["expires_at"]) < now))
    if cfg.get("auto_add_new") and active + len(items) < int(cfg["min_active"]):
        need = min(int(cfg["max_new_per_run"]), int(cfg["min_active"]) - active - len(items))
        for email, line in creds.items():
            if need <= 0:
                break
            if email in labels or blocked(email):
                continue
            items.append({"email": email, "line": line, "reason": "bổ sung TK mới", "kind": "new", "tokens": -1.0})
            need -= 1

    items = items[: int(cfg["max_per_run"])]
    return {"items": items, "skipped": skipped, "active": active, "total": len(accounts), "creds": len(creds)}


def plan_summary(plan: dict) -> dict:
    return {
        "active": plan["active"], "total": plan["total"], "creds": plan["creds"],
        "will_run": [{"email": i["email"], "reason": i["reason"]} for i in plan["items"]],
        "skipped": [{"email": k, "reason": v} for k, v in list(plan["skipped"].items())[:200]],
    }


# --------------------------------------------------------------------------------------------
# Chạy
# --------------------------------------------------------------------------------------------
def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _quota_check_live(aid: str) -> dict | None:
    """Gọi app đang chạy đọc hạn mức bằng phiên riêng của TK. None nếu app không chạy / lỗi."""
    try:
        import requests
        headers = {"Authorization": f"Bearer {CFG.api_key}"} if CFG.api_key else {}
        r = requests.post(f"http://127.0.0.1:{CFG.port}/admin/accounts/{aid}/quota-check", headers=headers, timeout=170)
        if r.status_code == 200:
            j = r.json()
            return j.get("quota", j)
    except Exception:  # noqa: BLE001
        pass
    return None


def raw_accounts(store) -> list[dict]:
    """Danh sách TK đầy đủ trường (kể cả quota/cookies) — list_accounts() của Store chỉ trả bản rút gọn."""
    try:
        store._sync_from_disk()
    except Exception:  # noqa: BLE001
        pass
    return [dict(a) for a in store.accounts]


def run(cfg: dict | None = None, max_n: int | None = None, dry_run: bool = False) -> dict:
    cfg = cfg or load_config()
    if max_n:
        cfg = dict(cfg, max_per_run=int(max_n))
    from store import Store
    store = Store(CFG)
    accounts = raw_accounts(store)
    creds = read_credentials(cfg["sources"])
    state = load_state()
    plan = make_plan(cfg, accounts, creds, state)
    summary = plan_summary(plan)
    if dry_run or not plan["items"]:
        if not dry_run:
            state.update(last_run=time.time(), running=False, last_summary={"planned": 0, "ok": 0, "fail": 0, "note": "không có TK cần đăng nhập lại"},
                         planned=summary["will_run"])
            save_state(state)
        return {"plan": summary, "ran": False}

    import gui_auto_login as gal
    from PyQt6.QtCore import QCoreApplication, QTimer

    items = plan["items"]
    lines = [it["line"] for it in items]
    emails = [it["email"] for it in items]
    state.update(running=True, heartbeat=time.time(), started_at=time.time(), finished_at=None,
                 planned=summary["will_run"], progress={"done": 0, "total": len(items), "ok": 0, "fail": 0})
    save_state(state)
    _log(f"Bắt đầu đăng nhập lại {len(items)} TK ({sum(1 for i in items if i['kind'] == 'new')} TK mới), {cfg['threads']} luồng")

    app = QCoreApplication.instance() or QCoreApplication([])
    ctrl = gal.WorkerController(
        accounts=lines, num_threads=int(cfg["threads"]), headless=bool(cfg["headless"]), save_to_store=True,
        delay_between=2, use_proxy=True, use_fingerprint=True, proxy_source=str(cfg.get("proxy_source") or "app"),
        manual_captcha=not bool(cfg["headless"]),
    )
    final: dict[int, str] = {}

    def on_status(row: int, text: str, _color: str):
        final[row] = text
        st = load_state()
        st["heartbeat"] = time.time()
        save_state(st)

    ctrl.log_signal.connect(lambda m: _log(m))
    ctrl.status_signal.connect(on_status)
    ctrl.progress_signal.connect(lambda done, total, ok, fail: _on_progress(done, total, ok, fail))
    ctrl.finished_signal.connect(app.quit)
    # đề phòng treo: tối đa 90 phút cho 1 lượt
    QTimer.singleShot(90 * 60 * 1000, app.quit)
    ctrl.start()
    app.exec()
    try:
        ctrl.stop()
        ctrl.wait(20000)
    except Exception:  # noqa: BLE001
        pass

    # tổng kết từng TK
    results = {}
    ok = fail = 0
    now = time.time()
    st = load_state()
    for idx, email in enumerate(emails):
        text = final.get(idx, "không có kết quả")
        good = text.strip().startswith("✓") or "thành công" in text.lower()
        results[email] = {"status": "ok" if good else "fail", "msg": text, "ts": now, "kind": items[idx]["kind"]}
        at = st["attempts"].setdefault(email, {})
        at["ts"] = now
        at["fails"] = 0 if good else int(at.get("fails") or 0) + 1
        ok += 1 if good else 0
        fail += 0 if good else 1
    st["results"].update(results)

    # kiểm tra hạn mức cho TK vừa đăng nhập được
    quota_notes = {}
    store2 = Store(CFG)
    by_label = {str(a.get("label") or "").strip().lower(): a for a in raw_accounts(store2)}
    for email, r in results.items():
        if r["status"] != "ok":
            continue
        acc = by_label.get(email)
        if not acc:
            continue
        q = _quota_check_live(acc["id"])
        if q is None:
            quota_notes[email] = "chưa đọc được hạn mức (app không chạy hoặc trang tải chậm)"
            continue
        hold = quota_hold_until(q)
        never_worked = int(acc.get("success_count") or 0) == 0
        if hold and never_worked:
            note = f"Hết hạn mức (cạn token), đợi reset {hold.strftime('%d/%m')}"
            store2.update_account(acc["id"], quota_hold_until=int(hold.timestamp()))
            store2.set_locked(acc["id"], True, note)
            quota_notes[email] = note
        else:
            left = q.get("extra_left") if isinstance(q, dict) else None
            quota_notes[email] = f"còn {left}, tuần {q.get('weekly_used_pct')}%" if isinstance(q, dict) else "đã đọc"
    for email, n in quota_notes.items():
        st["results"][email]["quota"] = n

    st.update(running=False, finished_at=time.time(), last_run=time.time(),
              last_summary={"planned": len(items), "ok": ok, "fail": fail,
                            "held": sum(1 for n in quota_notes.values() if n.startswith("Hết hạn mức"))},
              progress={"done": len(items), "total": len(items), "ok": ok, "fail": fail})
    save_state(st)
    _log(f"Xong: {ok} thành công, {fail} thất bại")
    return {"plan": summary, "ran": True, "ok": ok, "fail": fail, "results": results}


def _on_progress(done: int, total: int, ok: int, fail: int) -> None:
    st = load_state()
    st["progress"] = {"done": done, "total": total, "ok": ok, "fail": fail}
    st["heartbeat"] = time.time()
    save_state(st)


def main() -> int:
    ap = argparse.ArgumentParser(description="Tự động đăng nhập lại TK Muse hết phiên (kèm kiểm tra hạn mức)")
    ap.add_argument("--dry-run", action="store_true", help="chỉ in kế hoạch, không đăng nhập")
    ap.add_argument("--run", action="store_true", help="chạy thật")
    ap.add_argument("--max", type=int, default=0, help="tối đa số TK lượt này (ghi đè cấu hình)")
    ap.add_argument("--show", action="store_true", help="hiện cửa sổ Chrome (không chạy ngầm) — dùng khi Meta hay bắt xác minh")
    args = ap.parse_args()
    if not (args.dry_run or args.run):
        ap.print_help()
        return 2
    if args.run and is_running():
        print("Đang có một lượt khác chạy, bỏ qua.")
        return 3
    cfg = load_config()
    if args.show:
        cfg["headless"] = False
    res = run(cfg=cfg, max_n=args.max or None, dry_run=args.dry_run)
    p = res["plan"]
    print(f"TK dùng được: {p['active']}/{p['total']} | email có thông tin đăng nhập: {p['creds']}")
    print(f"Sẽ đăng nhập lại: {len(p['will_run'])}")
    for i in p["will_run"][:50]:
        print("   +", i["email"], "-", i["reason"])
    reasons: dict[str, int] = {}
    for s in p["skipped"]:
        reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
    print("Bỏ qua:", reasons)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
