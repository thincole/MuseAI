"""Tích hợp đọc hòm thư & bóc mã OTP 6 số qua API:
  1. DongVanFB OAuth2 API (chính): https://tools.dongvanfb.net/api/get_messages_oauth2 & get_code_oauth2
  2. Smail1s API (dự phòng khi DongVanFB không lấy được): https://smail1s.com/api-docs
     - POST https://smail1s.com/get_messages  {mode: "oauth" | "graph" | "roundcube" | "emailfake", data: ...}
  3. TempMail.Plus API (cho @merepost.com / tempmail.plus): https://tempmail.plus/api/mails
"""
from __future__ import annotations

import email.utils
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Callable

DONGVAN_API_MESSAGES = "https://tools.dongvanfb.net/api/get_messages_oauth2"
DONGVAN_API_CODE = "https://tools.dongvanfb.net/api/get_code_oauth2"
SMAIL1S_API_MESSAGES = "https://smail1s.com/get_messages"
TEMPMAIL_PLUS_API_MAILS = "https://tempmail.plus/api/mails"
TEMPMAIL_PLUS_DOMAINS = (
    "@merepost.com",
    "@mailto.plus",
    "@fexpost.com",
    "@fexbox.org",
    "@mailbox.in.ua",
    "@rover.info",
    "@fextemp.com",
    "@any.pink",
)

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    ),
}


