# BFG 基线对账报告 / Baseline Reconciliation Report
## 量化回测过滤治理 / Quant Backtest Filter Governance

**Document / 文档编号**: BFG-RECON-20260831
**Date / 生成日期**: 2026-08-31
**Window / 对账区间**: 2024-08-10 ~ 2024-09-08 (30 合成交易日 / 30 synthetic trading days)
**Universe / 对账标的**: 10 symbols (SID=1..10)
**Report Author / 编制方**: Quant Platform (automated via `scripts/backtest_filter_dual_run_compare.py` + `tests/test_backtest_filter_30d_replay.py`)
**Status / 状态**: DRAFT — pending tri-party signatures

---

## 1. 对账项目说明 / Reconciliation Items (7 Dimensions)

每项包含：项目名、计算方式、通过阈值。中英文双语描述。
Each entry contains: name, calculation, acceptance threshold (bilingual).

| # | 项目 / Item | 计算方式 / Calculation | 阈值 / Threshold |
|---:|---|---|---|
| 1 | **成交笔数 / Trade Count**<br><small>按 (run_id, trade_date, symbol_id) 聚合 BUY/SELL/FORCE_LIQUIDATE 事件次数。<br>Count BUY/SELL/FORCE_LIQUIDATE events bucketed by (run, date, symbol).</small> | AFTER 与 BEFORE 差异逐条归因。<br>AFTER 笔数 = BEFORE 笔数 − 正确过滤项。<br>Every diff individually classified. |
| 2 | **持仓数量 / Position Closing Quantity**<br><small>按 (run_id, trade_date, symbol_id) 的 closing_qty 对比。<br>Compare closing position quantity per run per date per symbol.</small> | `|after − before| ≤ ε=1e-6` 或因退市强平/停牌冻结造成的合法归零。<br>Exact match unless liquidation (DELISTED -> 0) or freeze (opening=closing) lawfully applied. |
| 3 | **强平收益 / Force-Liquidation PnL**<br><small>sum of `(exit_price − avg_cost) × qty` for side=FORCE_LIQUIDATE 事件。<br>PnL attributed to every DELISTED mandatory liquidation event.</small> | BEFORE 缺失 → AFTER 新增为「正确修正」。数值对不上需人工复核。<br>New positive entries in AFTER = lawful correction; value mismatches → manual review. |
| 4 | **净值 / Net Asset Value (NAV)**<br><small>逐日累计 run 总净值 `nav(trade_date)`。<br>Per-run cumulative net asset value at each trade date.</small> | `|after − before| ≤ 0.5% × before_nav` 或强平/冻结带来的已知差异。<br>Within 0.5% relative tolerance OR fully explained by liquidation/freeze deltas. |
| 5 | **覆盖率 / Candidate Coverage**<br><small>`ratio = eligible_count / total_candidate_count` 逐日比较。<br>Daily ratio of (post-filter pool size) / (pre-filter pool size).</small> | AFTER 覆盖率 ≤ BEFORE 覆盖率（过滤降低覆盖率属于预期）。<br>AFTER ratio is expected to be ≤ BEFORE ratio (filters reduce the candidate universe). |
| 6 | **排除统计 / Exclusion-by-Rule Stat**<br><small>按 (trade_date, rule_code) 聚合 exclude 事件计数。<br>Exclude event counts aggregated per rule code per day.</small> | 仅 AFTER 新增条目被视为「过滤治理新增正确修正」。<br>Only AFTER-side net new entries are treated as lawful filter-governance corrections. |
| 7 | **最后交易日 / Last Trade Date per Symbol**<br><small>每个 symbol 最后一次 BUY/SELL/FORCE_LIQUIDATE 事件出现的日期。<br>The last date on which each symbol had any trade execution event.</small> | AFTER 更早属于正确（停牌/退市阻断后续伪交易）；更晚需人工复核。<br>AFTER earlier than BEFORE → lawful correction; later → suspicious, manual review needed. |

---

## 2. 差异清单 / Difference Ledger (Top 10 Sample Rows)

说明：以下样本来自本项目合成模拟数据（BEFORE runs = 1,2,3；AFTER runs = 11,12,13），
覆盖 2024-01-01 ~ 2024-06-01 区间。字段定义与 scripts/backtest_filter_dual_run_compare.py 输出一致。
The following sample rows are synthetic data produced by this project's offline
generator (BEFORE runs = 1,2,3; AFTER runs = 11,12,13; range 2024-01-01 to 2024-06-01).
Schema matches the dual-run CSV output.

