import json
import os
import sys
import time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from DrissionPage import ChromiumPage, ChromiumOptions

def fix_accounts():
    accounts_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "accounts.json")
    if not os.path.isfile(accounts_file):
        print(f"Không tìm thấy file: {accounts_file}")
        return

    with open(accounts_file, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    to_fix = [a for a in accounts if "hatch_gw" not in a.get("cookies", {})]
    print(f"Tổng số tài khoản: {len(accounts)}, số tài khoản thiếu hatch_gw: {len(to_fix)}")
    if not to_fix:
        print("Tất cả tài khoản đều đã có đủ 4 cookie cốt lõi!")
        return

    co = ChromiumOptions()
    co.headless(True)
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if os.path.isfile(chrome_path):
        co.set_browser_path(chrome_path)

    page = ChromiumPage(co)
    fixed_count = 0

    try:
        for idx, acc in enumerate(to_fix, 1):
            email = acc.get("label")
            print(f"[{idx}/{len(to_fix)}] Đang bổ sung hatch_gw cho: {email} ...")
            
            # Reset cookie
            try:
                page.run_cdp("Network.clearBrowserCookies")
            except Exception:
                pass
            
            page.get("https://muse.ai")
            for k, v in acc.get("cookies", {}).items():
                if v:
                    page.set.cookies({k: v, "domain": ".muse.ai", "path": "/"})
            
            page.get("https://muse.ai/thread/new")
            
            got_gw = False
            for _ in range(8):
                time.sleep(1)
                try:
                    cdp = page.run_cdp("Storage.getCookies")
                    clist = cdp.get("cookies", [])
                    gw_cookie = next((c for c in clist if c.get("name") == "hatch_gw"), None)
                    if gw_cookie:
                        acc["cookies"]["hatch_gw"] = gw_cookie["value"]
                        if not acc.get("cookies_exp"):
                            acc["cookies_exp"] = {}
                        if gw_cookie.get("expires", 0) > 0:
                            acc["cookies_exp"]["hatch_gw"] = int(float(gw_cookie["expires"]))
                        got_gw = True
                        break
                except Exception:
                    pass
            
            if got_gw:
                fixed_count += 1
                print(f" -> Thành công! Đã nạp hatch_gw cho {email}")
            else:
                print(f" -> Không lấy được hatch_gw cho {email}")

        # Lưu lại accounts.json
        with open(accounts_file, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=1, ensure_ascii=False)
        print(f"\n===> HOÀN TẤT: Đã bổ sung thành công hatch_gw cho {fixed_count}/{len(to_fix)} tài khoản!")

    finally:
        try:
            page.quit()
        except Exception:
            pass

if __name__ == "__main__":
    fix_accounts()
