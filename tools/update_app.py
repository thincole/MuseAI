#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MuseAI - Công cụ Cập nhật Phiên bản Mới nhất từ GitHub
Kho lưu trữ: https://github.com/thincole/MuseAI
"""
from __future__ import annotations

import os
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
REQUIREMENTS_FILE = os.path.join(BASE_DIR, "requirements.txt")
TARGET_REPO = "https://github.com/thincole/MuseAI.git"


def run_cmd(cmd: list[str], check: bool = False, capture: bool = False) -> subprocess.CompletedProcess:
    kwargs = {
        "cwd": BASE_DIR,
        "text": True,
    }
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    return subprocess.run(cmd, check=check, **kwargs)


def get_version() -> str:
    if os.path.isfile(VERSION_FILE):
        try:
            with open(VERSION_FILE, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except Exception:
            pass
    return "Chưa xác định"


def main():
    print("=" * 68)
    print("        MuseAI Video Studio Pro - Cập Nhật Phiên Bản Mới Nhất")
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
    except Exception:
        print("[LỖI] Không tìm thấy lệnh 'git' trong PATH!")
        print("Vui lòng tải và cài đặt Git: https://git-scm.com/")
        return 1

    # 2. Cấu hình safe.directory
    run_cmd(["git", "config", "--global", "--add", "safe.directory", BASE_DIR], capture=True)

    # 3. Đảm bảo trỏ đúng kho GitHub TARGET_REPO
    remotes = run_cmd(["git", "remote"], capture=True).stdout or ""
    if "origin" in remotes.split():
        run_cmd(["git", "remote", "set-url", "origin", TARGET_REPO])
    else:
        run_cmd(["git", "remote", "add", "origin", TARGET_REPO])

    old_ver = get_version()
    print(f"[*] Phiên bản hiện tại trên máy: {old_ver}")
    print("[*] Đang kết nối GitHub và kiểm tra cập nhật mới nhất...")
    print()

    # 4. Kéo cập nhật từ GitHub
    run_cmd(["git", "fetch", "origin", "main", "--tags"], capture=True)
    pull_res = run_cmd(["git", "pull", "origin", "main"])

    if pull_res.returncode != 0:
        print()
        print("[!] Phát hiện có sự thay đổi trong các file cục bộ.")
        print("[*] Đang tự động lưu trữ tạm (git stash) và cập nhật bản mới nhất...")
        run_cmd(["git", "stash"])
        retry_pull = run_cmd(["git", "pull", "origin", "main"])
        run_cmd(["git", "stash", "pop"], capture=True)
        if retry_pull.returncode != 0:
            print("[X] Không thể tự động gộp cập nhật. Vui lòng kiểm tra lại trạng thái tệp.")
            return retry_pull.returncode

    new_ver = get_version()

    # 5. Cập nhật thư viện requirements.txt nếu có
    if os.path.isfile(REQUIREMENTS_FILE):
        print()
        print("[*] Đang kiểm tra các thư viện phụ thuộc (pip install -r requirements.txt)...")
        run_cmd([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"])

    print()
    print("=" * 68)
    print(" [✓] TIẾN TRÌNH CẬP NHẬT HOÀN TẤT THÀNH CÔNG!")
    print()
    print(f" Phiên bản trước    : {old_ver}")
    print(f" Phiên bản hiện tại : {new_ver}")
    print()
    print(" Bạn có thể khởi động lại phần mềm ngay bằng file: Chay_MESUAI.bat")
    print("=" * 68)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
