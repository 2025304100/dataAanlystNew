# -*- coding: utf-8 -*-
"""TD6 卡收尾补丁：迁移修订文件登记 + not_do/artifacts/pitfalls 与实况同步。
幂等；改前改后由 _selfcheck_conflict.py 把关（外层跑）。
"""
import json

P = r"D:\ai_project\dataAanlystNew\.workbuddy\mining\tasks.json"
MIG = "alembic/versions/2026_09_18_0058_wps_0023_056_td6_snapshot_binding_columns.py"

with open(P, encoding="utf-8") as f:
    data = json.load(f)

card = next(t for t in data["tasks"] if t["id"] == "TD6")

if MIG not in card["writes"]:
    card["writes"].insert(4, MIG)
card["granularity_exempt"] = True
card["granularity_note"] = (
    "第9产物为收尾补的迁移修订：DoD③ 暴露迁移链从未覆盖三列（TD5 C2 历史成因定位："
    "线上靠 auto-align 补出），不补则纯迁移链环境（G1 契约测试/新装）必炸，闭环必需"
)

# not_do[0] 与实况同步：修订是声明性对齐，线上三列已在=幂等跳过（实测 upgrade 全链 no-op）
card["not_do"][0] = (
    "不改变线上表实态（0056 修订为声明性对齐：线上三列已存在=幂等跳过，"
    "scratch sqlite 全链 upgrade 验证通过、downgrade -1 干净回退）"
)

art0 = "ORM 补 3 列+索引对齐 DB 实态（col_db_only/extra_idx 清零）+ 新模型壳化"
if "迁移修订 wps_0023_056" not in card["artifacts"]:
    card["artifacts"].insert(
        1,
        "迁移修订 wps_0023_056：三列+三索引补进迁移链（MySQL 三段式/SQLite NOT NULL DEFAULT，幂等防御式）",
    )

# pitfalls 第4条补充真相结论
card["pitfalls"][3] = (
    "★ DB 三列是迁移体系之外的加宽（alembic 无 add_column 记录）——以 SHOW CREATE TABLE "
    "实态为唯一权威；TD6 定位历史成因：线上靠历史进程 auto-align 补出，迁移链产物一直是旧结构，"
    "『ORM 修对』后该缺口显性化（G1 契约测试 7 红），必须补修订闭环而非改 fixture 绕过"
)

with open(P, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")

print("TD6 card updated: writes=%d exempt=%s" % (len(card["writes"]), card["granularity_exempt"]))