| # | run_id_before | run_id_after | symbol_id | trade_date | 差异类型 / Metric | value_before | value_after | delta | rule_code | 解释分类 / Class | 中文解释 / Explanation (CN) |
|---:|---:|---:|---:|---|---|---:|---:|---:|---|---|---|
| 1 | 1 | 11 | 3 | 2024-01-15 | trade_count | 3 | 0 | −3 | SUSPENDED_EXCLUDE | NEW_CORRECTION | AFTER 在停牌日 2024-01-15 正确拦截：symbol_id=3 全天停牌。BEFORE 错误地在 SUSPENDED 状态模拟了 3 笔成交，属停牌回填前收的违规行为。/ AFTER correctly filters SUSPENDED-day trades; BEFORE had fake 3 fills using prev_close backfill on suspended bar — that is the exact violation prevented by Task 29 ANTI_CORRUPTION_FAKE_SUSPENSION_PRICE. |
| 2 | 1 | 11 | 7 | 2024-01-18 | trade_count | 2 | 0 | −2 | ST_EXCLUDE | NEW_CORRECTION | AFTER 正确：ST 生效期间禁止新开仓（2 笔 BUY）。BEFORE 错误买入 ST 股票。/ AFTER correctly blocks 2 BUY orders while symbol=7 was under ST status; BEFORE wrongly entered ST names as new positions. |
| 3 | 2 | 12 | 14 | 2024-02-06 | position_qty | 1500.00 | 0.00 | −1500.00 | DELISTING_LIQUIDATION_MANDATORY | NEW_CORRECTION | AFTER 在摘牌日按最后收盘价 10.50 强制平仓 1500 股，closing_qty 归零。BEFORE 持仓被错误保留到退市之后。/ AFTER delisted symbol=14 on 2024-02-06 is forced-liquidated at last close 10.50 (qty 1500); BEFORE incorrectly retained a post-delisting position. |
| 4 | 2 | 12 | 14 | 2024-02-06 | force_liquidate_pnl | 0.00 | +2250.00 | +2250.00 | DELISTING_LIQUIDATION_MANDATORY | NEW_CORRECTION | 摘牌强平收益：(10.50 − 9.00) × 1500 = 2250 元，AFTER 正确记账，BEFORE 缺失此事件。/ Delisting liquidation PnL realised by AFTER: (10.50 − 9.00) × 1500 = 2250; BEFORE was missing the event. |
| 5 | 3 | 13 | 2 | 2024-03-20 | trade_count | 1 | 0 | −1 | NEW_LISTING_EXCLUDE | NEW_CORRECTION | AFTER：次新股上市 119 天，低于 120 天阈值，拦截 1 笔新开仓。BEFORE 未实施次新股过滤。/ AFTER blocks one NEW open on symbol=2 on day 119 post-IPO (below 120-day threshold); BEFORE had no listing-age gate. |
| 6 | 3 | 13 | 9 | 2024-03-22 | nav | 1,008,314 | 1,004,120 | −4,194 | — | NEW_CORRECTION | 净值差异 4194 元（约 0.42%），完全由 symbol=9 退市整理期伪成交被剔除导致。容差 ≤ 0.5% 通过。/ NAV delta = 4194 (≈ 0.42% of before), explained by removing DELISTING_PERIOD fake trades on symbol=9; within 0.5% tolerance. |
| 7 | 1 | 11 | 6 | 2024-01-22 | coverage | 0.95 | 0.80 | −0.15 | ST_EXCLUDE / NEW_LISTING_EXCLUDE | NEW_CORRECTION | AFTER 覆盖率从 95% 降到 80%（15% 净排除），来自当日 2 个 ST + 1 个次新股正确排除。属预期行为。/ Coverage drops 95% → 80% (net 15 pct points) because AFTER lawfully excludes 2 ST + 1 NEW_LISTING candidates that day — expected. |
| 8 | 2 | 12 | 6 | 2024-02-14 | exclude_by_rule | 0 | 4 | +4 | DELISTING_PERIOD_EXCLUDE | NEW_CORRECTION | 当日 symbol=6 家族 4 只进入整理期，AFTER 按 RULE_DELISTING_PERIOD_EXCLUDE 每只 1 条排除事件统计。BEFORE 完全没有此规则。/ On 2024-02-14 four symbols in group 6 enter the delisting period; AFTER emits 4 rule events; BEFORE had no such gate. |
| 9 | 1 | 11 | 3 | 2024-01-17 | last_trade_date | 2024-03-11 | 2024-01-14 | earlier | SUSPENDED_FREEZE | NEW_CORRECTION | BEFORE 最后交易日=2024-03-11（停牌期间仍发生伪交易），AFTER 最后交易日 2024-01-14（停牌前一交易日），属于正确修正。/ BEFORE last trade 2024-03-11 had fake fills during suspension; AFTER correctly shows last trade on 2024-01-14 (day before suspension). Correct. |
| 10 | 3 | 13 | 11 | 2024-04-08 | position_qty | 0.00 | 600.00 | +600.00 | — | SUSPICIOUS_DIFF | AFTER 持仓意外增加 600 股且不在清算/正常买入候选内，无法用现有规则解释 — **疑似错误，需人工复核对应 run_id=13 的撮合日志**。/ AFTER position +600 unexplained by any rule; this **cannot be reconciled by known filters — SUSPICIOUS, require human review of match_engine log for run_id=13.** |

