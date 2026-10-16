"""Run: python tests/test_relogin.py; không đăng nhập thật, không dùng TK/mạng thật.

Kiểm tra bộ lập kế hoạch tự đăng nhập lại: chọn đúng TK hết phiên, bỏ qua TK khoá hẳn/chết/cạn hạn mức/vừa thử, ưu tiên TK còn
nhiều token, bổ sung TK mới khi thiếu, và các endpoint /api/relogin/*."""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent))
from test_async_images import Client  # noqa: E402


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def acc(label, **kw):
    d = {"id": label.split("@")[0], "label": label, "enabled": True, "cookies": {"x": "1"}, "muse_locked": False,
         "expires_at": time.time() + 86400 * 2}
    d.update(kw)
    return d


def check_plan(ar, home):
    cfg = ar.load_config()
    now = time.time()
    reset_future = (datetime.now() + timedelta(days=3)).strftime("%b %d")
    reset_past = (datetime.now() - timedelta(days=2)).strftime("%b %d")
    exhausted = {"found": True, "weekly_used_pct": 100, "extra_left": "0 tokens left", "extra_used_pct": 100}
    accounts = [
        acc("expired@x.com", muse_locked=True, note="Xác thực thất bại khi duy trì: 会话认证失败 (/api/session HTTP 401)"),
        acc("noassign@x.com", muse_locked=True, note="Chưa xác nhận duy trì: /api/session HTTP 200 未返回 assigned 会话"),
        acc("banned@x.com", muse_locked=True, note="Muse ngắt kết nối TK (Muse is disconnected) — cần gửi ticket"),
        acc("vmoff@x.com", muse_locked=True, note="Phiên hợp lệ · Tự động duy trì (VM: DISABLED)"),
        acc("dead@x.com", muse_locked=True, note="Xác thực thất bại khi duy trì: HTTP 401"),
        acc("nocreds@x.com", muse_locked=True, note="Xác thực thất bại khi duy trì: HTTP 401"),
        acc("hold@x.com", muse_locked=True, note="HTTP 401", quota=dict(exhausted, weekly_reset=reset_future)),
        acc("reset@x.com", muse_locked=True, note="HTTP 401", quota=dict(exhausted, weekly_reset=reset_past)),
        acc("rich@x.com", muse_locked=True, note="HTTP 401", quota={"found": True, "weekly_used_pct": 100, "extra_left": "1.5B tokens left", "weekly_reset": reset_future}),
        acc("soon@x.com", expires_at=now + 3600),
        acc("healthy@x.com"),
        acc("tried@x.com", muse_locked=True, note="HTTP 401"),
        acc("failed3@x.com", muse_locked=True, note="HTTP 401"),
    ]
    creds = {e: f"{e}|pw|tok|cid" for e in
             ["expired@x.com", "noassign@x.com", "banned@x.com", "vmoff@x.com", "dead@x.com", "hold@x.com", "reset@x.com",
              "rich@x.com", "soon@x.com", "healthy@x.com", "tried@x.com", "failed3@x.com", "fresh1@x.com", "fresh2@x.com", "fresh3@x.com"]}
    with open(ar.DEAD_FILE, "w", encoding="utf-8") as f:
        f.write("dead@x.com|Muse is disconnected|2026-10-14 10:00:00\n")
    state = {"attempts": {"tried@x.com": {"ts": now - 600, "fails": 1}, "failed3@x.com": {"ts": now - 7200, "fails": 3}}, "results": {}}
    plan = ar.make_plan(cfg, accounts, creds, state, now)
    names = [i["email"] for i in plan["items"]]
    sk = plan["skipped"]
    expect({"expired@x.com", "noassign@x.com", "reset@x.com", "rich@x.com", "soon@x.com"} == set(names), f"chọn sai: {names}")
    expect(names[0] == "rich@x.com", f"TK còn nhiều token phải đứng đầu: {names}")
    for e in ("banned@x.com", "vmoff@x.com", "healthy@x.com"):
        expect(e not in names, f"{e} không được chọn")
    expect("dead@x.com" in sk and "chết" in sk["dead@x.com"], f"TK chết: {sk.get('dead@x.com')}")
    expect("nocreds@x.com" in sk and "không có thông tin" in sk["nocreds@x.com"], "thiếu thông tin đăng nhập phải bị bỏ qua")
    expect("hold@x.com" in sk and "reset" in sk["hold@x.com"], f"cạn hạn mức chưa tới ngày reset phải bỏ qua: {sk.get('hold@x.com')}")
    expect("tried@x.com" in sk and "vừa thử" in sk["tried@x.com"], "vừa thử phải bỏ qua")
    expect("failed3@x.com" in sk and "thất bại" in sk["failed3@x.com"], "thất bại nhiều lần phải tạm bỏ")
    expect(plan["active"] == 2, f"TK dùng được: {plan['active']} (soon + healthy)")
    print("plan_selects_expired_sessions=PASS skips_hard_locks_dead_exhausted_recent=PASS prioritizes_rich_accounts=PASS")

    # bổ sung TK mới khi thiếu
    cfg2 = dict(cfg, auto_add_new=True, min_active=12, max_new_per_run=2, max_per_run=50)
    plan2 = ar.make_plan(cfg2, accounts, creds, state, now)
    kinds = [i["kind"] for i in plan2["items"]]
    expect(kinds.count("new") == 2, f"phải bổ sung đúng 2 TK mới: {kinds}")
    expect(all(i["email"].startswith("fresh") for i in plan2["items"] if i["kind"] == "new"), "TK mới phải là email chưa có trong hệ thống")
    cfg3 = dict(cfg2, auto_add_new=False)
    expect(not [i for i in ar.make_plan(cfg3, accounts, creds, state, now)["items"] if i["kind"] == "new"], "tắt auto_add_new thì không bổ sung")
    cfg4 = dict(cfg, max_per_run=2)
    expect(len(ar.make_plan(cfg4, accounts, creds, state, now)["items"]) == 2, "phải giới hạn max_per_run")
    print("auto_add_new_tops_up_only_when_low=PASS max_per_run_cap=PASS")

    # đọc thông tin đăng nhập
    d = os.path.join(home, "src")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "a.txt"), "w", encoding="utf-8") as f:
        f.write("one@h.com|p1|t1|c1\nbad line\nshort@h.com|p\nmail@nguyenmaucole.io.vn\n# c\n")
    with open(os.path.join(d, "auto_login_success_cookies.txt"), "w", encoding="utf-8") as f:
        f.write("skipme@h.com|cookie=1\n")
    c = ar.read_credentials([d])
    expect(set(c) == {"one@h.com", "mail@nguyenmaucole.io.vn"}, f"đọc thông tin đăng nhập sai: {set(c)}")
    print("read_credentials_filters_lines_and_files=PASS")

    # hạn mức
    expect(ar.parse_tokens("1.1B tokens left") == 1.1e9 and ar.parse_tokens("356M tokens left") == 356e6 and ar.parse_tokens("0 tokens left") == 0, "parse_tokens")
    expect(ar.quota_hold_until(dict(exhausted, weekly_reset=reset_future)) is not None, "hold future")
    expect(ar.quota_hold_until(dict(exhausted, weekly_reset=reset_past)) is None, "hold past")
    expect(ar.quota_hold_until({"weekly_used_pct": 100, "extra_left": "5M tokens left", "weekly_reset": reset_future}) is None, "còn token thì không giữ")
    print("quota_parsing_and_hold=PASS")


