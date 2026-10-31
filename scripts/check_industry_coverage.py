"""行业口径覆盖率验证（缺陷清单 #3 / #11 / #13 的前置检查）。

回答三个问题：
  1. 同花顺 90 个行业是否覆盖全部 A 股？（#13）
  2. 「两市成交额」到底是哪两市？（#11）
  3. 行业净额加总会不会因未归类而偏小？（#13）

用法：
    .venv/Scripts/python.exe scripts/check_industry_coverage.py
    .venv/Scripts/python.exe scripts/check_industry_coverage.py --json out.json

退出码：0 = 覆盖率达标；1 = 覆盖率异常，需人工确认。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import requests

SINA_NODE_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"

# 板块前缀 → 名称。用于把「全市场」拆成可解释的分块。
BOARD_BY_PREFIX = {
    "60": "沪主板",
    "00": "深主板",
    "30": "创业板",
    "68": "科创板",
    "92": "北交所",
}
B_PREFIX = {"900": "沪B", "200": "深B"}

# 覆盖率判定阈值（同花顺家数合计 / 新浪 A 股数）
# 第三轮审查 #72：原 0.98–1.10 过宽（0.98 允许漏 111 只），收窄。
COVERAGE_MIN = 0.995
COVERAGE_MAX = 1.02
# 未解释差值上限（#72）：超过即 fail，不再只打日志
MAX_UNEXPLAINED = 5


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
        "Referer": "https://vip.stock.finance.sina.com.cn/",
    })
    return s


def fetch_node(s: requests.Session, node: str, max_pages: int = 60) -> list[dict]:
    """按 node 分页拉取，动态翻页（不写死页数，见缺陷 #18）。"""
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        r = s.get(SINA_NODE_URL, params={
            "page": page, "num": 100, "sort": "symbol", "asc": 1,
            "node": node, "symbol": "", "_s_r_a": "page",
        }, timeout=20)
        r.raise_for_status()
        part = r.json()
        if not isinstance(part, list) or not part:
            break
        out.extend(part)
        if len(part) < 100:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="把结果另存为 JSON")
    args = ap.parse_args()

    s = _session()

    # ---------- 1 新浪全市场（hs_a = A 股，含北交所）----------
    a_rows = fetch_node(s, "hs_a")
    pref = collections.Counter(str(x.get("code", ""))[:2] for x in a_rows)
    a_total = len(a_rows)
    bj = pref.get("92", 0)
    hs = a_total - bj          # 沪深两市（不含北交所）

    # ---------- 2 B 股（用于解释差值）----------
    b_total = 0
    b_detail = {}
    for node, label in (("sh_b", "沪B"), ("sz_b", "深B")):
        try:
            rows = fetch_node(s, node, max_pages=3)
            b_detail[label] = len(rows)
            b_total += len(rows)
        except Exception as exc:                     # noqa: BLE001
            b_detail[label] = f"ERR:{type(exc).__name__}"

    # ---------- 3 同花顺 90 行业 ----------
    import akshare as ak
    ind = ak.stock_fund_flow_industry(symbol="即时")
    if "公司家数" not in ind.columns:
        print("FATAL: 同花顺返回中没有『公司家数』列，接口可能已改版", file=sys.stderr)
        return 1
    ind_total = int(ind["公司家数"].sum())
    n_ind = len(ind)

    # ---------- 4 判定 ----------
    # 第三轮 #76：未知前缀必须为 0，否则 hs = a_total - bj 会静默把未知板块算进沪深
    known = set(BOARD_BY_PREFIX) | {"90", "20"}      # 含 B 股前缀
    unknown = {k: v for k, v in pref.items() if k not in known}

    ratio = ind_total / a_total if a_total else 0
    gap = ind_total - a_total

    # 第三轮 #70：不再假设「差值=B股」。B 股数量只作参考，不计入 explained。
    # 原因：同花顺「公司家数」语义未确认（银行 84 vs 申万 42），
    #      用 B 股数解释差值是未经证实的假设。
    unexplained = gap

    ok = (
        COVERAGE_MIN <= ratio <= COVERAGE_MAX
        and abs(unexplained) <= MAX_UNEXPLAINED
        and not unknown
    )

    report = {
        "run_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "sina_a_total": a_total,
        "sina_board_split": {BOARD_BY_PREFIX.get(k, k): v for k, v in sorted(pref.items())},
        "unknown_prefix": unknown,
        "sina_hs_two_markets": hs,
        "sina_bj": bj,
        "b_share_total": b_total,
        "b_share_detail": b_detail,
        "ths_industry_count": n_ind,
        "ths_company_total": ind_total,
        "coverage_ratio": round(ratio, 4),
        "gap": gap,
        "gap_unexplained": unexplained,
        "verdict": "ok" if ok else "check",
        "caveats": [
            "同花顺『公司家数』语义未确认（银行 84 vs 申万 42），不可用于论证『净额可加』",
            "净额对账基准拿不到（push2 阻断），只能声明『不可对账』",
        ],
    }

    # ---------- 5 人类可读输出 ----------
    print("=" * 60)
    print("行业口径覆盖率验证")
    print("=" * 60)
    print(f"新浪 hs_a（A股，含北交所）: {a_total} 只")
    for k, v in sorted(pref.items(), key=lambda x: -x[1]):
        print(f"    {k}xx {BOARD_BY_PREFIX.get(k, '?'):<6} {v:>5}")
    print(f"  → 沪深两市（不含北交所）: {hs} 只")
    print(f"  → 北交所: {bj} 只")
    print()
    print(f"B 股（仅参考，不计入解释）: {b_total} 只 {b_detail}")
    print()
    print(f"同花顺行业: {n_ind} 个, 公司家数合计 {ind_total}")
    print(f"覆盖率 = {ind_total} / {a_total} = {ratio * 100:.2f}%  (阈值 {COVERAGE_MIN*100:.1f}–{COVERAGE_MAX*100:.1f}%)")
    print(f"差值 = {gap:+d}（未解释 {unexplained:+d}，上限 ±{MAX_UNEXPLAINED}）")
    if unknown:
        print(f"⚠ 未知前缀: {unknown}  ← 必须为 0，否则沪深口径被污染")
    print()

    if ok:
        print("结论: 数量级对得上，无明显缺口。")
        print("      但注意：这不等于『净额可加』——")
        print("        · 同花顺『公司家数』语义未确认（银行 84 vs 申万 42）")
        print("        · 标的级互斥无法验证（拿不到成分股清单）")
        print("        · 净额对账基准拿不到（push2 阻断）")
        print("      → 只能声明『结构上应互斥，但不可对账』")
    else:
        print("结论: 未通过。")
        if not (COVERAGE_MIN <= ratio <= COVERAGE_MAX):
            print(f"      · 覆盖率 {ratio*100:.2f}% 超出阈值")
        if abs(unexplained) > MAX_UNEXPLAINED:
            print(f"      · 未解释差值 {unexplained:+d} 超过 ±{MAX_UNEXPLAINED}")
        if unknown:
            print(f"      · 存在未知前缀 {unknown}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n已保存 {args.json}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
