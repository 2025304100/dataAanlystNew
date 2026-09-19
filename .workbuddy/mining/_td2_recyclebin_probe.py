#!/usr/bin/env python3
"""TD2 附带探针：解析 D:/ C:/ $Recycle.Bin 的 $I 元数据，统计 integration_* 遗留去向。

$I 文件格式（v1/v2）：header(8B version) + size(8B LE) + delete_time(8B FILETIME) + 原始路径(UTF-16)。
v1 路径固定 520B；v2 为 4B 长度 + 字符串。
"""
import glob
import os
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8")


def parse_i_file(path: str):
    try:
        with open(path, "rb") as f:
            data = f.read()
        if len(data) < 24:
            return None
        version = struct.unpack("<q", data[0:8])[0]
        size = struct.unpack("<q", data[8:16])[0]
        if version == 2:
            (nchars,) = struct.unpack("<i", data[24:28])
            raw = data[28:28 + nchars * 2]
        else:
            raw = data[24:24 + 520]
        orig = raw.decode("utf-16-le", errors="ignore").split("\x00")[0]
        return size, orig
    except OSError:
        return None


def main() -> None:
    grand_hits = 0
    grand_bytes = 0
    for drive in ("D:/", "C:/"):
        root = os.path.join(drive, "$Recycle.Bin")
        if not os.path.isdir(root):
            print(f"{root}: 不存在")
            continue
        hits = 0
        total_bytes = 0
        samples = []
        denied = False
        for sid_dir in glob.glob(os.path.join(root, "*")):
            try:
                names = os.listdir(sid_dir)
            except OSError:
                denied = True
                continue
            for name in names:
                if not name.startswith("$I"):
                    continue
                parsed = parse_i_file(os.path.join(sid_dir, name))
                if parsed is None:
                    continue
                size, orig = parsed
                low = orig.lower()
                if "integration_" in low and "sqlite3" in low:
                    hits += 1
                    total_bytes += size
                    if len(samples) < 3:
                        samples.append(f"  {orig} ({size} B)")
        print(f"{root}: integration_*sqlite3 条目 {hits} 个, 共 {total_bytes / 1024 / 1024:.1f} MB"
              + ("（部分 SID 目录拒绝访问，计数可能偏小）" if denied else ""))
        for s in samples:
            print(s)
        grand_hits += hits
        grand_bytes += total_bytes
    print(f"合计: {grand_hits} 个, {grand_bytes / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
