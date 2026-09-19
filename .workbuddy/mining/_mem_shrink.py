# -*- coding: utf-8 -*-
"""MEMORY.md 压缩至 <=3000 字。单进程读-改-写原子完成，每处替换校验命中一次。"""
from pathlib import Path

P = Path(r"D:\ai_project\dataAanlystNew\.workbuddy\memory\MEMORY.md")
t = P.read_text(encoding="utf-8")

pairs = [
    ("pytest 加 --basetemp。🚨 SAFE_DELETE shim 拦 unlink 进回收站；大量删 tmp 设 CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD=1000、真删 ENABLED=0。",
     "pytest 加 --basetemp。SAFE_DELETE shim 拦 unlink 进回收站：大批删 tmp 设 BULK_THRESHOLD=1000，真删设 ENABLED=0。"),
    ("prev_close 是投影虚字段勿删。Grep/Glob 显示路径可能丢层级——定位用 os.walk。",
     "prev_close 是投影虚字段勿删；Grep/Glob 路径可能丢层级——定位用 os.walk。"),
    ("✅ 技术债 **TD1~TD6 全清**（49/127 起底→技术债报告 §7/§8；C1=utf8mb3 豁免+`text_charset` 写入守卫；C2=snapshot 三列归 decision_engine+修订 0060；遗留 FK30/CHECK19/service id 契约 P1 挂账）。",
     "✅ 技术债 **TD1~TD6 全清**（详见技术债报告 §7/§8；C1 守卫/C2 修订 0060 已落地；遗留 FK30/CHECK19/service id 契约 P1）。"),
    ("metrics_json 写入走 clean_json_tree+allow_nan=False（TD3）。",
     "metrics_json=clean_json_tree+allow_nan=False（TD3）。"),
    ("外键须行为验证（1452/级联）。",
     "外键须行为验证（1452）。"),
    ("🚨 测试建表双路径：conftest `db_session`=auto-align（按当前 metadata）、G0/G1 契约 fixture=**纯迁移链**——ORM 修对而迁移链没跟上必炸，修复前绿=「双错一致」假绿（TD6 教训）。",
     "🚨 测试建表双路径：conftest db_session=auto-align（metadata 全量）、G0/G1 契约 fixture=纯迁移链——ORM 修对而迁移链没跟上必炸，修复前绿=「双错一致」假绿（TD6）。"),
    ("进度唯一事实源 PROGRESS.json；**开工双登记**（tasks.json 卡+PROGRESS 条目）；收工 DoD→PROGRESS→update_progress_doc.py；已裁决项不重开。",
     "进度唯一事实源 PROGRESS.json；开工双登记；收工 DoD→PROGRESS→update_progress_doc.py；已裁决项不重开。"),
]

for old, new in pairs:
    n = t.count(old)
    assert n == 1, "hit=%d for %r..." % (n, old[:30])
    t = t.replace(old, new)

P.write_text(t, encoding="utf-8")
print("chars=%d" % len(t))
assert len(t) <= 3000, "still over limit"
print("OK under limit")
