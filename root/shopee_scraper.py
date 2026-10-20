"""
Shopee Scraper cho MuseAI — Tự động bóc tách link sản phẩm Shopee từ file text / URL:
Lấy tiêu đề (og:title), ảnh đại diện chất lượng cao (og:image), mô tả và ItemID.
Sử dụng curl_cffi giả lập Chrome 124 TLS + Facebook ExternalHit UA để tránh hoàn toàn captcha/anti-bot của Shopee.
"""
from __future__ import annotations

import concurrent.futures
import html as html_mod
import logging
import os
import re
import urllib.parse

log = logging.getLogger("shopee_scraper")

# curl_cffi trên máy mới cài có thể "có nhưng hỏng" (thiếu/lệch DLL -> OSError, không phải ImportError) -> trước đây
# làm sập cả chức năng Import Link. Hỏng kiểu gì cũng chuyển sang requests thường.
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except Exception as _cffi_err:  # noqa: BLE001
    cffi_requests = None
    HAS_CURL_CFFI = False
    log.warning("curl_cffi không dùng được (%s: %s) -> cào Shopee bằng requests thường",
                type(_cffi_err).__name__, _cffi_err)

# Sử dụng WhatsApp Link-Preview User-Agent: Shopee đã chặn facebookexternalhit/Twitterbot/Googlebot (HTTP 403),
# nhưng mở hoàn toàn SSR meta og:title + og:image (down-*.img.susercontent.com) cho WhatsApp preview bot.
FACEBOOK_UA = "WhatsApp/2.23.20.0"
FALLBACK_UAS = (
    "WhatsApp/2.23.20.0",
    "WhatsApp/2.24.12.78 A",
)

RE_OG_TITLE = re.compile(r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']*)["\']', re.IGNORECASE)
RE_OG_TITLE_ALT = re.compile(r'<meta[^>]*content=["\']([^"\']*)["\'][^>]*property=["\']og:title["\']', re.IGNORECASE)

RE_OG_DESC = re.compile(r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']*)["\']', re.IGNORECASE)
RE_OG_DESC_ALT = re.compile(r'<meta[^>]*content=["\']([^"\']*)["\'][^>]*property=["\']og:description["\']', re.IGNORECASE)

