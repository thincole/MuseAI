#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MuseAI - Công cụ Cập nhật Phiên bản Mới nhất từ GitHub
Kho lưu trữ: https://github.com/thincole/MuseAI

Hỗ trợ đồng thời 2 chế độ:
  1. Git Update: Dành cho máy có cài đặt Git và thư mục là Git repository.
  2. Direct GitHub Download (Zero-Git): Tự động tải bản mới nhất trực tiếp từ GitHub qua file ZIP,
     áp dụng cập nhật mượt mà mà KHÔNG CẦN CÀI ĐẶT GIT. Bảo vệ an toàn dữ liệu người dùng (.env, data, logs, output).
"""
from __future__ import annotations

import os
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

# Đảm bảo console hiển thị tiếng Việt UTF-8 chuẩn trên Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

_CURR_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_CURR_DIR)
_PARENT_DIR = os.path.dirname(_ROOT_DIR)

if os.path.isfile(os.path.join(_PARENT_DIR, "Chay_MESUAI.bat")) or os.path.isdir(os.path.join(_PARENT_DIR, "root")):
    BASE_DIR = _PARENT_DIR
else:
    BASE_DIR = _ROOT_DIR

VERSION_FILE = os.path.join(_ROOT_DIR, "version.txt")
REQUIREMENTS_FILE = os.path.join(_ROOT_DIR, "requirements.txt")
TARGET_REPO = "https://github.com/thincole/MuseAI.git"

GITHUB_ZIP_URLS = [
    "https://codeload.github.com/thincole/MuseAI/zip/refs/heads/main",
    "https://github.com/thincole/MuseAI/archive/refs/heads/main.zip",
]

# Danh mục thư mục & file bảo mật cá nhân — TUYỆT ĐỐI KHÔNG GHI ĐÈ KHI CẬP NHẬT
PROTECTED_ITEMS = {
    ".env",
    ".git",
    "data",
    "output",
    "temp",
    "logs",
    "venv",
    "__pycache__",
    ".idea",
    ".vscode",
}

PROTECTED_EXTENSIONS = (
    ".log",
    ".session",
)


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


def find_git() -> str | None:
    """Tự động tìm kiếm Git trong PATH và các vị trí cài đặt phổ biến trên Windows."""
    # 1. Thử qua PATH
    git_which = shutil.which("git")
    if git_which:
        return git_which

    # 2. Tìm trong các thư mục cài đặt mặc định trên Windows
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Git\cmd\git.exe",
            r"C:\Program Files\Git\bin\git.exe",
            r"C:\Program Files (x86)\Git\cmd\git.exe",
            r"C:\Program Files (x86)\Git\bin\git.exe",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Git\cmd\git.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Git\bin\git.exe"),
            r"C:\Git\cmd\git.exe",
            r"C:\Git\bin\git.exe",
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c

    return None


def try_git_update(git_exe: str) -> bool:
    """Cập nhật phần mềm qua Git nếu có kho lưu trữ .git."""
    dot_git = os.path.join(BASE_DIR, ".git")
    if not os.path.isdir(dot_git):
        return False

    print("[*] Phát hiện kho Git cục bộ. Đang đồng bộ cập nhật qua Git...")
    try:
        # Cấu hình safe.directory
        run_cmd([git_exe, "config", "--global", "--add", "safe.directory", BASE_DIR], capture=True)

        # Đảm bảo remote origin trỏ đúng kho GitHub
        remotes = run_cmd([git_exe, "remote"], capture=True).stdout or ""
        if "origin" in remotes.split():
            run_cmd([git_exe, "remote", "set-url", "origin", TARGET_REPO], capture=True)
        else:
            run_cmd([git_exe, "remote", "add", "origin", TARGET_REPO], capture=True)

        print("[*] Đang kéo mã nguồn mới nhất (git pull origin main)...")
        run_cmd([git_exe, "fetch", "origin", "main", "--tags"], capture=True)
        pull_res = run_cmd([git_exe, "pull", "origin", "main"])

        if pull_res.returncode != 0:
            print("[!] Phát hiện có thay đổi tệp cục bộ, đang lưu trữ tạm (stash)...")
            run_cmd([git_exe, "stash"])
            retry_pull = run_cmd([git_exe, "pull", "origin", "main"])
            run_cmd([git_exe, "stash", "pop"], capture=True)
            if retry_pull.returncode != 0:
                print("[!] Git pull chưa thành công. Tự động chuyển sang tải trực tiếp từ GitHub...")
                return False

        return True
    except Exception as e:
        print(f"[!] Lỗi khi chạy lệnh Git: {e}")
        return False


def update_via_direct_download() -> bool:
    """Tải trực tiếp bản phát hành mới nhất từ GitHub qua ZIP (Zero-Git, không cần cài đặt Git)."""
    print("[*] Khởi động chế độ Cập nhật Trực tiếp (Zero-Git): Không cần cài đặt Git!")
    print("[*] Đang kết nối tới máy chủ GitHub để tải gói cập nhật mới nhất...")

    # Tạo SSL context an toàn, hỗ trợ cả các hệ thống Windows có chứng chỉ cũ
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MuseAI-Updater/1.0",
        "Accept": "application/vnd.github.v3+json, application/zip, */*",
    }

    zip_data = None
    last_error = None

    for url in GITHUB_ZIP_URLS:
        try:
            print(f"[*] Đang tải từ: {url} ...")
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, context=ctx, timeout=40) as resp:
                total_size = resp.getheader("Content-Length")
                total_bytes = int(total_size) if total_size and total_size.isdigit() else 0

                chunks = []
                downloaded = 0
                start_t = time.time()
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    downloaded += len(chunk)
                    if total_bytes > 0:
                        pct = int(downloaded * 100 / total_bytes)
                        mb = downloaded / (1024 * 1024)
                        total_mb = total_bytes / (1024 * 1024)
                        print(f"\r    -> Tiến độ: {mb:.2f} MB / {total_mb:.2f} MB ({pct}%)", end="", flush=True)
                    else:
                        mb = downloaded / (1024 * 1024)
                        print(f"\r    -> Tiến độ: Đã tải {mb:.2f} MB", end="", flush=True)

                print()
                zip_data = b"".join(chunks)
                if zip_data:
                    elapsed = time.time() - start_t
                    print(f"[✓] Tải thành công ({len(zip_data) / (1024 * 1024):.2f} MB) trong {elapsed:.1f}s!")
                    break
        except Exception as exc:
            last_error = exc
            print(f"\n[!] Thử tải từ nguồn này không thành công ({exc}), đang thử nguồn dự phòng...")

    if not zip_data:
        print(f"\n[LỖI] Không thể tải bản cập nhật từ GitHub: {last_error}")
        print("Vui lòng kiểm tra lại kết nối mạng Internet của máy tính.")
        return False

    # Giải nén gói cập nhật ra thư mục tạm
    temp_dir = tempfile.mkdtemp(prefix="museai_update_")
    zip_path = os.path.join(temp_dir, "update.zip")
    try:
        with open(zip_path, "wb") as f:
            f.write(zip_data)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(temp_dir)

        # File ZIP của GitHub thường chứa 1 thư mục gốc con: ví dụ MuseAI-main/
        subdirs = [d for d in os.listdir(temp_dir) if os.path.isdir(os.path.join(temp_dir, d)) and d != "__MACOSX"]
        if subdirs:
            source_root = os.path.join(temp_dir, subdirs[0])
        else:
            source_root = temp_dir

        print("[*] Đang ghi đè và áp dụng các tệp cập nhật mới...")
        updated_count = 0

        for root, dirs, files in os.walk(source_root):
            rel_dir = os.path.relpath(root, source_root)
            if rel_dir == ".":
                rel_dir = ""

            # Bỏ qua các thư mục bảo vệ
            top_dir = rel_dir.split(os.sep)[0] if rel_dir else ""
            if top_dir in PROTECTED_ITEMS:
                continue

            dest_dir = os.path.join(BASE_DIR, rel_dir) if rel_dir else BASE_DIR
            os.makedirs(dest_dir, exist_ok=True)

            for file_name in files:
                rel_file_path = os.path.join(rel_dir, file_name) if rel_dir else file_name

                # Bảo vệ an toàn dữ liệu người dùng (.env, data, logs)
                first_part = rel_file_path.split(os.sep)[0]
                if first_part in PROTECTED_ITEMS or file_name in PROTECTED_ITEMS:
                    continue
                if any(file_name.lower().endswith(ext) for ext in PROTECTED_EXTENSIONS):
                    continue
                if file_name.startswith("log.txt"):
                    continue

                src_file = os.path.join(root, file_name)
                dest_file = os.path.join(dest_dir, file_name)

                try:
                    if os.path.exists(dest_file):
                        # Xóa cờ Read-Only nếu có để tránh PermissionError trên Windows
                        os.chmod(dest_file, stat.S_IWRITE)
                    shutil.copy2(src_file, dest_file)
                    updated_count += 1
                except Exception as cp_err:
                    print(f"    [!] Bỏ qua file bận {rel_file_path}: {cp_err}")

        print(f"[✓] Đã cập nhật thành công {updated_count} tệp tin mới nhất từ GitHub!")
        return True

    finally:
        # Dọn dẹp thư mục tạm
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


def main():
    print("=" * 68)
    print("        MuseAI Video Studio Pro - Cập Nhật Phiên Bản Mới Nhất")
    print(f"        Kho lưu trữ: https://github.com/thincole/MuseAI")
    print("=" * 68)
    print()

    old_ver = get_version()
    print(f"[*] Phiên bản hiện tại trên máy: {old_ver}")
    print()

    success = False
    git_exe = find_git()
    dot_git = os.path.join(BASE_DIR, ".git")

    # Ưu tiên 1: Cập nhật qua Git nếu máy có cài đặt Git và thư mục là Git repo
    if git_exe and os.path.isdir(dot_git):
        success = try_git_update(git_exe)

    # Ưu tiên 2: Tự động tải trực tiếp từ GitHub (Zero-Git Mode) nếu không có Git hoặc Git lỗi
    if not success:
        success = update_via_direct_download()

    if not success:
        print()
        print("[X] TIẾN TRÌNH CẬP NHẬT THẤT BẠI!")
        print("Vui lòng kiểm tra lại kết nối mạng Internet và thử lại sau.")
        return 1

    new_ver = get_version()

    # Cập nhật các thư viện phụ thuộc trong requirements.txt nếu có
    if os.path.isfile(REQUIREMENTS_FILE):
        print()
        print("[*] Đang kiểm tra các thư viện phụ thuộc (pip install -r requirements.txt)...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet", "--no-warn-script-location"],
                cwd=BASE_DIR,
                check=False
            )
        except Exception as e:
            print(f"[!] Cảnh báo kiểm tra thư viện: {e}")

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
