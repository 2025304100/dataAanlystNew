"""CI 分片器：把 `-m <marker>` 收集到的测试按**文件**均衡分到 N 个并行 job。

为什么不让 CI 手写路径清单（比如 `tests/factors tests/services`）：
清单会过期。新增目录/新文件一旦没被任何一条清单覆盖，就会被**静默跳过** ——
本项目真实发生过同族事故（141 个测试文件没有 marker，CI 永远不跑，契约红藏了
两个月，见体检报告 §十四）。这里改成"先收集、再按收集结果分片"，覆盖与否由
收集决定，不依赖人工维护。

分片单位是文件而不是单个用例：同一文件里的用例共享昂贵的 fixture 导入与建库，
切散反而更慢；文件间用"用例数"做贪心均衡（LPT），因为新克隆上没有
duration 数据可用，用例数是唯一稳定可得的代理指标。

用法：
    python scripts/ci_shard.py --marker whitebox --groups 4 --group 0
    python scripts/ci_shard.py --marker whitebox --groups 4 --plan   # 只看分片计划
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent


def collect(marker: str) -> dict[str, int]:
    """返回 {测试文件相对路径: 该文件被收集到的用例数}。

    不解析 pytest 控制台输出，也不在同进程里跑 pytest.main：实测两条都会坑我
    （前者输出是树形且汇总数字含 deselect；后者收集完整个白盒套件后
    父进程 stdout 被测试模块导入副作用影响，导致后续子进程 pytest 的输出看不见）。
    改用子进程 + 临时文件交接，详见 scripts/_ci_collect_plugin.py。
    """
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w+", suffix=".nodeids", delete=False, encoding="utf-8"
    ) as fh:
        collect_file = Path(fh.name)
    collect_file.unlink(missing_ok=True)

    env = dict(os.environ)
    env["CI_SHARD_COLLECT_FILE"] = str(collect_file)
    env["PYTHONPATH"] = str(SCRIPTS_DIR) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-m", marker,
            "--collect-only", "-q", "--no-header", "-p", "no:cacheprovider",
            "-p", "_ci_collect_plugin",
        ],
        cwd=str(REPO), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )

    try:
        if not collect_file.exists():
            raise SystemExit(
                f"[ci_shard] 收集没产出文件（marker={marker}, rc={proc.returncode}）。"
                f"\nstdout 尾：\n{proc.stdout[-1200:]}\nstderr 尾：\n{proc.stderr[-1200:]}"
            )
        kept = [n for n in collect_file.read_text(encoding="utf-8").splitlines() if n.strip()]
    finally:
        collect_file.unlink(missing_ok=True)

    if not kept:
        raise SystemExit(
            f"[ci_shard] marker={marker} 收集到 0 个用例 —— CI 会拿零覆盖当通过，"
            f"先修收集。\nstdout 尾：\n{proc.stdout[-1200:]}"
        )

    counts: dict[str, int] = defaultdict(int)
    for nodeid in kept:
        counts[nodeid.split("::", 1)[0]] += 1

    # 外部校验：每个文件都得真的在磁盘上（防止把参数化后缀/异常 nodeid 当成路径）
    missing = [p for p in counts if not (REPO / p).exists()]
    if missing:
        raise SystemExit(
            f"[ci_shard] {len(missing)} 个收集结果在磁盘上找不到对应文件："
            f"{missing[:5]}"
        )
    return dict(counts)


def partition(counts: dict[str, int], groups: int) -> list[list[str]]:
    """把文件按用例数贪心分到 groups 桶（LPT：大的先放，装进当前最空的桶）。

    确定性：先按 (-用例数, 路径) 排序，所以任何 job 独立计算都得到同一结果，
    不需要在 CI 之间传中间状态。
    """
    if groups < 1:
        raise ValueError(f"groups 必须 >= 1，实际 {groups}")
    order = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    buckets: list[list[str]] = [[] for _ in range(groups)]
    load = [0] * groups
    for path, weight in order:
        i = load.index(min(load))
        buckets[i].append(path)
        load[i] += weight
    return buckets


def main() -> int:
    parser = argparse.ArgumentParser(description="按收集结果把测试均衡分片")
    parser.add_argument("--marker", required=True, help="如 whitebox / blackbox")
    parser.add_argument("--groups", type=int, required=True, help="分片总数")
    parser.add_argument("--group", type=int, help="本 job 负责第几片（0 起）")
    parser.add_argument("--plan", action="store_true", help="只打印分片计划，不跑测试")
    args = parser.parse_args()

    counts = collect(args.marker)
    buckets = partition(counts, args.groups)

    if args.plan:
        total = sum(counts.values())
        print(f"marker={args.marker} 文件 {len(counts)} 个 / 用例 {total} 个 → {args.groups} 片")
        for i, bucket in enumerate(buckets):
            load = sum(counts[p] for p in bucket)
            print(f"  [group {i}] 文件 {len(bucket):>3} 用例 {load:>5} ({load / total:.1%})")
        # 覆盖自检：所有桶并起来必须正好等于全量收集
        flat = [p for b in buckets for p in b]
        assert sorted(flat) == sorted(counts), "分片丢了文件，CI 会静默漏测"
        assert len(flat) == len(set(flat)), "同一文件被分进多个桶，会重复跑"
        return 0

    if args.group is None:
        parser.error("非 --plan 模式必须给 --group")
    if not (0 <= args.group < args.groups):
        parser.error(f"--group 必须在 0..{args.groups - 1}，实际 {args.group}")

    bucket = buckets[args.group]
    load = sum(counts[p] for p in bucket)
    # flush：不刷的话父进程清单会排在子进程结果后面，CI 日志顺序很误事
    print(f"[ci_shard] group {args.group}/{args.groups}：文件 {len(bucket)} 个，用例 {load} 个", flush=True)
    for p in bucket:
        print(f"   - {p} ({counts[p]})", flush=True)
    if not bucket:
        parser.error(f"group {args.group} 分不到任何文件（groups 比文件数还多？）")

    cmd = [sys.executable, "-m", "pytest", "-m", args.marker, "--tb=short", "-q", *bucket]
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
