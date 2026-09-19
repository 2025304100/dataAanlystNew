"""配置哈希：向导配置的**唯一**规范化实现（SD-v2.0 §6.15；任务 T16）。

为什么必须收敛到一处
==================
配置哈希是三条链路的共同键：
1. **校验任务幂等**（需求 §3.5）：同一草稿 + 同 config_hash 不得重复创建校验任务
2. **快照/运行溯源**：`factor_mining_runs.rule_hash` 与模板版本绑定
3. **数据修复回流**（向导 §5.1）：每次回流都**重新生成** config_hash

只要有两处实现（哪怕算法看似相同），将来任何一侧调整（例如决定保留 None、
或加入新字段）都会让「同一配置」在两条链路上算出不同键 ——
表现为幂等失效、重复建任务，且**不会报错**。

因此 T14 在 `validation_service.compute_config_hash` 里的临时实现
由本模块接管；`test_config_hash.py::test_parity_with_validation_service`
钉死两者在当前版本一致，T14 侧改为 import 本模块属后续清理（该文件不在
T16 写权限内）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

#: 草稿步骤键（与 `factor_mining_drafts.stepN_json` 对应）
DRAFT_STEP_KEYS: tuple[str, ...] = ("step1", "step2", "step3", "step4")

#: 参与 config_hash 的步骤：Step4 是进化参数，**不参与**（改参数不重跑字段校验）
HASHED_STEPS: tuple[str, ...] = ("step1", "step2", "step3")

#: 哈希长度（16 位十六进制 = 64 bit，足够区分同草稿内的配置变体）
HASH_LENGTH = 16


def strip_none(value: Any) -> Any:
    """规范化：**字典里**值为 None 的键去掉；**列表里的 None 保留**。

    为什么两者不对称（T16 parity 测试抓出来的真实分歧点）
    --------------------------------------------------
    - **字典**：`None` 与「未设置」语义等价。前端清空一个字段常表现为
      `{"a": None}`，若不归一，就会出现 `{"a": 1}` 与 `{"a": 1, "b": None}`
      两个哈希 → 幂等失效、重复创建校验任务。
    - **列表**：位置本身有语义。`["close", None, "pe_ttm"]` 里的 None 是
      「这个槽位为空」，与 `["close", "pe_ttm"]` 是**不同的配置**
      （长度、下标都变了）。删掉会让两种配置同哈希。
      故列表内的 None **原样保留**参与哈希。

    > 对照：T11 的 `_freeze_json` 用于**快照落库**，必须保留全部 None ——
    > 三者用途不同（哈希归一 / 快照保真），不要互相替换。
    """
    if isinstance(value, Mapping):
        return {k: strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        # 注意：不过滤 None —— 见上方「列表位置有语义」
        return [strip_none(v) for v in value]
    return value


def canonical_config_payload(config: Mapping[str, Any] | None) -> str:
    """规范化 JSON 串（键排序 + 紧凑分隔 + 去 None），哈希的稳定输入。"""
    stripped = strip_none(dict(config or {}))
    return json.dumps(stripped, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True)


def compute_config_hash(config: Mapping[str, Any] | None) -> str:
    """配置 → 哈希（键序无关、None 无关、值相关）。

    同一输入**必须**永远得到同一结果（跨进程、跨重启）——
    故用 SHA-256，绝不用 Python 内置 `hash()`（有随机化种子，铁律 R4）。
    """
    payload = canonical_config_payload(config)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def steps_to_config(steps: Mapping[str, Any] | None) -> dict[str, Any]:
    """把 `{"step1": {...}, ...}` 收成参与哈希的子集（Step4 不参与）。"""
    src = dict(steps or {})
    return {k: src[k] for k in HASHED_STEPS if src.get(k) is not None}


def compute_draft_config_hash(
    *, step1: Mapping[str, Any] | None = None,
    step2: Mapping[str, Any] | None = None,
    step3: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """草稿步骤 → config_hash。

    - 只吃 Step1~Step3（候选池 / 时间与目标 / 字段与校验）
    - Step4（进化参数）不参与：改参数不应导致字段校验重跑
    - `extra` 供后续追加共识字段（例如快照 id），当前调用方传 None 即可
    """
    payload: dict[str, Any] = {}
    for key, value in (("step1", step1), ("step2", step2), ("step3", step3)):
        if value is not None:
            payload[key] = value
    if extra:
        payload.update(extra)
    return compute_config_hash(payload)


def compute_step_hash(step: Any) -> str:
    """单个步骤的哈希（用于「草稿是否被改动」的局部比较）。"""
    return compute_config_hash({"step": step})


def hashes_differ(a: str | None, b: str | None) -> bool:
    """None 安全的哈希比较（None 视为「未知」，与任何值都算不同）。"""
    if a is None or b is None:
        return a != b
    return a != b


def summarize_differences(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None,
    *, keys: Sequence[str] | None = None,
) -> list[str]:
    """列出两个配置在哪些字段上不同（供「配置已变更」提示）。"""
    keys = list(keys) if keys is not None else sorted(
        set(before or {}) | set(after or {}))
    out: list[str] = []
    for key in keys:
        b = strip_none((before or {}).get(key))
        a = strip_none((after or {}).get(key))
        if b != a:
            out.append(key)
    return out


__all__ = [
    "DRAFT_STEP_KEYS",
    "HASHED_STEPS",
    "HASH_LENGTH",
    "strip_none",
    "canonical_config_payload",
    "compute_config_hash",
    "steps_to_config",
    "compute_draft_config_hash",
    "compute_step_hash",
    "hashes_differ",
    "summarize_differences",
]