async def check_api(source, home):
    sys.path.insert(0, str(source.parent))
    spec = importlib.util.spec_from_file_location("relogin_target", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    import auto_relogin as ar
    started = []
    module._spawn_relogin = lambda max_n=None: started.append(max_n) or True
    module._relogin_running = lambda: False
    headers = {"Authorization": "Bearer test-only"}
    async with Client(module.app, headers) as c:
        r = await c.get("/api/relogin/status")
        d = r.json()
        expect(r.status_code == 200 and "config" in d and d["running"] is False, f"status: {r.status_code}")
        r = await c.post("/api/relogin/config", json={"enabled": True, "interval_hours": 12, "max_per_run": 7, "bogus": 1, "sources": "A:\\x; B:\\y"})
        cfg = r.json()["config"]
        expect(cfg["enabled"] is True and cfg["interval_hours"] == 12 and cfg["max_per_run"] == 7 and cfg["sources"] == ["A:\\x", "B:\\y"] and "bogus" not in cfg, f"config: {cfg}")
        expect(ar.load_config()["max_per_run"] == 7, "cấu hình phải được lưu")
        r = await c.post("/api/relogin/run", json={"dry_run": True})
        expect(r.status_code == 200 and r.json()["dry_run"] is True and not started, "dry_run không được chạy thật")
        r = await c.post("/api/relogin/run", json={"max": 3})
        expect(r.json()["started"] is True and started == [3], f"run: {r.json()} {started}")
    print("relogin_endpoints_status_config_dry_run_run=PASS")


def main():
    source = Path(__file__).resolve().parents[1] / "app.py"
    with tempfile.TemporaryDirectory() as home:
        os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home, MUSE2API_KEY="test-only", MUSE2API_PUBLIC_BASE="")
        sys.path.insert(0, str(source.parent))
        import auto_relogin as ar
        os.makedirs(ar.DATA_DIR, exist_ok=True)
        check_plan(ar, home)
        asyncio.run(check_api(source, home))
    print("PASS relogin regression")


if __name__ == "__main__":
    main()