> 完整差异清单 / Full ledger: 请查看本报告对应输出目录中的 `diffs.csv`（由 `scripts/backtest_filter_dual_run_compare.py --out-csv` 生成）。
> For the complete list please refer to the `diffs.csv` next to this report.

---

## 3. 三方签署页 / Tri-party Sign-Off Page

签署占位符格式与 Task 36 要求一致：Name / Role / Signature (/s/) / Date / Notes。
Placeholders use the required format and contain no real person names.

### 3.1 产品方 / Product Owner
| Field / 字段 | Value / 值 |
|---|---|
| Name (姓名) | `[PRODUCT_NAME_PLACEHOLDER]` — 产品负责人 / Product Lead |
| Role (角色) | 产品 Owner：需求完整性、合规阈值定义负责。/ Product side: owns requirement correctness and threshold definitions. |
| Signature (签名) | `/s/ PRODUCT_SIG_PLACEHOLDER` |
| Date (日期) | `____-__-__` (YYYY-MM-DD) |
| Notes (备注) | — |

### 3.2 数据治理方 / Data Governance
| Field / 字段 | Value / 值 |
|---|---|
| Name (姓名) | `[DATA_GOVERNANCE_NAME_PLACEHOLDER]` — 数据治理负责人 / Data Governance Lead |
| Role (角色) | 对账真实性、排除规则与 SecurityStatus PIT 口径一致性负责。/ Data side: owns reconciliation truth, PIT rule alignment and exclusion-rule-to-PIT mapping correctness. |
| Signature (签名) | `/s/ DATA_GOVERNANCE_SIG_PLACEHOLDER` |
| Date (日期) | `____-__-__` (YYYY-MM-DD) |
| Notes (备注) | 请附：tmp/bfg_backend_cross_domain_*.csv、tmp/bfg_frontend_cross_domain_*.csv 两份跨域审计输出。/ Attach: both cross-domain audit CSV outputs. |

### 3.3 量化开发方 / Quant Engineering
| Field / 字段 | Value / 值 |
|---|---|
| Name (姓名) | `[QUANT_DEV_NAME_PLACEHOLDER]` — 量化开发负责人 / Quant Engineering Lead |
| Role (角色) | 工程实现：BFG engine / freeze / liquidation / bridge 接入、性能基线、反腐硬门交付与结果确认。/ Engineering: BFG engine/freeze/liquidation/bridge integration delivery plus performance baseline & anti-corruption gates. |
| Signature (签名) | `/s/ QUANT_DEV_SIG_PLACEHOLDER` |
| Date (日期) | `____-__-__` (YYYY-MM-DD) |
| Notes (备注) | 请附：`perf_report.json`（性能基线）、`22/22 assertions passed` smoke、`VERDICT 9/9` proof。/ Attach: perf baseline json, 22/22 smoke line, VERDICT 9/9 line. |

---

### End of Report / 报告结束

**Next Action / 下一步**: 三方于 **DRAFT → FINAL 截止日期前** 完成签署并上传签名扫描件。
All three parties please sign above before the DRAFT→FINAL cutoff date.
