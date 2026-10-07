#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MuseAI - Công cụ Quản lý Phiên bản & Đẩy Code lên GitHub
Kho đích: https://github.com/thincole/MuseAI
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

# Đảm bảo console UTF-8 trên Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(BASE_DIR, "version.txt")
TARGET_REPO = "https://github.com/thincole/MuseAI.git"


def run_cmd(cmd: list[str], check: bool = False, capture: bool = False) -> subprocess.CompletedProcess:
    """Chạy lệnh hệ thống an toàn trong thư mục BASE_DIR."""
    kwargs = {
        "cwd": BASE_DIR,
        "text": True,
    }
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    return subprocess.run(cmd, check=check, **kwargs)


def get_current_version() -> str:
    """Đọc phiên bản hiện tại từ version.txt."""
    if os.path.isfile(VERSION_FILE):
        try:
            with open(VERSION_FILE, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except Exception:
            pass
    return "1.2.1"


def get_suggested_version(current: str) -> str:
    """Tính toán phiên bản đề xuất tiếp theo (mặc định 1.2.1 hoặc tự tăng patch)."""
    # Lấy danh sách tag trên Git
    tags: list[str] = []
    try:
        res = run_cmd(["git", "-c", "safe.directory=*", "tag", "-l"], capture=True)
        if res.returncode == 0 and res.stdout:
            tags = [t.strip() for t in res.stdout.split()]
    except Exception:
        pass

    # Nếu tag v{current} đã tồn tại trên git -> tăng số patch lên 1 (ví dụ: 1.2.1 -> 1.2.2)
    if f"v{current}" in tags:
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", current)
        if m:
            return f"{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}"
    return current or "1.2.1"


def main():
    print("=" * 68)
    print("        MuseAI Video Studio Pro - Quản Lý Phiên Bản & Đẩy GitHub")
    print(f"        Kho lưu trữ: https://github.com/thincole/MuseAI")
    print("=" * 68)
    print()

    # 1. Kiểm tra Git
    try:
        git_ver = run_cmd(["git", "--version"], capture=True)
        if git_ver.returncode != 0:
            print("[LỖI] Không tìm thấy Git trên máy tính!")
            print("Vui lòng tải và cài đặt Git: https://git-scm.com/")
            return 1
        print(f"[*] {git_ver.stdout.strip()}")
    except Exception:
        print("[LỖI] Không tìm thấy lệnh 'git' trong PATH!")
        print("Vui lòng tải và cài đặt Git: https://git-scm.com/")
        return 1

    # 2. Cấu hình safe.directory
    run_cmd(["git", "config", "--global", "--add", "safe.directory", BASE_DIR], capture=True)

    # 3. Kiểm tra và trỏ remote origin về TARGET_REPO
    remotes = run_cmd(["git", "remote"], capture=True).stdout or ""
    if "origin" in remotes.split():
        run_cmd(["git", "remote", "set-url", "origin", TARGET_REPO])
    else:
        run_cmd(["git", "remote", "add", "origin", TARGET_REPO])
    print(f"[*] Đã cấu hình remote origin -> {TARGET_REPO}")

    # 4. Xác định phiên bản
    curr_ver = get_current_version()
    suggested_ver = get_suggested_version(curr_ver)

    print()
    print(f"[*] Phiên bản hiện tại : {curr_ver}")
    print(f"[*] Phiên bản đề xuất  : {suggested_ver}")
    print()

    try:
        input_ver = input(f"Nhập phiên bản mới [Nhấn Enter để chọn '{suggested_ver}']: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n[!] Đã hủy thao tác.")
        return 0

    new_ver = input_ver if input_ver else suggested_ver

    # Lưu phiên bản vào version.txt
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write(new_ver + "\n")
    print(f"[✓] Đã lưu phiên bản {new_ver} vào version.txt")

    # 5. Nhập mô tả commit
    default_msg = f"Release v{new_ver}"
    try:
        input_msg = input(f"Nhập mô tả cập nhật [Nhấn Enter để dùng '{default_msg}']: ").strip()
    except (EOFError, KeyboardInterrupt):
        input_msg = ""
    commit_msg = input_msg if input_msg else default_msg

    # 6. Chuẩn bị tập tin & Commit
    print()
    print("[*] Đang chuẩn bị tập tin (git add .)...")
    run_cmd(["git", "add", "."])

    # Kiểm tra xem có thay đổi để commit không
    status_res = run_cmd(["git", "status", "--porcelain"], capture=True)
    if status_res.stdout.strip():
        print(f"[*] Đang tạo commit: \"{commit_msg}\"...")
        run_cmd(["git", "commit", "-m", commit_msg])
    else:
        print("[*] Không có thay đổi mới trong mã nguồn, bỏ qua bước commit.")

    # 7. Gắn thẻ phiên bản (tag)
    tag_name = f"v{new_ver}"
    print(f"[*] Đang gắn thẻ phiên bản: {tag_name}...")
    run_cmd(["git", "tag", "-a", tag_name, "-m", commit_msg, "-f"])

    # 8. Đẩy code lên GitHub
    print(f"[*] Đang đẩy mã nguồn lên nhánh 'main'...")
    run_cmd(["git", "branch", "-M", "main"])
    push_res = run_cmd(["git", "push", "-u", "origin", "main"])
    if push_res.returncode != 0:
        print()
        print("[!] Đẩy nhánh 'main' chưa thành công, đang thử đồng bộ (git pull --rebase)...")
        run_cmd(["git", "pull", "--rebase", "origin", "main"])
        print("[*] Đang thử đẩy lại lên 'main'...")
        push_retry = run_cmd(["git", "push", "-u", "origin", "main"])
        if push_retry.returncode != 0:
            print("[X] Lỗi khi đẩy lên GitHub. Vui lòng kiểm tra quyền đăng nhập GitHub trên máy tính.")
            return push_retry.returncode

    # 9. Đẩy thẻ Tag
    print(f"[*] Đang đẩy thẻ phiên bản {tag_name} lên GitHub...")
    run_cmd(["git", "push", "origin", tag_name, "-f"])

    print()
    print("=" * 68)
    print(f" [✓] ĐÃ ĐẨY PHIÊN BẢN {tag_name} LÊN GITHUB THÀNH CÔNG!")
    print()
    print(f" Kho lưu trữ: https://github.com/thincole/MuseAI")
    print(f" Thẻ Tag    : {tag_name}")
    print()
    print(" Các máy khác có thể chạy 'update.bat' để đồng bộ phiên bản này.")
    print("=" * 68)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
