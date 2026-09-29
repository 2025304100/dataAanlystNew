"""白盒 - CI 分片器 `scripts/ci_shard.py` 的 partition 纯函数。

为什么单独测这个函数：分片器最怕的不是跑慢，而是**静默漏测**。本项目真实发生过
同族事故（141 个测试文件没有 marker，永远不进任何闸门，契约红藏了两个月），
所以"并集等于全量、桶间不相交、任何 job 算出来一样"必须是断言，而不是我看一眼觉得对。

duration 在新克隆上拿不到，所以均衡用"用例数"做代理指标；这里的界是 LPT 贪心的
数学性质：最重的桶 ≤ 平均负载 + 最大单文件负载。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ci_shard import partition  # noqa: E402

pytestmark = pytest.mark.whitebox

COUNTS = {
    "tests/a.py": 120,
    "tests/b.py": 80,
    "tests/c.py": 50,
    "tests/d.py": 40,
    "tests/e.py": 10,
    "tests/sub/f.py": 5,
}


def test_partition_covers_every_file_exactly_once():
    buckets = partition(COUNTS, 3)
    flat = [p for b in buckets for p in b]
    assert sorted(flat) == sorted(COUNTS), "分片丢了文件 = CI 静默漏测"
    assert len(flat) == len(set(flat)), "同一文件进多个桶 = 重复跑浪费闸门"


def test_partition_is_deterministic_regardless_of_input_order():
    """每个 job 独立算分片，结果必须逐桶一致，否则片与片之间会错位重叠。"""
    left = partition(COUNTS, 4)
    right = partition(dict(reversed(list(COUNTS.items()))), 4)
    assert left == right


def test_partition_load_stays_within_lpt_bound():
    groups = 3
    buckets = partition(COUNTS, groups)
    total = sum(COUNTS.values())
    loads = [sum(COUNTS[p] for p in b) for b in buckets]
    bound = math.ceil(total / groups) + max(COUNTS.values())
    assert max(loads) <= bound, f"最重的桶 {max(loads)} 超出 LPT 上界 {bound}"
    assert min(loads) > 0, "不该有空桶（文件数远多于片数）"


def test_partition_more_groups_than_files_leaves_empty_buckets():
    """片数超过文件数时必须有空桶而不是报错或复制文件 —— 上层会明确拒绝这种配置。"""
    buckets = partition({"tests/only.py": 10}, 4)
    flat = [p for b in buckets for p in b]
    assert flat == ["tests/only.py"]
    assert sum(1 for b in buckets if not b) == 3


def test_partition_rejects_non_positive_groups():
    with pytest.raises(ValueError):
        partition(COUNTS, 0)


def test_empty_collection_is_not_silently_accepted():
    """收集为 0 时不能给出"全绿"的分片计划（那正是零覆盖假绿的入口）。"""
    buckets = partition({}, 3)
    assert buckets == [[], [], []]
