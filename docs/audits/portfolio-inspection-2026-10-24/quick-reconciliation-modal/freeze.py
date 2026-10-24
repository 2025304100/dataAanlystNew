"""Freeze this completed UI acceptance without overwriting earlier evidence."""
from pathlib import Path
import hashlib
import json
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")
repo = Path.cwd().resolve()
source = repo / "tmp/inspection-quick-check-2026-10-24"
target = repo / "docs/audits/portfolio-inspection-2026-10-24/quick-reconciliation-modal"
assert not target.exists(), "Refuse to overwrite frozen evidence"
safety = json.loads((source / "safety.json").read_text(encoding="utf-8"))
browser = json.loads((source / "browser-result.json").read_text(encoding="utf-8"))
assert safety["passed"] and browser["passed"]
assert safety["audit_before"] == 3799 and safety["audit_after"] == 3800
assert "63 passed (63)" in (source / "frontend-tests.log").read_text(encoding="utf-8")
assert "built in 36.08s" in (source / "frontend-build.log").read_text(encoding="utf-8")
assert len(browser["writes"]) == 1 and not browser["blockedWrites"]
assert browser["writes"][0]["body"] == {"trade_date": "2026-09-30"}

target.mkdir(parents=True)
for path in sorted(source.iterdir()):
    if path.is_file():
        shutil.copy2(path, target / path.name)
reference = target / "reference-scripts"
reference.mkdir()
shutil.copy2(repo / "tmp/inspection-etf-fallback-2026-10-24/capture_safety.py",
             reference / "protected-table-capture.py")

readme = """# 按日快捷核对：真实 main 验收

日期：2026-10-24。范围仅为“组合交易 → 检查与对账 → 自动交易检查”中的按日核对交互。生产前端资源已构建，后端未重启。

## 已实现

- 每个交易日待办增加“快捷核对”，点击打开居中弹窗；移除原卡片内嵌的三步流程与直接解除入口。
- 打开只读取所选日期预览；“重新核对本日”才提交该日期重检。结构化日期、组合与响应范围均校验；错误或过期异步响应不覆盖当前上下文。
- 先显示日期、结论、剩余问题和处理入口，只有实际有值的差异才展示账实比较。历史缺证不再展示空比较或内部字段。
- 三类历史材料和查找方法统一折叠，无逐项登记。确实找不到的资料保留历史未知，重复重检不能清除缺证。
- 本日历史和原订单关联分别使用独立浮层，携带日期及实际报告版本，可逐层返回。关闭快捷弹窗保留父列表展开及滚动位置。
- 后端校验、放行条件和统一运行审核不变。仅本日核对通过不会表示整个组合允许交易。

## 修复的日期问题

旧内嵌流程的重检请求未携带卡片日期，09-30卡片可能显示09-29报告。新流程预览、重检、历史查询和原订单关联持续限定所选交易日；响应日期或组合不一致时拒绝显示结论。

## 验证结果

- [前端测试](frontend-tests.log)：5个相关测试文件、63项全部通过，38.64秒。
- [生产构建](frontend-build.log)：TypeScript及Vite通过，36.08秒。保留既有体积提示。
- [真实浏览器](browser-result.json)：访问本地8000生产页面，验证居中位置、缺口折叠、12次Tab焦点约束、Escape返回、滚动及展开保留、历史/原订单浮层返回和切换10-08重新读取。390px与320px无文档横向溢出；浏览器异常、接口错误及被拦截的写入尝试均为0。
- 只执行一次真实 main / 2026-09-30 重检，实际请求与报告均为该日，新增审计3800；[实际报告](live-recheck.json)仍为证据不完整、不可确认，连续核验日仍09-28。其他非读取接口在浏览器验收层全部禁止。
- [前后保护比较](safety.json)：25张业务保护表、main整行、受影响行情、基础ETF行情、自动交易任务和派发历史完全一致。审计3799→3800只有上述一次重检。
- 后台作业、自动交易、交易写入及最终审核持续关闭；新买入和自动恢复仍不允许。未恢复交易、补单、接受新基线、修改模型/策略或解冻准入。任务16、派发66，无活动任务。

## 查看效果

- [日期卡片入口](card-entry.png)
- [居中快捷核对](quick-check.png)
- [按日处理历史](quick-history.png)
- [390px](quick-390.png)与[320px](quick-320.png)

## 证据说明

本目录为本轮独立冻结，未覆盖此前验收。before/after为本次唯一重检前后的只读快照，mode和final文件记录当时安全模式、队列与运行状态。执行脚本按原样保留；capture.py原执行位置为仓库tmp/inspection-quick-check-2026-10-24，并引用上一轮已审查的只读快照实现，其副本保存在reference-scripts中。复现脚本应恢复到原工作路径后审查再运行，不应直接在冻结目录运行或覆盖证据。

manifest.json记录本目录所有其他文件的SHA-256与字节数，并记录交付源码及设计文档当时的哈希。文档和代码后续可能继续开发，历史证据保持原样。本次工作没有解决main仍存在的14日历史缺证或正式模型/策略资格问题，也没有声称组合已经可以恢复交易。
"""
(target / "README.md").write_text(readme, encoding="utf-8")

def describe(path, base):
    data = path.read_bytes()
    return {"path": path.relative_to(base).as_posix(), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}

source_paths = [
    "frontend/src/components/portfolio-trading/ReadinessRepairDrawer.tsx",
    "frontend/src/components/portfolio-trading/__tests__/ReadinessRepairDrawer.test.tsx",
    "frontend/src/components/portfolio-trading/inspection/ReconciliationQuickCheckModal.tsx",
    "frontend/src/components/portfolio-trading/inspection/reconciliationQuickCheck.css",
    "frontend/src/components/portfolio-trading/inspection/__tests__/ReconciliationQuickCheckModal.test.tsx",
    "frontend/src/i18n/zh-CN.ts", "frontend/src/i18n/en-US.ts",
    "docs/组合交易检查与对账-功能闭环与交互重构设计-2026-10-21.md",
    "docs/组合交易检查与对账-设计与v10逐项核查及补齐记录-2026-10-23.md",
]
manifest = {
    "schema_version": 1, "date": "2026-10-24", "portfolio": "main",
    "scope": "per-trading-day centered reconciliation modal",
    "files": [describe(p, target) for p in sorted(target.rglob("*")) if p.is_file()],
    "source_files": [describe(repo / p, repo) for p in source_paths],
}
(target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
for item in manifest["files"]:
    assert describe(target / item["path"], target) == item
for item in manifest["source_files"]:
    assert describe(repo / item["path"], repo) == item
print(json.dumps({"target": str(target), "frozen_files": len(manifest["files"]),
                  "source_fingerprints": len(manifest["source_files"]), "verified": True}, ensure_ascii=False))
