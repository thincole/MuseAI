"""
hub_manager.py - Central Account Hub & Distributed 2-Slot Lease Manager
Quản lý tập trung tài khoản Muse.ai cho hệ thống máy chủ và các máy trạm qua mạng Tailscale.

Các tính năng cốt lõi:
1. Cơ chế Dynamic 2-Slot Concurrency: Mỗi tài khoản Muse chạy tối đa 2 luồng đồng thời (Slot 1 & Slot 2)
   trên toàn hệ thống (tính gộp cả các luồng trên Máy Chủ và mọi Máy Trạm Tailscale).
2. Phân phối công bằng (Fair Balance): Ưu tiên cấp tài khoản có 0 slot đang chạy trước khi cấp slot thứ 2,
   kết hợp độ trễ lệch pha (Stagger 15s) để 2 luồng không đập vào cùng 1 máy ảo Muse cùng lúc.
3. Tự động nhận diện vai trò Máy Chủ (Server) hay Máy Trạm (Client/Worker) theo địa chỉ IP Tailscale
   của máy (`100.79.170.67` / `XEON-CT2A`), hoặc cấu hình trực tiếp trên giao diện Studio.
4. Bảo mật danh sách tài khoản: Máy trạm chỉ mượn/trả tài khoản ngầm khi tạo video, hoàn toàn không hiển thị
   danh sách email/cookie trên giao diện của máy trạm.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from config import CFG
import store as store_module
from store import ESSENTIAL_COOKIES, account_exhausted, account_sort_key

log = logging.getLogger("hub_manager")

hub_router = APIRouter()

# Mỗi tài khoản Muse.ai cho phép chạy tối đa 2 luồng đồng thời
MAX_CONCURRENT_PER_ACCOUNT = 1
DEFAULT_HUB_SERVER = "http://100.79.170.67:18610"
HUB_CONFIG_FILE = os.path.join(CFG.data_dir, "hub_config.json")

_ROLE_CACHE_LOCK = threading.Lock()
_ROLE_CACHE: dict = {"ts": 0.0, "cfg": None}


def _extract_host(url_or_host: str) -> str:
    s = (url_or_host or "").strip()
    if not s:
        return "100.79.170.67"
    if "://" not in s:
        s = "http://" + s
    try:
        p = urllib.parse.urlsplit(s)
        return (p.hostname or "100.79.170.67").strip().lower()
    except Exception:
        return "100.79.170.67"


def is_local_ip_owned(url_or_host: str) -> bool:
    """Kiểm tra nhanh (<0.1ms) xem địa chỉ IP của Hub Server có thuộc về chính máy tính này không."""
    host = _extract_host(url_or_host)
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return True
    comp_name = (socket.gethostname() or os.environ.get("COMPUTERNAME") or "").strip().upper()
    if comp_name == "XEON-CT2A" and host == "100.79.170.67":
        return True
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((host, 0))
            return True
        finally:
            s.close()
    except OSError:
        return False
    except Exception:
        return False


def get_hub_node_config(force_refresh: bool = False) -> dict:
    """Đọc cấu hình vai trò Hub (Máy Chủ / Máy Trạm) của máy hiện tại."""
    now = time.time()
    with _ROLE_CACHE_LOCK:
        if not force_refresh and _ROLE_CACHE["cfg"] is not None and (now - _ROLE_CACHE["ts"]) < 2.0:
            return dict(_ROLE_CACHE["cfg"])

        saved = {}
        if os.path.isfile(HUB_CONFIG_FILE):
            try:
                with open(HUB_CONFIG_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    if isinstance(d, dict):
                        saved = d
            except Exception:
                pass

        hub_server = (
            saved.get("hub_server")
            or os.environ.get("MUSE2API_HUB_SERVER", "").strip()
            or os.environ.get("MUSE2API_PUBLIC_BASE", "").strip()
            or DEFAULT_HUB_SERVER
        ).rstrip("/")
        if not hub_server.startswith(("http://", "https://")):
            hub_server = "http://" + hub_server

        mode = (
            saved.get("mode")
            or os.environ.get("MUSE2API_HUB_MODE", "").strip()
            or "auto"
        ).lower()
        if mode not in ("auto", "server", "client"):
            mode = "auto"

        comp_name = (socket.gethostname() or os.environ.get("COMPUTERNAME") or "WORKER").strip()
        client_id = (
            saved.get("client_id")
            or os.environ.get("MUSE2API_CLIENT_ID", "").strip()
            or os.environ.get("SHOPEE_CLIENT_ID", "").strip()
            or comp_name
        )

        owns_hub_ip = is_local_ip_owned(hub_server)
        if mode == "server":
            effective_role = "server"
        elif mode == "client":
            effective_role = "client"
        else:
            effective_role = "server" if owns_hub_ip else "client"

        cfg = {
            "mode": mode,
            "effective_role": effective_role,
            "is_client": effective_role == "client",
            "is_server": effective_role == "server",
            "owns_hub_ip": owns_hub_ip,
            "hub_server": hub_server,
            "client_id": client_id,
            "computer_name": comp_name,
            "api_key": saved.get("api_key") or CFG.api_key,
        }
        _ROLE_CACHE["ts"] = now
        _ROLE_CACHE["cfg"] = cfg
        return dict(cfg)


def is_hub_client_mode() -> bool:
    """Trả về True nếu phần mềm trên máy này đang đóng vai trò Máy Trạm (mượn TK từ Máy Chủ)."""
    return bool(get_hub_node_config().get("is_client"))


def save_hub_node_config(mode: str | None = None, hub_server: str | None = None,
                         client_id: str | None = None, api_key: str | None = None) -> dict:
    """Lưu cấu hình Hub vào data/hub_config.json và cập nhật bộ nhớ đệm."""
    cur = get_hub_node_config(force_refresh=True)
    new_mode = (mode or cur["mode"] or "auto").strip().lower()
    if new_mode not in ("auto", "server", "client"):
        new_mode = "auto"
    new_server = (hub_server or cur["hub_server"] or DEFAULT_HUB_SERVER).strip().rstrip("/")
    if not new_server.startswith(("http://", "https://")):
        new_server = "http://" + new_server
    new_client_id = (client_id or cur["client_id"] or cur["computer_name"]).strip()
    new_key = (api_key if api_key is not None else cur.get("api_key") or CFG.api_key or "").strip()

    os.makedirs(os.path.dirname(HUB_CONFIG_FILE), exist_ok=True)
    payload = {
        "mode": new_mode,
        "hub_server": new_server,
        "client_id": new_client_id,
        "api_key": new_key,
        "updated_at": int(time.time()),
    }
    with open(HUB_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    os.environ["MUSE2API_HUB_MODE"] = new_mode
    os.environ["MUSE2API_HUB_SERVER"] = new_server
    os.environ["MUSE2API_CLIENT_ID"] = new_client_id
    return get_hub_node_config(force_refresh=True)


# ============================================================================
# CẤU TRÚC DỮ LIỆU & QUẢN LÝ CHO THUÊ (LEASE MANAGER)
# ============================================================================

@dataclass
class AccountLease:
    lease_id: str
    account_id: str
    account_label: str
    slot_number: int  # 1 hoặc 2
    client_id: str
    client_ip: str
    task_type: str
    leased_at: float
    expires_at: float
    timeout_sec: int
    last_heartbeat: float
    heartbeat_count: int = 0

    def is_expired(self, now: float | None = None) -> bool:
        t = now or time.time()
        return t > self.expires_at


@dataclass
class ClientNodeInfo:
    client_id: str
    ip: str
    first_seen: float
    last_seen: float
    total_leases: int = 0
    success_count: int = 0
    error_count: int = 0
    last_task_type: str = ""
    is_tailscale: bool = False


class AccountHubManager:
    """Bộ quản lý trung tâm điều phối tài khoản 2-Slot cho toàn bộ hệ thống máy trạm."""

    def __init__(self, store: store_module.Store | None = None):
        self._store = store
        self._lock = threading.Lock()
        # account_id -> list[AccountLease] (tối đa MAX_CONCURRENT_PER_ACCOUNT phần tử)
        self._active_leases: dict[str, list[AccountLease]] = {}
        # lease_id -> account_id
        self._lease_lookup: dict[str, str] = {}
        # client_id -> ClientNodeInfo
        self._clients: dict[str, ClientNodeInfo] = {}
        self._start_time = time.time()

    @property
    def store(self) -> store_module.Store:
        if self._store is None:
            self._store = store_module.Store(CFG)
        return self._store

    @staticmethod
    def is_tailscale_ip(ip: str) -> bool:
        """Kiểm tra địa chỉ IP có thuộc dải mạng CGNAT của Tailscale (100.64.0.0/10) hay không."""
        try:
            parts = ip.split(".")
            if len(parts) == 4 and parts[0] == "100":
                second = int(parts[1])
                return 64 <= second <= 127
        except Exception:
            pass
        return False

    def _cleanup_expired_leases_locked(self, now: float | None = None):
        """Dọn dẹp các slot thuê đã hết hạn do máy trạm bị tắt nguồn hoặc mất mạng."""
        t = now or time.time()
        for aid, leases in list(self._active_leases.items()):
            active = []
            for lease in leases:
                if lease.is_expired(t):
                    self._lease_lookup.pop(lease.lease_id, None)
                    log.info(
                        "[Hub] Slot %d hết hạn tự động giải phóng: TK %s (%s) của máy trạm %s",
                        lease.slot_number, aid, lease.account_label, lease.client_id
                    )
                else:
                    active.append(lease)
            if active:
                self._active_leases[aid] = active
            else:
                self._active_leases.pop(aid, None)

    def get_active_lease_count(self, aid: str) -> int:
        """Số slot đang được các máy trạm thuê của tài khoản `aid` (gọi từ Store để đồng bộ 2-slot với Máy Chủ)."""
        with self._lock:
            self._cleanup_expired_leases_locked()
            return len(self._active_leases.get(aid, []))

    def register_client_activity(self, client_id: str, ip: str, task_type: str = ""):
        """Ghi nhận thông tin kết nối và thời gian hoạt động của máy trạm."""
        now = time.time()
        is_ts = self.is_tailscale_ip(ip)
        with self._lock:
            if client_id not in self._clients:
                self._clients[client_id] = ClientNodeInfo(
                    client_id=client_id,
                    ip=ip,
                    first_seen=now,
                    last_seen=now,
                    is_tailscale=is_ts,
                    last_task_type=task_type,
                )
            else:
                c = self._clients[client_id]
                c.last_seen = now
                c.ip = ip
                c.is_tailscale = is_ts
                if task_type:
                    c.last_task_type = task_type

    def is_account_healthy(self, acc: dict, now: float | None = None,
                           skip_exhausted: bool | None = None) -> bool:
        """Kiểm tra toàn diện tài khoản có đủ điều kiện phục vụ máy trạm không (chuẩn theo Store.acquire_account)."""
        if not acc.get("enabled", True):
            return False
        if acc.get("muse_locked"):
            return False
        if self.store.in_cooldown(acc["id"]):
            return False

        cookies = acc.get("cookies") or {}
        if not cookies.get("hatch_vml"):
            return False

        use_skip = self.store.SKIP_EXHAUSTED if skip_exhausted is None else skip_exhausted
        if use_skip and account_exhausted(acc):
            return False

        return True

    def _total_busy_slots_for_account(self, aid: str) -> int:
        """Tổng số luồng đang chạy trên TK `aid` = luồng cục bộ trên Máy Chủ + luồng thuê từ các Máy Trạm."""
        local_busy = self.store._BUSY_COUNT.get(aid, 0)
        remote_busy = len(self._active_leases.get(aid, []))
        return local_busy + remote_busy

    def get_pool_summary(self) -> dict:
        """Trả về thống kê chi tiết kho tài khoản máy chủ theo mô hình 2-Slot."""
        now = time.time()
        self.store._sync_from_disk()
        with self._lock:
            self._cleanup_expired_leases_locked(now)
            all_accs = list(self.store.accounts)
            total = len(all_accs)
            live_vml = [a for a in all_accs if a.get("enabled", True) and (a.get("cookies") or {}).get("hatch_vml") and not a.get("muse_locked") and not self.store.in_cooldown(a["id"])]
            all_exhausted = bool(live_vml and all(account_exhausted(a) for a in live_vml))
            skip_ex = False if all_exhausted else self.store.SKIP_EXHAUSTED

            alive_accs = [a for a in all_accs if self.is_account_healthy(a, now, skip_exhausted=skip_ex)]
            alive = len(alive_accs)
            locked = sum(1 for a in all_accs if a.get("muse_locked"))
            exhausted = sum(1 for a in all_accs if account_exhausted(a))

            max_per_acc = self.store.MAX_JOBS_PER_ACCOUNT or MAX_CONCURRENT_PER_ACCOUNT
            total_slots = sum(self.store.effective_limit(a["id"], max_per_acc) for a in alive_accs)
            remote_used_slots = sum(len(ls) for ls in self._active_leases.values())
            local_used_slots = sum(self.store._BUSY_COUNT.values())
            used_slots = remote_used_slots + local_used_slots
            available_slots = max(0, total_slots - used_slots)

            leases_list = []
            for leases in self._active_leases.values():
                for lease in leases:
                    leases_list.append({
                        "lease_id": lease.lease_id,
                        "account_id": lease.account_id,
                        "account_label": lease.account_label,
                        "slot_number": lease.slot_number,
                        "slot_label": f"Slot {lease.slot_number}/{max_per_acc}",
                        "client_id": lease.client_id,
                        "client_ip": lease.client_ip,
                        "task_type": lease.task_type,
                        "leased_at": lease.leased_at,
                        "expires_at": lease.expires_at,
                        "remaining_sec": max(0, int(lease.expires_at - now)),
                        "heartbeat_count": lease.heartbeat_count,
                    })

            clients_dict = {}
            for cid, c in self._clients.items():
                clients_dict[cid] = {
                    "ip": c.ip,
                    "is_tailscale": c.is_tailscale,
                    "first_seen": c.first_seen,
                    "last_seen": c.last_seen,
                    "active_seconds_ago": int(now - c.last_seen),
                    "total_leases": c.total_leases,
                    "success_count": c.success_count,
                    "error_count": c.error_count,
                    "last_task": c.last_task_type,
                }

        node_cfg = get_hub_node_config()
        return {
            "server_time": now,
            "uptime_sec": int(now - self._start_time),
            "tailscale_ip": CFG.public_base or node_cfg.get("hub_server") or DEFAULT_HUB_SERVER,
            "hub_mode": node_cfg.get("mode"),
            "effective_role": node_cfg.get("effective_role"),
            "total_accounts": total,
            "alive_accounts": alive,
            "locked_accounts": locked,
            "exhausted_accounts": exhausted,
            "max_slots_per_account": max_per_acc,
            "account_dispatch_mode": self.store.ACCOUNT_DISPATCH_MODE,
            "total_slots": total_slots,
            "used_slots": used_slots,
            "remote_used_slots": remote_used_slots,
            "local_used_slots": local_used_slots,
            "available_slots": available_slots,
            "active_leases_count": remote_used_slots,
            "available_for_lease": available_slots,
            "active_leases": leases_list,
            "connected_clients": clients_dict,
        }

    def list_accounts_for_clients(
        self,
        alive_only: bool = True,
        include_cookies: bool = True,
        limit: int = 0,
    ) -> list[dict]:
        """Lấy danh sách tài khoản cho máy trạm (hỗ trợ đồng bộ đầy đủ cookie & số slot rảnh)."""
        now = time.time()
        self.store._sync_from_disk()
        with self._lock:
            self._cleanup_expired_leases_locked(now)
            candidates = []
            max_per_acc = self.store.MAX_JOBS_PER_ACCOUNT or MAX_CONCURRENT_PER_ACCOUNT
            for a in self.store.accounts:
                if alive_only and not self.is_account_healthy(a, now, skip_exhausted=False):
                    continue

                item = dict(a)
                if not include_cookies:
                    item.pop("cookies", None)

                leases = self._active_leases.get(a["id"], [])
                active_slots = self._total_busy_slots_for_account(a["id"])
                item["max_slots"] = max_per_acc
                item["active_slots"] = active_slots
                item["free_slots"] = max(0, max_per_acc - active_slots)
                item["is_full"] = active_slots >= max_per_acc
                item["is_leased"] = active_slots > 0
                item["leased_by"] = [l.client_id for l in leases]
                item["lease_remaining_sec"] = max((max(0, int(l.expires_at - now)) for l in leases), default=0)

                candidates.append(item)

            if limit > 0:
                candidates = candidates[:limit]

            return candidates

    def lease_account(
        self,
        client_id: str,
        client_ip: str,
        task_type: str = "video",
        timeout_sec: int = 300,
        preferred_id: str | None = None,
        preferred_label: str | None = None,
        exclude_ids: list[str] | None = None,
        skip_exhausted: bool | None = None,
    ) -> tuple[bool, str, dict | None, AccountLease | None]:
        """Thuê 1 Slot của tài khoản sẵn sàng để máy trạm thực hiện tác vụ tạo video/ảnh (tối đa 2 slot/tài khoản)."""
        now = time.time()
        timeout_sec = max(60, min(timeout_sec, 1800))

        self.register_client_activity(client_id, client_ip, task_type)
        self.store._sync_from_disk()

        with self._lock:
            self._cleanup_expired_leases_locked(now)
            max_per_acc = self.store.MAX_JOBS_PER_ACCOUNT or MAX_CONCURRENT_PER_ACCOUNT
            use_skip = self.store.SKIP_EXHAUSTED if skip_exhausted is None else bool(skip_exhausted)

            live_vml = [
                a for a in self.store.accounts
                if a.get("enabled", True) and (a.get("cookies") or {}).get("hatch_vml")
                and not a.get("muse_locked") and not self.store.in_cooldown(a["id"])
            ]
            if use_skip and live_vml and all(account_exhausted(a) for a in live_vml):
                use_skip = False

            pool: list[tuple[dict, int]] = []
            alive_count = 0
            for a in self.store.accounts:
                if not self.is_account_healthy(a, now, skip_exhausted=use_skip):
                    continue
                eff_lim = self.store.effective_limit(a["id"], max_per_acc)
                if eff_lim <= 0:
                    continue
                alive_count += 1
                current_slots = self._total_busy_slots_for_account(a["id"])
                if current_slots < eff_lim:
                    pool.append((a, current_slots))

            if exclude_ids and len(pool) > 1:
                ex_set = set(exclude_ids)
                filtered = [item for item in pool if item[0]["id"] not in ex_set]
                if filtered:
                    pool = filtered

            if not pool:
                all_active_leases = [l for ls in self._active_leases.values() for l in ls]
                next_free = min(
                    (l.expires_at - now for l in all_active_leases),
                    default=15
                )
                wait_sec = max(2, int(next_free))
                total_capacity = alive_count * max_per_acc
                return False, f"Toàn bộ {total_capacity} Slots của máy chủ đang bận hết. Vui lòng đợi {wait_sec}s.", None, None

            # Áp dụng độ trễ lệch pha 15s (SAME_ACCOUNT_STAGGER_SECONDS) khi gán luồng thứ 2 vào cùng 1 tài khoản
            stagger = getattr(self.store, "SAME_ACCOUNT_STAGGER_SECONDS", 15.0)
            ready_pool = [
                (a, s) for (a, s) in pool
                if s == 0 or (now - self.store._LAST_ACQUIRE_TS.get(a["id"], 0.0)) >= stagger
            ]
            if not ready_pool:
                soonest = min(
                    max(1.0, stagger - (now - self.store._LAST_ACQUIRE_TS.get(a["id"], 0.0)))
                    for (a, _s) in pool
                )
                return False, f"Đang chờ độ trễ lệch pha máy ảo Muse ({int(soonest)}s)...", None, None

            warm_ids = self.store._get_warm_ids()
            ready_pool.sort(key=lambda item: account_sort_key(item[0], item[1], now_ts=now, warm_ids=warm_ids))

            target_acc, current_slots = ready_pool[0]

            if preferred_id:
                for a, s in ready_pool:
                    if a["id"] == preferred_id and a.get("ok") is not False:
                        target_acc, current_slots = a, s
                        break
            elif preferred_label:
                for a, s in ready_pool:
                    if a.get("label", "").lower() == preferred_label.lower():
                        target_acc, current_slots = a, s
                        break

            aid = target_acc["id"]
            label = target_acc.get("label", "")
            lease_id = uuid4().hex
            slot_number = current_slots + 1

            lease = AccountLease(
                lease_id=lease_id,
                account_id=aid,
                account_label=label,
                slot_number=slot_number,
                client_id=client_id,
                client_ip=client_ip,
                task_type=task_type,
                leased_at=now,
                expires_at=now + timeout_sec,
                timeout_sec=timeout_sec,
                last_heartbeat=now,
            )

            if aid not in self._active_leases:
                self._active_leases[aid] = []
            self._active_leases[aid].append(lease)
            self._lease_lookup[lease_id] = aid

            if client_id in self._clients:
                self._clients[client_id].total_leases += 1

            self.store._LAST_ACQUIRE_TS[aid] = now
            target_acc["last_used"] = now
            target_acc["use_count"] = target_acc.get("use_count", 0) + 1
            try:
                self.store._save_accounts(force=False)
            except Exception:
                pass

            log.info(
                "[Hub] Cấp Slot %d/%d của TK %s (%s) cho máy trạm '%s' (%s) trong %ds (lease_id=%s)",
                slot_number, max_per_acc, aid, label, client_id, client_ip, timeout_sec, lease_id[:8]
            )

            return True, "Thành công", dict(target_acc), lease

    def heartbeat(self, lease_id: str, extend_sec: int = 180) -> tuple[bool, str, int]:
        """Gia hạn thời gian thuê slot khi tác vụ tạo video mất nhiều thời gian."""
        now = time.time()
        extend_sec = max(30, min(extend_sec, 600))
        with self._lock:
            aid = self._lease_lookup.get(lease_id)
            if not aid or aid not in self._active_leases:
                return False, "Phiên thuê không tồn tại hoặc đã hết hạn", 0

            leases = self._active_leases[aid]
            lease = next((l for l in leases if l.lease_id == lease_id), None)
            if not lease:
                return False, "Phiên thuê không tồn tại hoặc đã hết hạn", 0

            max_expiry = lease.leased_at + 1800
            new_exp = min(max_expiry, max(lease.expires_at, now) + extend_sec)

            lease.expires_at = new_exp
            lease.last_heartbeat = now
            lease.heartbeat_count += 1
            remaining = max(0, int(new_exp - now))
            return True, "Gia hạn thành công", remaining

    def apply_account_feedback(self, aid: str, client_id: str = "", feedback: dict | None = None):
        """Áp dụng báo cáo trạng thái tài khoản từ Máy Trạm vào Store của Máy Chủ."""
        if not aid or not feedback:
            return
        acc = self.store.get_account(aid)
        if not acc:
            return

        prefix = f"[{client_id}] " if client_id else ""
        status = str(feedback.get("status") or "").strip().lower()
        err_msg = str(feedback.get("error_message") or feedback.get("note") or "").strip()

        if feedback.get("quota") and isinstance(feedback["quota"], dict):
            self.store.update_account(aid, quota=feedback["quota"])
        elif "hết sạch 100% Token" in err_msg or "0 tokens left" in err_msg:
            from datetime import date, timedelta
            from store import parse_token_count
            q_cur = dict(acc.get("quota") or {})
            if (parse_token_count(q_cur.get("extra_left")) or 0) <= 0:
                q_cur.update({
                    "plan": q_cur.get("plan") or "Free plan",
                    "weekly_used_pct": 100,
                    "extra_used_pct": 100,
                    "extra_left": "0 tokens left",
                    "extra_expires": q_cur.get("extra_expires") or "never",
                    "weekly_reset": (date.today() + timedelta(days=7)).strftime("%b %d"),
                    "found": True,
                    "checked_at": int(time.time()),
                })
                self.store.update_account(aid, quota=q_cur)

        if feedback.get("proxy"):
            self.store.update_account(
                aid,
                proxy=feedback.get("proxy"),
                proxy_ip=feedback.get("proxy_ip"),
                proxy_status=feedback.get("proxy_status")
            )

        if feedback.get("vm_ok"):
            self.store.touch_keepalive(aid, True, "Phiên hợp lệ · Sẵn sàng")
            self.store.record_vm_ok(aid)
        elif "outcome_ok" in feedback and feedback["outcome_ok"] is not None:
            self.store.record_outcome(aid, bool(feedback["outcome_ok"]))

        if feedback.get("vm_stuck"):
            self.store.record_vm_stuck(aid)
        if feedback.get("attach_fail"):
            self.store.record_attach_fail(aid)
        if feedback.get("timeout_fail"):
            self.store.record_timeout(aid)

        if feedback.get("cooldown_seconds"):
            try:
                cd_sec = float(feedback["cooldown_seconds"])
                if cd_sec > 0:
                    self.store.block_until(aid, cd_sec)
            except (TypeError, ValueError):
                pass

        if feedback.get("muse_locked") is not None:
            self.store.set_locked(aid, bool(feedback["muse_locked"]), f"{prefix}{err_msg}" if err_msg else "")

        if "ok" in feedback and feedback["ok"] is not None:
            self.store.mark(aid, bool(feedback["ok"]), f"{prefix}{err_msg}" if err_msg else "")
        elif status == "quota_exhausted":
            self.store.block_until(aid, 3600)
            self.store.mark(aid, True, f"{prefix}Hết hạn ngạch: {err_msg[:120]}")
        elif status == "cookie_expired":
            self.store.mark(aid, False, f"{prefix}Hết hạn cookie: {err_msg[:120]}")
            self.store.set_locked(aid, True, f"{prefix}Hết hạn cookie: {err_msg[:120]}")
        elif status == "error" and err_msg:
            self.store.mark(aid, True, f"{prefix}{err_msg[:150]}")

        if feedback.get("video_success"):
            self.store.record_success(aid)

    def release_account(
        self,
        lease_id: str,
        account_id: str | None = None,
        client_id: str = "",
        status: str = "success",
        error_message: str = "",
        duration: float | None = None,
        extra_feedback: dict | None = None,
    ) -> tuple[bool, str]:
        """Giải phóng slot tài khoản sau khi máy trạm hoàn thành hoặc gặp lỗi."""
        matching_lease = None
        slot_num = 1
        cid = client_id
        with self._lock:
            aid = account_id or self._lease_lookup.get(lease_id)
            if aid and aid in self._active_leases:
                leases = self._active_leases[aid]
                if lease_id:
                    matching_lease = next((l for l in leases if l.lease_id == lease_id), None)
                elif leases:
                    matching_lease = leases[0]
                if matching_lease:
                    slot_num = matching_lease.slot_number
                    cid = cid or matching_lease.client_id
                    leases.remove(matching_lease)
                    if not leases:
                        self._active_leases.pop(aid, None)
            if lease_id:
                self._lease_lookup.pop(lease_id, None)

            if cid and cid in self._clients:
                if status == "success":
                    self._clients[cid].success_count += 1
                else:
                    self._clients[cid].error_count += 1

        if not aid:
            return False, "Không tìm thấy thông tin phiên thuê"

        fb = dict(extra_feedback or {})
        fb.setdefault("status", status)
        if error_message:
            fb.setdefault("error_message", error_message)
        if status == "success" and "vm_ok" not in fb and "ok" not in fb:
            fb["vm_ok"] = True
            # Nếu gọi từ SDK độc lập (muse_hub_client), cộng luôn thành công
            if fb.get("from_sdk", True) and "video_success" not in fb:
                fb["video_success"] = True

        self.apply_account_feedback(aid, client_id=cid, feedback=fb)
        # Đánh thức các luồng đang chờ trên Máy Chủ nếu có
        try:
            with self.store._COND:
                self.store._COND.notify_all()
        except Exception:
            pass
        return True, f"Đã giải phóng Slot {slot_num} của tài khoản về nhóm dùng chung"


# Khởi tạo singleton Manager
HUB = AccountHubManager()


# ============================================================================
# XÁC THỰC BẢO MẬT HUB API
# ============================================================================

def hub_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    x_hub_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
):
    """Xác thực API Key hoặc IP nội bộ Tailscale dành cho Máy Trạm."""
    client_ip = request.client.host if request.client else ""
    if client_ip in ("127.0.0.1", "::1", "localhost") or AccountHubManager.is_tailscale_ip(client_ip):
        return True

    server_key = (CFG.api_key or "").strip()
    admin_key = (getattr(CFG, "admin_key", "") or "").strip()

    if not server_key and not admin_key:
        return True

    candidate = ""
    if x_hub_key:
        candidate = x_hub_key.strip()
    elif key:
        candidate = key.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        parts = authorization.split(None, 1)
        if len(parts) > 1:
            candidate = parts[1].strip()

    def _matches(a: str, b: str) -> bool:
        return bool(a and b) and secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

    if _matches(candidate, server_key) or _matches(candidate, admin_key):
        return True

    raise HTTPException(
        status_code=401,
        detail="API Key không hợp lệ. Vui lòng cung cấp key trong Header 'Authorization: Bearer <key>' hoặc kết nối qua mạng Tailscale."
    )


# ============================================================================
# CÁC ENDPOINT API DÀNH CHO MÁY TRẠM & CẤU HÌNH HUB
# ============================================================================

@hub_router.get("/config")
def api_get_hub_config():
    """Trả về cấu hình vai trò (Máy Chủ / Máy Trạm) của máy hiện tại."""
    cfg = get_hub_node_config(force_refresh=True)
    return {"success": True, **cfg}


@hub_router.post("/config")
def api_save_hub_config(req: dict):
    """Lưu cấu hình vai trò Hub (auto / server / client) và địa chỉ Máy Chủ."""
    cfg = save_hub_node_config(
        mode=req.get("mode"),
        hub_server=req.get("hub_server"),
        client_id=req.get("client_id"),
        api_key=req.get("api_key"),
    )
    return {
        "success": True,
        "message": f"Đã lưu cấu hình Hub (Vai trò hiện tại: {'Máy Trạm' if cfg['is_client'] else 'Máy Chủ'})",
        **cfg,
    }


@hub_router.get("/status")
def get_hub_status(request: Request, _=Depends(hub_auth)):
    """Trả về trạng thái tổng quan kho tài khoản và các máy trạm đang kết nối (kèm thông số 2-Slot)."""
    client_ip = request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")
    client_id = request.headers.get("x-client-id") or ""
    if client_id and client_ip:
        HUB.register_client_activity(client_id, client_ip, "status")
    return HUB.get_pool_summary()


@hub_router.get("/accounts")
def get_accounts(
    alive_only: bool = Query(default=True, description="Chỉ lấy các tài khoản còn sống và hợp lệ"),
    include_cookies: bool = Query(default=True, description="Kèm theo đầy đủ cookie để sử dụng"),
    limit: int = Query(default=0, description="Số lượng tối đa cần lấy (0 = tất cả)"),
    _=Depends(hub_auth),
):
    """Lấy danh sách tài khoản từ máy chủ cho tiến trình đồng bộ (bao gồm thông tin slot rảnh)."""
    accs = HUB.list_accounts_for_clients(
        alive_only=alive_only,
        include_cookies=include_cookies,
        limit=limit,
    )
    max_per_acc = HUB.store.MAX_JOBS_PER_ACCOUNT or MAX_CONCURRENT_PER_ACCOUNT
    return {
        "server_time": time.time(),
        "total": len(accs),
        "max_slots_per_account": max_per_acc,
        "account_dispatch_mode": HUB.store.ACCOUNT_DISPATCH_MODE,
        "accounts": accs,
    }


@hub_router.get("/accounts/{aid}")
def get_single_account(aid: str, _=Depends(hub_auth)):
    """Lấy chi tiết 1 tài khoản cụ thể (bao gồm cookies)."""
    acc = HUB.store.get_account(aid)
    if not acc:
        raise HTTPException(404, f"Không tìm thấy tài khoản ID: {aid}")
    return acc


@hub_router.post("/lease")
def lease_account(
    req: dict,
    request: Request,
    _=Depends(hub_auth),
):
    """Máy trạm thuê một Slot tài khoản để tạo video/ảnh (tối đa 2 luồng/tài khoản)."""
    client_id = str(req.get("client_id") or "unknown_client").strip()
    task_type = str(req.get("task_type") or "video").strip()
    timeout_sec = int(req.get("timeout_sec") or 300)
    preferred_id = req.get("preferred_id")
    preferred_label = req.get("preferred_label")
    exclude_ids = req.get("exclude_ids")
    if isinstance(exclude_ids, str):
        exclude_ids = [exclude_ids]
    skip_exhausted = req.get("skip_exhausted")

    client_ip = request.headers.get("x-forwarded-for") or (request.client.host if request.client else "unknown")

    ok, msg, acc, lease = HUB.lease_account(
        client_id=client_id,
        client_ip=client_ip,
        task_type=task_type,
        timeout_sec=timeout_sec,
        preferred_id=preferred_id,
        preferred_label=preferred_label,
        exclude_ids=exclude_ids,
        skip_exhausted=skip_exhausted,
    )

    if not ok:
        raise HTTPException(503, msg)

    max_per_acc = HUB.store.MAX_JOBS_PER_ACCOUNT or MAX_CONCURRENT_PER_ACCOUNT
    return {
        "success": True,
        "message": msg,
        "lease_id": lease.lease_id,
        "slot_number": lease.slot_number,
        "slot_label": f"Slot {lease.slot_number}/{max_per_acc}",
        "expires_at": lease.expires_at,
        "timeout_sec": lease.timeout_sec,
        "account": acc,
    }


@hub_router.post("/heartbeat")
def heartbeat_lease(req: dict, _=Depends(hub_auth)):
    """Máy trạm gửi nhịp tim (heartbeat) để gia hạn thời gian thuê slot."""
    lease_id = str(req.get("lease_id") or "").strip()
    extend_sec = int(req.get("extend_sec") or 180)
    if not lease_id:
        raise HTTPException(400, "Thiếu tham số 'lease_id'")

    ok, msg, remaining = HUB.heartbeat(lease_id, extend_sec=extend_sec)
    if not ok:
        raise HTTPException(404, msg)

    return {
        "success": True,
        "message": msg,
        "remaining_sec": remaining,
    }


@hub_router.post("/release")
def release_account(req: dict, request: Request, _=Depends(hub_auth)):
    """Máy trạm giải phóng slot tài khoản sau khi dùng xong hoặc báo cáo lỗi."""
    lease_id = str(req.get("lease_id") or "").strip()
    account_id = req.get("account_id")
    client_id = str(req.get("client_id") or "").strip()
    status = str(req.get("status") or "success").strip().lower()
    error_msg = str(req.get("error_message") or req.get("note") or "").strip()
    duration = req.get("duration")

    ok, msg = HUB.release_account(
        lease_id=lease_id,
        account_id=account_id,
        client_id=client_id,
        status=status,
        error_message=error_msg,
        duration=duration,
        extra_feedback=req,
    )
    return {"success": ok, "message": msg}


@hub_router.post("/report")
def report_account_state(req: dict, request: Request, _=Depends(hub_auth)):
    """Máy trạm cập nhật trạng thái tài khoản (ví dụ: cộng Video Thành Công sau khi FFmpeg ghép xong, hoặc cập nhật quota)."""
    aid = str(req.get("account_id") or "").strip()
    client_id = str(req.get("client_id") or "").strip()
    client_ip = request.headers.get("x-forwarded-for") or (request.client.host if request.client else "unknown")
    if client_id:
        HUB.register_client_activity(client_id, client_ip, "report")
    if not aid:
        raise HTTPException(400, "Thiếu account_id")
    HUB.apply_account_feedback(aid, client_id=client_id, feedback=req)
    return {"success": True}


@hub_router.get("/sync")
def sync_accounts_for_node(
    request: Request,
    since: float = Query(default=0.0, description="Dấu thời gian cập nhật gần nhất"),
    _=Depends(hub_auth),
):
    """Endpoint để đồng bộ kho tài khoản khi cần."""
    client_ip = request.headers.get("x-forwarded-for") or (request.client.host if request.client else "unknown")
    client_id = request.headers.get("x-client-id") or f"client_{client_ip}"
    HUB.register_client_activity(client_id, client_ip, "sync")

    accs = HUB.list_accounts_for_clients(alive_only=True, include_cookies=True)
    return {
        "sync_time": time.time(),
        "total": len(accs),
        "accounts": accs,
    }


@hub_router.post("/import")
def import_accounts_from_node(req: dict, request: Request, _=Depends(hub_auth)):
    """Cho phép công cụ thu hoạch cookie đẩy tài khoản mới về Máy Chủ."""
    batch_text = str(req.get("batch") or "").strip()
    if not batch_text:
        raise HTTPException(400, "Thiếu trường 'batch' chứa danh sách tài khoản")

    from app import parse_batch

    added = []
    for label, cookies in parse_batch(batch_text):
        acc = HUB.store.add_account(cookies, label)
        added.append({"id": acc["id"], "label": acc["label"]})

    return {
        "success": True,
        "added_count": len(added),
        "accounts": added,
    }


@hub_router.get("/export")
def export_accounts(
    format: str = Query(default="txt", pattern="^(txt|json)$"),
    alive_only: bool = Query(default=True),
    _=Depends(hub_auth),
):
    """Xuất danh sách tài khoản dạng TXT (email|cookie_header) hoặc JSON."""
    accs = HUB.list_accounts_for_clients(alive_only=alive_only, include_cookies=True)
    if format == "json":
        return JSONResponse(
            content=accs,
            headers={"Content-Disposition": "attachment; filename=muse_accounts_hub.json"}
        )

    lines = []
    for a in accs:
        cookies = a.get("cookies", {})
        header_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        label = a.get("label", a["id"])
        lines.append(f"{label}|{header_str}")

    return PlainTextResponse(
        "\n".join(lines) + "\n",
        headers={"Content-Disposition": "attachment; filename=muse_accounts_hub.txt"}
    )