RE_OG_IMAGE = re.compile(r'<meta[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']*)["\']', re.IGNORECASE)
RE_OG_IMAGE_ALT = re.compile(r'<meta[^>]*content=["\']([^"\']*)["\'][^>]*property=["\']og:image["\']', re.IGNORECASE)

RE_URL_DASH_I = re.compile(r'-i\.(\d+)\.(\d+)')
RE_URL_PRODUCT = re.compile(r'(?:product|opaanlp)/(\d+)/(\d+)')
RE_URL_ANY_ID = re.compile(r'(\d{8,15})')


def extract_ids_from_url(url: str) -> tuple[str, str]:
    """Trích xuất shop_id và item_id từ URL Shopee."""
    m = RE_URL_DASH_I.search(url)
    if m:
        return m.group(1), m.group(2)
    m = RE_URL_PRODUCT.search(url)
    if m:
        return m.group(1), m.group(2)
    # Tìm chuỗi số dài (thường là item_id)
    nums = RE_URL_ANY_ID.findall(url)
    if nums:
        return "", nums[-1]
    # Fallback tạo mã băm nếu là link rút gọn chưa redirect
    fallback_id = str(abs(hash(url)) % 10000000000)
    return "", fallback_id


def resolve_canonical_url(url: str) -> str:
    """Chuẩn hóa URL sản phẩm Shopee về dạng /product/{shop_id}/{item_id}."""
    shop_id, item_id = extract_ids_from_url(url)
    if shop_id and item_id and shop_id != "0":
        domain = urllib.parse.urlparse(url).netloc or "shopee.vn"
        return f"https://{domain}/product/{shop_id}/{item_id}"
    return url


def parse_shopee_links(text: str) -> list[str]:
    """Tách danh sách link Shopee hợp lệ từ nội dung văn bản / file TXT."""
    lines = text.splitlines()
    valid_links: list[str] = []
    seen = set()
    
    url_pattern = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        matches = url_pattern.findall(s)
        for u in matches:
            u_clean = u.rstrip(".,;")
            if u_clean not in seen:
                seen.add(u_clean)
                valid_links.append(u_clean)
    return valid_links


def _clean_shopee_image_url(img_url: str) -> str:
    s = html_mod.unescape(str(img_url or "")).strip()
    if not s:
        return ""
    if s.startswith("//"):
        s = "https:" + s
    if "@resize" in s:
        s = s.split("@resize")[0]
    if s.endswith("_tn"):
        s = s[:-3]
    elif "_tn." in s:
        s = s.replace("_tn.", ".")
    return s


def _http_get_html(target_url: str, timeout: int = 15, ua: str = FACEBOOK_UA) -> tuple[str, str, int]:
    """Tải HTML từ Shopee với User-Agent chỉ định. Trả về (html, final_url, status_code)."""
    if HAS_CURL_CFFI:
        try:
            resp = cffi_requests.get(
                target_url,
                impersonate="chrome124",
                headers={"User-Agent": ua},
                timeout=timeout,
                allow_redirects=True,
            )
            return resp.text or "", str(resp.url or target_url), int(resp.status_code or 0)
        except Exception as e:  # noqa: BLE001
            log.warning("curl_cffi lỗi khi cào %s (%s) -> thử lại bằng requests", target_url, e)
    import requests as std_requests
    resp = std_requests.get(
        target_url,
        headers={"User-Agent": ua},
        timeout=timeout,
        allow_redirects=True,
    )
    return resp.text or "", str(resp.url or target_url), int(resp.status_code or 0)


def _lookup_db_product(item_id: str) -> dict | None:
    """Fallback tra cứu tên, ảnh và shop_id từ PostgreSQL shopee_products khi link bị Shopee ẩn hoặc chỉ có item_id."""
    iid = str(item_id or "").strip()
    if not iid.isdigit():
        return None
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(
            host=os.environ.get("SHOPEE_DB_HOST", "100.79.170.67"),
            port=int(os.environ.get("SHOPEE_DB_PORT", "5432")),
            dbname=os.environ.get("SHOPEE_DB_NAME", "shopee_db"),
            user=os.environ.get("SHOPEE_DB_USER", "postgres"),
            password=os.environ.get("SHOPEE_DB_PASS", "postgres123"),
            connect_timeout=3,
        )
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            cur.execute(
                "SELECT item_id, shop_id, market, name, image_url FROM shopee_products WHERE item_id = %s LIMIT 1;",
                (int(iid),),
            )
            row = cur.fetchone()
            cur.close()
            if row:
                mkt = str(row.get("market") or "PH").lower()
                mkt_dom = {"vn": "vn", "ph": "ph", "id": "co.id", "my": "com.my", "th": "co.th", "sg": "sg", "tw": "tw"}.get(mkt, "ph")
                raw_img = _clean_shopee_image_url(row.get("image_url") or "")
                if raw_img and not raw_img.startswith(("http://", "https://", "data:")):
                    raw_img = f"https://cf.shopee.{mkt_dom}/file/{raw_img}"
                return {
                    "item_id": str(row["item_id"]),
                    "shop_id": str(row.get("shop_id") or ""),
                    "market": str(row.get("market") or ""),
                    "name": str(row.get("name") or "").strip(),
                    "image": raw_img,
                }
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_shopee_product(url: str, timeout: int = 15) -> dict | None:
    """Cào thông tin chi tiết (tiêu đề, ảnh, mô tả) của 1 link Shopee."""
    raw_url = url.strip()
    shop_id, item_id = extract_ids_from_url(raw_url)

    # Nếu URL chỉ có item_id (hoặc shop_id == "0"), thử tra shop_id từ Database trước để tạo canonical URL chuẩn
    db_row = None
    if item_id and item_id.isdigit() and (not shop_id or shop_id == "0"):
        db_row = _lookup_db_product(item_id)
        if db_row and db_row.get("shop_id") and db_row["shop_id"] != "0":
            shop_id = db_row["shop_id"]
            domain = urllib.parse.urlparse(raw_url).netloc or "shopee.vn"
            target_url = f"https://{domain}/product/{shop_id}/{item_id}"
        else:
            target_url = resolve_canonical_url(raw_url)
    else:
        target_url = resolve_canonical_url(raw_url)

    html = ""
    final_url = target_url

    try:
        for ua in FALLBACK_UAS:
            html, final_url, status_code = _http_get_html(target_url, timeout=timeout, ua=ua)
            # Nếu link rút gọn (s.shopee.*, shp.ee) redirect sang /opaanlp/{shop}/{item}, chuyển về /product/{shop}/{item}
            s_id, i_id = extract_ids_from_url(final_url)
            if s_id and i_id and s_id != "0":
                shop_id, item_id = s_id, i_id
                if not (RE_OG_IMAGE.search(html) or RE_OG_IMAGE_ALT.search(html)):
                    dom = urllib.parse.urlparse(final_url).netloc or urllib.parse.urlparse(target_url).netloc or "shopee.vn"
                    canon_after = f"https://{dom}/product/{s_id}/{i_id}"
                    if canon_after != target_url:
                        html, final_url, status_code = _http_get_html(canon_after, timeout=timeout, ua=ua)
            if status_code == 200 and (RE_OG_IMAGE.search(html) or RE_OG_IMAGE_ALT.search(html)):
                break
    except Exception as e:
        log.warning(f"Lỗi khi cào link {target_url}: {e}")
        if not db_row and item_id and item_id.isdigit():
            db_row = _lookup_db_product(item_id)
        if not db_row:
            return None

    # Cập nhật lại shop_id và item_id từ URL cuối cùng sau redirect (ví dụ shp.ee -> shopee.vn/...-i.123.456)
    if not item_id or item_id.startswith("-") or len(item_id) < 6:
        s_id, i_id = extract_ids_from_url(final_url)
        if i_id:
            item_id = i_id
            shop_id = s_id

    info = {
        "item_id": item_id or str(abs(hash(raw_url)) % 10000000000),
        "shop_id": shop_id,
        "name": "",
        "description": "",
        "image": "",
        "images": [],
        "url": final_url,
        "price": 0,
        "sold": 0,
        "commission": "0",
        "_status": "waiting",
        "_is_imported": True
    }

    # 1. Trích xuất Tiêu đề (og:title)
    m_title = RE_OG_TITLE.search(html) or RE_OG_TITLE_ALT.search(html)
    if m_title:
        title = html_mod.unescape(m_title.group(1)).strip()
        for sep in [" | Shopee Việt Nam", " | Shopee Philippines", " | Shopee Malaysia", " | Shopee Indonesia", " | Shopee Thailand", " | Shopee Singapore", " | Shopee", " - Shopee"]:
            idx = title.find(sep)
            if idx > 0:
                title = title[:idx].strip()
                break
        info["name"] = title

    # Nếu Shopee trả về trang chủ / anti-bot (không có og:title sản phẩm thật)
    bad_titles = (
        "Mua và Bán Trên Ứng Dụng",
        "Shopee Verification",
        "Shop Online with Promos and Vouchers",
        "Shopee Việt Nam",
        "Shopee Philippines",
    )
    if any(bt in info["name"] for bt in bad_titles):
        info["name"] = ""

    # 2. Trích xuất Mô tả (og:description)
    m_desc = RE_OG_DESC.search(html) or RE_OG_DESC_ALT.search(html)
    if m_desc:
        info["description"] = html_mod.unescape(m_desc.group(1)).strip()

    # 3. Trích xuất Ảnh đại diện (og:image)
    m_img = RE_OG_IMAGE.search(html) or RE_OG_IMAGE_ALT.search(html)
    if m_img:
        img_url = _clean_shopee_image_url(m_img.group(1))
        if img_url:
            info["image"] = img_url
            info["images"] = [img_url]

    # 4. Fallback sang PostgreSQL Database nếu SP bị ẩn trên web hoặc thiếu ảnh/tên
    if (not info["image"] or not info["name"]) and info["item_id"].isdigit():
        if db_row is None:
            db_row = _lookup_db_product(info["item_id"])
        if db_row:
            if not info["name"] and db_row.get("name"):
                info["name"] = db_row["name"]
            if not info["image"] and db_row.get("image"):
                info["image"] = db_row["image"]
                info["images"] = [db_row["image"]]

    # Nếu không có tên, đặt tên mặc định theo item_id
    if not info["name"]:
        info["name"] = f"Shopee Item {info['item_id']}"

    return info


MARKET_DOMAINS = {
    "PH": "shopee.ph",
    "VN": "shopee.vn",
    "MY": "shopee.com.my",
    "TH": "shopee.co.th",
    "ID": "shopee.co.id",
    "SG": "shopee.sg",
    "TW": "shopee.tw",
}


def fetch_image_url_by_item_id(item_id: str, market: str = "PH", shop_id: str = "") -> str:
    """Cào link ảnh đại diện khi chỉ còn ItemID (clip temp mồ côi, không còn dữ liệu sản phẩm trên giao diện).

    Trả về "" nếu không lấy được (Shopee chặn, sai thị trường, sản phẩm đã bị gỡ...).
    """
    iid = str(item_id or "").strip()
    if not iid.isdigit():
        return ""
    domain = MARKET_DOMAINS.get(str(market or "PH").strip().upper(), "shopee.ph")
    sid = str(shop_id or "").strip() or "0"
    try:
        info = fetch_shopee_product(f"https://{domain}/product/{sid}/{iid}")
    except Exception as e:
        log.warning("Cào ảnh theo ItemID %s thất bại: %s", iid, e)
        return ""
    return str((info or {}).get("image") or "").strip()


def scrape_multiple_shopee_links(links: list[str], max_workers: int = 5) -> list[dict]:
    """Cào thông tin hàng loạt link Shopee chạy song song đa luồng nhanh chóng."""
    results: list[dict] = []
    if not links:
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(fetch_shopee_product, link): link for link in links}
        for future in concurrent.futures.as_completed(future_map):
            link = future_map[future]
            try:
                prod_info = future.result()
                if prod_info:
                    prod_info["_input_url"] = link
                    results.append(prod_info)
                else:
                    _, iid = extract_ids_from_url(link)
                    results.append({
                        "item_id": iid or str(abs(hash(link)) % 10000000000),
                        "name": f"Shopee Link ({link[-25:]})",
                        "image": "",
                        "images": [],
                        "url": link,
                        "_input_url": link,
                        "_status": "waiting",
                        "_need_fetch": True,
                        "_is_imported": True
                    })
            except Exception as e:
                log.warning(f"Lỗi future cào link {link}: {e}")
                _, iid = extract_ids_from_url(link)
                results.append({
                    "item_id": iid or str(abs(hash(link)) % 10000000000),
                    "name": f"Shopee Link ({link[-25:]})",
                    "image": "",
                    "images": [],
                    "url": link,
                    "_input_url": link,
                    "_status": "waiting",
                    "_need_fetch": True,
                    "_is_imported": True
                })

    # Giữ nguyên thứ tự ban đầu của danh sách link
    link_order = {link: i for i, link in enumerate(links)}
    results.sort(key=lambda p: link_order.get(p.get("_input_url") or p.get("url", ""), 999999))
    for p in results:
        p.pop("_input_url", None)
    return results