def parse_mail_time(time_str: str) -> datetime | None:
    """Chuyển đổi chuỗi thời gian thư từ DongVanFB, Smail1s hoặc TempMail.Plus sang datetime (giờ nội bộ, naive).

    Hỗ trợ các định dạng:
      - 'HH:MM - D/M/YYYY' (DongVanFB, Smail1s oauth)
      - ISO-8601 UTC/Offset: '2026-11-13T17:33:02Z' (Smail1s graph / emailfake)
      - 'YYYY-MM-DD HH:MM[:SS]' (Smail1s roundcube / docs)
      - RFC 2822 Email Date: 'Sat, 14 Nov 2026 19:37:51 -0800' (TempMail.Plus detail API)
    """
    s = str(time_str or "").strip()
    if not s:
        return None
    m_dv = re.search(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m_dv:
        try:
            h, mi, d, mo, y = map(int, m_dv.groups())
            return datetime(y, mo, d, h, mi)
        except Exception:  # noqa: BLE001
            pass
    if "T" in s and re.match(r"^\d{4}-\d{2}-\d{2}T", s):
        try:
            iso = s[:-1] + "+00:00" if s.endswith("Z") else s
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        except Exception:  # noqa: BLE001
            pass
    m_ymd = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m_ymd:
        try:
            y, mo, d, h, mi, sec = m_ymd.groups()
            return datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0))
        except Exception:  # noqa: BLE001
            pass
    if "," in s or re.search(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", s):
        try:
            dt = email.utils.parsedate_to_datetime(s)
            if dt is not None:
                if dt.tzinfo is not None:
                    dt = dt.astimezone().replace(tzinfo=None)
                return dt
        except Exception:  # noqa: BLE001
            pass
    return None


def is_recent_mail_time(time_str: str, max_age_seconds: int = 3600) -> bool:
    """Kiểm tra thư có phải nhận gần đây trong vòng max_age_seconds (dung sai lệch giờ 15 phút) hay không."""
    mail_dt = parse_mail_time(time_str)
    if mail_dt is None:
        return True
    diff = (datetime.now() - mail_dt).total_seconds()
    return -900 <= diff <= max_age_seconds


def parse_dongvan_oauth2(acc_line: str, fallback_email: str = "") -> tuple[str, str, str] | None:
    """Tách (email, refresh_token, client_id) từ dòng tài khoản:
      - email|pass|refresh_token|client_id
      - email|refresh_token|client_id
    """
    parts = [p.strip() for p in (acc_line or "").split("|") if p.strip()]
    if len(parts) < 3:
        return None
    email = (fallback_email or parts[0]).strip()
    client_id = ""
    refresh_token = ""
    for p in parts[1:]:
        if not client_id and UUID_RE.match(p):
            client_id = p
        elif not refresh_token and (p.startswith("M.") or len(p) >= 40) and not UUID_RE.match(p):
            refresh_token = p
    if not refresh_token and len(parts) >= 4:
        refresh_token = parts[2]
    if not client_id and len(parts) >= 4:
        client_id = parts[3]
    elif not client_id and len(parts) == 3:
        client_id = parts[2]
        if not refresh_token:
            refresh_token = parts[1]
    if email and refresh_token and client_id:
        return email, refresh_token, client_id
    return None


def parse_roundcube_creds(acc_line: str, fallback_email: str = "") -> tuple[str, str] | None:
    """Tách (email, password) cho hòm thư Roundcube/IMAP không dùng OAuth2."""
    parts = [p.strip() for p in (acc_line or "").split("|") if p.strip()]
    if len(parts) < 2:
        return None
    email = (fallback_email or parts[0]).strip()
    pwd = parts[1].strip()
    if email and "@" in email and pwd and not pwd.startswith("http") and not pwd.startswith("M.") and len(pwd) < 64:
        return email, pwd
    return None


def format_smail1s_oauth_data(acc_line: str, fallback_email: str = "") -> str | None:
    """Chuẩn hoá chuỗi `data` gửi cho Smail1s mode 'oauth'/'graph'.

    Thực nghiệm trên https://smail1s.com/get_messages cho thấy Smail1s yêu cầu tối thiểu 4 trường
    phân tách bởi `|` (`email|pass_hoac_x|refresh_token|client_id`). Dòng 3 trường (`email|rt|cid`)
    sẽ tự chèn placeholder ở vị trí thứ 2 để đọc thành công cả 2 chế độ `oauth` và `graph`.
    """
    creds = parse_dongvan_oauth2(acc_line, fallback_email)
    if not creds:
        return None
    email, rt, cid = creds
    parts = [p.strip() for p in (acc_line or "").split("|") if p.strip()]
    if len(parts) >= 4 and parts[2] == rt and parts[3] == cid:
        if parts[0].lower() == email.lower():
            return "|".join(parts)
        return "|".join([email] + parts[1:])
    return f"{email}|x|{rt}|{cid}"


def is_tempmailplus_account(acc_line: str = "", email: str = "") -> bool:
    """Kiểm tra tài khoản có sử dụng hòm thư https://tempmail.plus (ví dụ @merepost.com) hay không."""
    em = str(email or "").strip().lower()
    if any(em.endswith(dom) for dom in TEMPMAIL_PLUS_DOMAINS):
        return True
    line_low = str(acc_line or "").lower()
    if "tempmail.plus" in line_low:
        return True
    return any(dom in line_low for dom in TEMPMAIL_PLUS_DOMAINS)


def parse_tempmailplus_creds(acc_line: str, fallback_email: str = "") -> tuple[str, str] | None:
    """Tách (email, epin) cho TempMail.Plus từ dòng `email|epin|...` hoặc `email`."""
    parts = [p.strip() for p in (acc_line or "").split("|") if p.strip()]
    email_addr = (fallback_email or (parts[0] if parts else "")).strip()
    if not any(email_addr.lower().endswith(dom) for dom in TEMPMAIL_PLUS_DOMAINS):
        for p in parts:
            if "@" in p and not p.startswith("http"):
                email_addr = p
                break
    if "@" not in email_addr or email_addr.startswith("http"):
        return None

    epin = ""
    m_ep = re.search(r"(?:^|[?&#|!])epin=([A-Za-z0-9_\-]+)", acc_line or "", re.IGNORECASE)
    if m_ep:
        epin = m_ep.group(1).strip()
    elif len(parts) >= 2:
        cand = parts[1]
        if not cand.startswith("http") and "@" not in cand and len(cand) < 64:
            epin = cand
    return email_addr, epin


def _get_json(url: str, timeout: float = 12.0) -> dict | list | None:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": DEFAULT_HEADERS["User-Agent"],
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def tempmailplus_fetch_messages(
    email: str,
    epin: str = "",
    limit: int = 20,
    timeout: float = 12.0,
) -> list[dict] | None:
    """Gọi GET https://tempmail.plus/api/mails?email=...&limit=20&epin=... lấy danh sách thư."""
    url = (
        f"{TEMPMAIL_PLUS_API_MAILS}"
        f"?email={urllib.parse.quote(email.strip())}"
        f"&limit={int(limit)}"
        f"&epin={urllib.parse.quote(epin.strip())}"
    )
    data = _get_json(url, timeout=timeout)
    if not isinstance(data, dict):
        return None
    if data.get("result") is True and isinstance(data.get("mail_list"), list):
        return data["mail_list"]
    return None


def tempmailplus_fetch_detail(
    email: str,
    mail_id: int | str,
    epin: str = "",
    timeout: float = 12.0,
) -> dict | None:
    """Gọi GET https://tempmail.plus/api/mails/<mail_id>?email=...&epin=... lấy nội dung và giờ RFC2822."""
    url = (
        f"{TEMPMAIL_PLUS_API_MAILS}/{urllib.parse.quote(str(mail_id))}"
        f"?email={urllib.parse.quote(email.strip())}"
        f"&epin={urllib.parse.quote(epin.strip())}"
    )
    data = _get_json(url, timeout=timeout)
    if isinstance(data, dict) and data.get("result") is True:
        return data
    return None


def _post_json(url: str, payload: dict, timeout: float = 15.0) -> dict | list | None:
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=DEFAULT_HEADERS,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def dongvan_api_fetch_messages(
    email: str, refresh_token: str, client_id: str, timeout: float = 10.0
) -> list[dict] | None:
    """Gọi POST https://tools.dongvanfb.net/api/get_messages_oauth2 lấy danh sách thư."""
    data = _post_json(
        DONGVAN_API_MESSAGES,
        {
            "email": email.strip(),
            "refresh_token": refresh_token.strip(),
            "client_id": client_id.strip(),
        },
        timeout=timeout,
    )
    if not isinstance(data, dict):
        return None
    msgs = data.get("messages")
    if isinstance(msgs, list) and (len(msgs) > 0 or data.get("status") is True):
        return msgs
    return None


def dongvan_api_fetch_code(
    email: str, refresh_token: str, client_id: str, timeout: float = 10.0
) -> dict | None:
    """Gọi POST https://tools.dongvanfb.net/api/get_code_oauth2 (dự phòng nội bộ DongVanFB)."""
    data = _post_json(
        DONGVAN_API_CODE,
        {
            "email": email.strip(),
            "refresh_token": refresh_token.strip(),
            "client_id": client_id.strip(),
            "type": "all",
        },
        timeout=timeout,
    )
    return data if isinstance(data, dict) else None


def smail1s_fetch_messages(
    acc_line: str,
    mode: str = "oauth",
    fallback_email: str = "",
    timeout: float = 15.0,
) -> list[dict] | None:
    """Gọi POST https://smail1s.com/get_messages theo tài liệu https://smail1s.com/api-docs.

    mode hỗ trợ: 'oauth' (IMAP/POP3), 'graph' (MS Graph API), 'roundcube' (email|pass), 'emailfake'.
    Trả về list[dict] nếu gọi thành công (kể cả rỗng khi error=None), hoặc None nếu API báo lỗi / mất kết nối.
    """
    data_str: str | None = None
    if mode in ("oauth", "graph"):
        data_str = format_smail1s_oauth_data(acc_line, fallback_email)
    elif mode == "roundcube":
        rc = parse_roundcube_creds(acc_line, fallback_email)
        if rc:
            data_str = f"{rc[0]}|{rc[1]}"
    elif mode == "emailfake":
        em = (fallback_email or (acc_line or "").split("|")[0]).strip()
        if "@" in em:
            data_str = em

    if not data_str:
        return None

    resp = _post_json(SMAIL1S_API_MESSAGES, {"mode": mode, "data": data_str}, timeout=timeout)
    if not isinstance(resp, dict):
        return None
    items = resp.get("data")
    if not isinstance(items, list) or not items:
        return None
    item = items[0]
    if not isinstance(item, dict):
        return None
    msgs = item.get("messages")
    err = item.get("error")
    if isinstance(msgs, list) and (len(msgs) > 0 or not err):
        return msgs
    return None


def mail_msg_sigs(mail: dict) -> list[str]:
    """Tạo các chữ ký nhận diện 1 bức thư, khớp chéo được cả giữa DongVanFB, Smail1s và TempMail.Plus."""
    if not isinstance(mail, dict):
        return []
    sigs: list[str] = []
    uid = str(mail.get("uid") or mail.get("id") or mail.get("mail_id") or "").strip()
    if uid:
        sigs.append(f"uid:{uid}")
    raw_dt = str(mail.get("date") or mail.get("time") or "").strip()
    parsed_dt = parse_mail_time(raw_dt)
    dt_norm = parsed_dt.strftime("%Y-%m-%d %H:%M") if parsed_dt else raw_dt
    subj = str(mail.get("subject") or "").strip()
    if dt_norm or subj:
        sigs.append(f"ms:{dt_norm}|{subj}")
    return sigs


def extract_mail_otp(mail: dict) -> str | None:
    """Bóc mã OTP 6 số từ 1 thư của DongVanFB, Smail1s hoặc TempMail.Plus."""
    if not isinstance(mail, dict):
        return None
    from_addr = str(mail.get("from") or mail.get("from_mail") or "").lower()
    subject = str(mail.get("subject") or "")
    # Bỏ qua thư cảnh báo bảo mật của chính Microsoft để không lấy nhầm số trong nội dung
    if "microsoft.com" in from_addr and "meta" not in subject.lower() and "muse" not in subject.lower():
        return None
    code_field = str(mail.get("code") or "").strip()
    if code_field and re.fullmatch(r"\d{6}", code_field):
        return code_field
    body = str(
        mail.get("message")
        or mail.get("bodyText")
        or mail.get("text")
        or mail.get("body")
        or mail.get("content")
        or mail.get("html")
        or ""
    )
    body = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"&nbsp;|&#160;", " ", body)
    text = re.sub(r"#[0-9a-fA-F]{3,8}\b", " ", f"{subject} {body}")
    m = re.search(
        r"(?:confirmation\s+code|use\s+code|login\s+code|security\s+code|verification\s+code|dùng\s+mã|mã\s+đăng\s+nhập|mã\s+xác\s+nhận|mã|code)\D{0,45}?(\d{6})\b",
        text,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    return m.group(1) if m else None


def _pick_new_otp(
    msgs: list[dict] | None,
    seen: set[str] | None,
    used: set[str],
    not_before: datetime | None,
) -> str | None:
    if not msgs:
        return None
    for mail in msgs:
        sigs = mail_msg_sigs(mail)
        if seen is not None and any(s in seen for s in sigs):
            continue
        date_str = str(mail.get("date") or "").strip()
        if date_str:
            if not is_recent_mail_time(date_str):
                continue
            mail_dt = parse_mail_time(date_str)
            if not_before is not None and mail_dt is not None and mail_dt < not_before:
                continue
        code = extract_mail_otp(mail)
        if code and code not in used:
            return code
    return None


def snapshot_mailbox(
    acc_line: str,
    email: str,
    remember_otp_fn: Callable[[str], None] | None = None,
) -> set[str] | None:
    """Chụp danh sách thư đang có trước khi bấm Continue để sau đó chỉ nhận thư OTP MỚI.

    Hỗ trợ TempMail.Plus (@merepost.com), DongVanFB OAuth2 và dự phòng Smail1s.
    """
    msgs: list[dict] | None = None
    if is_tempmailplus_account(acc_line, email):
        tm_creds = parse_tempmailplus_creds(acc_line, email)
        if tm_creds:
            msgs = tempmailplus_fetch_messages(tm_creds[0], tm_creds[1], timeout=12.0)
    else:
        dv_creds = parse_dongvan_oauth2(acc_line, email)
        if dv_creds:
            msgs = dongvan_api_fetch_messages(*dv_creds, timeout=10.0)
            if msgs is None:
                msgs = smail1s_fetch_messages(acc_line, mode="oauth", fallback_email=email, timeout=12.0)
            if msgs is None:
                msgs = smail1s_fetch_messages(acc_line, mode="graph", fallback_email=email, timeout=12.0)
        elif parse_roundcube_creds(acc_line, email):
            msgs = smail1s_fetch_messages(acc_line, mode="roundcube", fallback_email=email, timeout=12.0)
        elif "@" in (email or ""):
            msgs = smail1s_fetch_messages(acc_line, mode="emailfake", fallback_email=email, timeout=12.0)

    if msgs is None:
        return None

    seen: set[str] = set()
    for mail in msgs:
        for sig in mail_msg_sigs(mail):
            seen.add(sig)
        if remember_otp_fn is not None:
            old_c = extract_mail_otp(mail)
            if old_c:
                remember_otp_fn(old_c)
    return seen


def _poll_tempmailplus_once(
    email: str,
    epin: str,
    seen: set[str] | None,
    used: set[str],
    not_before: datetime | None,
) -> tuple[str | None, bool]:
    """Đọc 1 lượt trên TempMail.Plus; trả về (otp_code, api_ok)."""
    msgs = tempmailplus_fetch_messages(email, epin, timeout=12.0)
    if msgs is None:
        return None, False

    checked = 0
    for mail in msgs:
        sigs = mail_msg_sigs(mail)
        if seen is not None and any(s in seen for s in sigs):
            continue
        if checked >= 5:
            break
        checked += 1

        mid = mail.get("mail_id")
        detail = tempmailplus_fetch_detail(email, mid, epin, timeout=12.0) if mid is not None else None
        if isinstance(detail, dict):
            # detail["date"] có múi giờ RFC2822 chuẩn; ưu tiên hơn mail["time"] (giờ server TempMail.Plus không kèm timezone)
            merged = {**mail, **detail}
            code = _pick_new_otp([merged], seen, used, not_before)
            if code:
                return code, True
        elif seen is not None:
            # Nếu detail lỗi tạm thời nhưng mail_id chắc chắn là thư mới sau khi bấm Continue -> bóc từ subject
            fallback_item = dict(mail)
            fallback_item.pop("date", None)
            code = _pick_new_otp([fallback_item], seen, used, not_before)
            if code:
                return code, True
    return None, True


def _poll_smail1s_once(
    acc_line: str,
    email: str,
    has_oauth: bool,
    has_roundcube: bool,
    seen: set[str] | None,
    used: set[str],
    not_before: datetime | None,
) -> tuple[str | None, str]:
    """Thử đọc 1 lượt trên Smail1s; trả về (otp_code, mode_đã_dùng)."""
    if has_oauth:
        for mode in ("oauth", "graph"):
            msgs = smail1s_fetch_messages(acc_line, mode=mode, fallback_email=email, timeout=15.0)
            code = _pick_new_otp(msgs, seen, used, not_before)
            if code:
                return code, f"smail1s:{mode}"
    elif has_roundcube:
        msgs = smail1s_fetch_messages(acc_line, mode="roundcube", fallback_email=email, timeout=15.0)
        code = _pick_new_otp(msgs, seen, used, not_before)
        if code:
            return code, "smail1s:roundcube"
    else:
        msgs = smail1s_fetch_messages(acc_line, mode="emailfake", fallback_email=email, timeout=15.0)
        code = _pick_new_otp(msgs, seen, used, not_before)
        if code:
            return code, "smail1s:emailfake"
    return None, ""


def fetch_otp_dongvan_or_smail1s(
    acc_line: str,
    email: str,
    attempts: int = 10,
    interval_sec: float = 4.0,
    seen: set[str] | None = None,
    used_otps: set[str] | None = None,
    not_before: datetime | None = None,
    is_running_fn: Callable[[], bool] | None = None,
    on_status: Callable[[str], None] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> tuple[str | None, str]:
    """Đọc OTP từ DongVanFB API; nếu DongVanFB không lấy được thì tự chuyển qua API https://smail1s.com/.

    Trả về (otp_code, source_label) trong đó source_label là 'dongvanfb' hoặc 'smail1s:<mode>'.
    """
    used = set(used_otps or ())

    def _running() -> bool:
        return True if is_running_fn is None else bool(is_running_fn())

    # Hòm thư TempMail.Plus (@merepost.com, tempmail.plus, ...)
    if is_tempmailplus_account(acc_line, email):
        tm_creds = parse_tempmailplus_creds(acc_line, email)
        if not tm_creds:
            if on_log:
                on_log("Không tách được địa chỉ email TempMail.Plus!")
            return None, ""
        tm_email, tm_epin = tm_creds
        for attempt in range(1, attempts + 1):
            if not _running():
                return None, ""
            if on_status:
                on_status(f"Đang lấy OTP TempMail.Plus (lần {attempt}/{attempts})...")
            if on_log and attempt == 1:
                on_log(f"Đang đọc OTP qua API TempMail.Plus ({tm_email})...")
            code, api_ok = _poll_tempmailplus_once(tm_email, tm_epin, seen, used, not_before)
            if code:
                return code, "tempmailplus"
            if attempt < attempts and _running():
                if on_log:
                    msg = (
                        f"Lỗi kết nối/epin TempMail.Plus (lần {attempt}/{attempts}), chờ {int(interval_sec)}s..."
                        if not api_ok
                        else f"Chưa thấy thư OTP mới trên TempMail.Plus (lần {attempt}/{attempts}), chờ {int(interval_sec)}s..."
                    )
                    on_log(msg)
                time.sleep(interval_sec)
        return None, ""

    dv_creds = parse_dongvan_oauth2(acc_line, email)
    has_oauth = dv_creds is not None
    has_roundcube = parse_roundcube_creds(acc_line, email) is not None

    if not has_oauth and not has_roundcube and "@" not in (email or ""):
        if on_log:
            on_log("Dòng tài khoản thiếu refresh_token/client_id hoặc mật khẩu để đọc hòm thư!")
        return None, ""

    dv_consec_errors = 0
    smail_announced = False

    for attempt in range(1, attempts + 1):
        if not _running():
            return None, ""

        # Tài khoản chỉ có user|pass (Roundcube) hoặc email đơn -> dùng thẳng Smail1s
        if not has_oauth:
            if on_status:
                on_status(f"Đang lấy OTP qua Smail1s API (lần {attempt}/{attempts})...")
            if on_log and attempt == 1:
                on_log("Tài khoản không có OAuth2 -> đọc OTP qua API Smail1s.com...")
            code, src = _poll_smail1s_once(acc_line, email, False, has_roundcube, seen, used, not_before)
            if code:
                return code, src
            if attempt < attempts and _running():
                time.sleep(interval_sec)
            continue

        # Ưu tiên 1: DongVanFB OAuth2 API (bỏ qua nếu DongVanFB đã lỗi liên tiếp >= 2 lần trong phiên này)
        dv_msgs: list[dict] | None = None
        if dv_consec_errors < 2:
            if on_status:
                on_status(f"Đang lấy OTP DongVanFB API (lần {attempt}/{attempts})...")
            dv_msgs = dongvan_api_fetch_messages(*dv_creds, timeout=10.0)
            if dv_msgs is None:
                dv_consec_errors += 1
            else:
                dv_consec_errors = 0
                code = _pick_new_otp(dv_msgs, seen, used, not_before)
                if code:
                    return code, "dongvanfb"
                if seen is not None:
                    code_resp = dongvan_api_fetch_code(*dv_creds, timeout=8.0)
                    if isinstance(code_resp, dict):
                        direct_code = str(code_resp.get("code") or "").strip()
                        if direct_code and re.fullmatch(r"\d{6}", direct_code) and direct_code not in used:
                            return direct_code, "dongvanfb"

        # Ưu tiên 2 (dự phòng): chuyển sang Smail1s khi DongVanFB lỗi HOẶC từ lần thử thứ 3 trở đi vẫn chưa có mã
        if dv_msgs is None or dv_consec_errors >= 2 or attempt >= 3:
            if not smail_announced:
                smail_announced = True
                if on_log:
                    why = "DongVanFB không phản hồi/lỗi" if dv_msgs is None or dv_consec_errors >= 2 else "DongVanFB chưa thấy thư OTP"
                    on_log(f"{why} -> chuyển sang lấy OTP qua API Smail1s.com...")
            if on_status:
                on_status(f"Đang lấy OTP Smail1s.com (lần {attempt}/{attempts})...")
            code, src = _poll_smail1s_once(acc_line, email, True, has_roundcube, seen, used, not_before)
            if code:
                return code, src

        if attempt < attempts and _running():
            if on_log and attempt < attempts:
                src_note = "Smail1s" if smail_announced else "DongVanFB"
                on_log(f"Chưa thấy thư OTP mới trên {src_note} (lần {attempt}/{attempts}), chờ {int(interval_sec)}s...")
            time.sleep(interval_sec)

    return None, ""
