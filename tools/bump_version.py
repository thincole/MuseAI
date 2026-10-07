"""
MuseAI Version Helper
Hỗ trợ kiểm tra phiên bản hiện tại, kiểm tra Git Tag và tự động tính toán phiên bản tiếp theo.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(BASE_DIR, "version.txt")


def get_current_version() -> str:
    if os.path.isfile(VERSION_FILE):
        try:
            with open(VERSION_FILE, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except Exception:
            pass
    return "1.2.1"


def get_next_version(current: str) -> str:
    # Kiểm tra xem thẻ Git tag v{current} đã tồn tại chưa
    tags: list[str] = []
    try:
        res = subprocess.check_output(
            ["git", "-c", "safe.directory=*", "tag", "-l", f"v{current}"],
            cwd=BASE_DIR,
            stderr=subprocess.DEVNULL,
            text=True
        ).strip()
        tags = res.split()
    except Exception:
        tags = []

    # Nếu tag đã tồn tại trên git, tăng số patch lên 1 (ví dụ 1.2.1 -> 1.2.2)
    if f"v{current}" in tags:
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", current)
        if m:
            return f"{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}"
    return current


if __name__ == "__main__":
    curr = get_current_version()
    if len(sys.argv) > 1 and sys.argv[1] == "--next":
        print(get_next_version(curr))
    elif len(sys.argv) > 2 and sys.argv[1] == "--save":
        new_v = sys.argv[2].strip()
        with open(VERSION_FILE, "w", encoding="utf-8") as f:
            f.write(new_v + "\n")
        print(new_v)
    else:
        print(curr)
