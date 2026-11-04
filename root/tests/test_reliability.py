"""Run: python tests/test_reliability.py [path/to/app.py]; không dùng TK/Database/mạng thật.

Kiểm tra: lỗi do TK -> trả SP về hàng chờ (tối đa 3 lần), lỗi do SP -> failed; chất lượng TK giảm việc cho TK hay lỗi;
endpoint gợi ý số luồng; dọn SP processing treo; thống kê băng thông proxy; Webshare đổi proxy cho TK có proxy bị thay."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).parent))
from test_async_images import Client  # noqa: E402  (client ASGI tối giản dùng chung)


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


async def check(source, home):
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home, MUSE2API_KEY="test-only", MUSE2API_PUBLIC_BASE="")
    sys.path.insert(0, str(source.parent))
    spec = importlib.util.spec_from_file_location("reliability_target", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    se = module.shopee_engine
    store = module.store

    # ---- trả SP về hàng chờ khi lỗi do TK ----
    reported = []
    se.report_job_completion = lambda server, key, item, status, path=None: reported.append((str(item), status)) or True
    se.REQUEUE_FILE = os.path.join(home, "rq.json")
    se._requeue_counts = None
    headers = {"Authorization": "Bearer test-only"}
    async with Client(module.app, headers) as c:
        acc_err = "附加参考图失败，已停止生成 (no-file-input)"
        for i in range(1, 5):
            r = await c.post("/api/shopee/complete", json={"item_id": "1001", "status": "failed", "error": acc_err})
            expect(r.status_code == 200, f"complete HTTP {r.status_code}")
        statuses = [s for i, s in reported if i == "1001"]
        expect(statuses == ["pending", "pending", "pending", "failed"], f"requeue sai: {statuses}")
        r = await c.post("/api/shopee/complete", json={"item_id": "2002", "status": "failed",
                                                      "error": "Mô hình không sinh video, chỉ phản hồi văn bản: 这个做不了"})
        expect(reported[-1] == ("2002", "failed") and r.json()["requeued"] == 0, "lỗi do SP phải đánh failed")
        r = await c.post("/api/shopee/complete", json={"item_id": "3003", "status": "failed"})
        expect(reported[-1] == ("3003", "failed"), "không có nội dung lỗi -> failed như cũ")
        await c.post("/api/shopee/complete", json={"item_id": "1001", "status": "completed", "video_path": "x.mp4"})
        expect(reported[-1] == ("1001", "completed") and "1001" not in se._load_requeue(), "completed phải xoá đếm")
        print("requeue_account_fault_to_pending=PASS product_fault_failed=PASS cap_3=PASS")

        # ---- chất lượng TK + gợi ý số luồng ----
        accs = [{"id": f"a{i}", "label": f"a{i}", "cookies": {"x": "1"}, "enabled": True} for i in range(6)]
        store.accounts = accs
        store._sync_from_disk = lambda: None
        store._save_accounts = lambda: None
        module.Store._OUTCOMES.clear(); module.Store._OUTCOME_TS.clear()
        for i in range(6):
            for _ in range(10):
                store.record_outcome("a0", True)       # tốt
                store.record_outcome("a1", False)      # hỏng
        r = await c.get("/api/shopee/capacity")
        d = r.json()
        expect(r.status_code == 200 and d["accounts_total"] == 6, f"capacity: {d}")
        base = d["jobs_per_account"]
        expect(d["slots"] == 4 * base + base, f"slots phải = 4 TK chưa có dữ liệu + 1 TK tốt, TK hỏng = 0: {d}")
        expect(d["accounts_usable"] == 5, f"TK hỏng không tính là dùng được: {d}")
        print(f"capacity_endpoint=PASS slots={d['slots']} usable={d['accounts_usable']}")

        # ---- dọn SP processing treo ----
        module.reclaim_stale_processing = lambda hours, market: 7 if (hours, market) == (24.0, "PH") else -1
        r = await c.post("/api/shopee/reclaim-stale", json={})
        expect(r.status_code == 200 and r.json()["reclaimed"] == 7, f"reclaim: {r.status_code} {r.text if hasattr(r, 'text') else ''}")
        print("reclaim_stale_endpoint=PASS")

        # ---- băng thông proxy ----
        import proxy_manager as pm
        pm.BW_DIR = os.path.join(home, "bw")
        os.makedirs(pm.BW_DIR, exist_ok=True)
        pm._APP_STARTED_AT = time.time() - 60
        for n, host_bytes in enumerate(({"muse.ai": 3_000_000, "x.example": 100}, {"muse.ai": 1_000_000})):
            with open(os.path.join(pm.BW_DIR, f"fw_{n}.json"), "w") as f:
                json.dump({"updated": int(time.time()), "bytes": host_bytes}, f)
        r = await c.get("/api/proxy/bandwidth")
        d = r.json()
        expect(d["hosts"][0] == {"host": "muse.ai", "mb": 4.0}, f"bandwidth: {d}")
        print("proxy_bandwidth_endpoint=PASS")


def main():
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "app.py"
    with tempfile.TemporaryDirectory() as home:
        asyncio.run(check(source, home))
    print("PASS reliability regression")


if __name__ == "__main__":
    main()
