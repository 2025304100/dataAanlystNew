"""Freeze today's bounded maintenance evidence in a new audit directory."""
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.stdout.reconfigure(encoding='utf-8')
source = Path(__file__).parent
repo = source.resolve().parents[1]
destination = repo / 'docs/audits/portfolio-inspection-2026-10-24/main-score-and-blocker-review'
if destination.exists():
    raise SystemExit('Refusing to overwrite a frozen evidence directory')
destination.mkdir(parents=True)
evidence = destination / 'evidence'
evidence.mkdir()
for path in sorted(source.rglob('*')):
    if not path.is_file() or '__pycache__' in path.parts:
        continue
    relative = path.relative_to(source)
    target = evidence / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
references = evidence / 'reference-scripts'
references.mkdir()
for label, path in {
    'protected-table-capture.py': repo / 'tmp/inspection-etf-fallback-2026-10-24/capture_safety.py',
    'read-only-candidate-review.py': repo / 'tmp/main-history-investigation/read_candidate_status.py',
}.items():
    shutil.copyfile(path, references / label)

def read(name):
    return json.loads((source / name).read_text(encoding='utf-8'))

safety = read('final-safety-comparison.json')
assert safety['passed']
preflight = read('preflight.json')
results = read('rechecks/summary.json')
assert len(results) == 14
assert all(not row['can_confirm'] and row['supplement_candidates'] == 0 and row['history_relation'] == 'matched' for row in results)

overview = '''# main 剩余阻断：安全评分与重检验收

核查日期：2026-10-24（北京时间），目标评分交易日为最近闭市日 **2026-10-23**。对象为本机实际 MySQL / main，不使用原型占位数据。

## 本轮已完成

| 项目 | 实际结果 | 证据 |
|---|---|---|
| 510300 现行规则评分 | 按原活动配置“ETF均衡机会”v1，新增 10-23 评分：质量 54.62、时机 57.17、综合 55.56；未覆盖旧评分 | [评分回执](evidence/score-refresh-result.json) |
| 两只股票现行规则评分 | 平安银行保留历史原值，质量56.90、时机65.34、综合60.85；中远海特限定更正08-07一行后，质量48.75、时机55.46、综合52.57 | [股票评分及单日更正回执](evidence/stock-score-refresh-result.json) |
| 历史执行对账 | 14 个队列交易日全部实际重检；仍为证据不完整、不可确认；每份报告都能立即定位相关历史 | [按日结果及缺证边界](history-gap-review.md) |
| 正式模型与策略 | 正式资格接口返回 0 个；列清真实模型资料、可参考的策略草案和需要本人确认的方向 | [模型与策略待确认清单](model-strategy-candidates.md) |
| 保护检查 | 25张业务保护表及main整行前后一致；原有45条评分、评分配置、自动交易任务和派发历史不变；行情仅更正600428/08-07一行 | [完整安全比对](evidence/final-safety-comparison.json) |

**三条都是现行规则评分，不能替代main冻结绑定的正式Ridge模型评分。** 未修改`weight_mode`为Ridge、未伪造模型/因子集引用、未将补算结果标记为历史事前可用。新评分实际发表于10-24，`pit_safety=NOT_PIT_SAFE`，不能用于宣称10-23当时已具备的生产决策证据。正式模型、策略绑定和历史对账阻断继续保留。

## main 当前剩余问题

1. **正式评分/因子链**：已应用 S4 仍绑定“验证测试”演示/来源无法核验模型，以及旧因子集。三只标的对应的正式评分与 `turnover_z20` 依赖未满足；现行规则重算不能替换该冻结输入。
2. **正式策略绑定**：已应用快照是 research/best_effort，引用已失效 rule 1；规则页活动 rule 12 只是保存的另一份配置，不能当成已应用正式策略。
3. **历史执行与账户核对**：09-29 至 10-23 队列内共 14 个交易日没有完整执行证据；连续核验标记仍为 09-28。

两只股票的当前评分输入问题已在不重跑成功范围的前提下处理：平安银行按既有跨来源0.02元价格容差核查后保留历史值；中远海特仅更正两家来源确认的08-07一行。它们的当前规则评分已完成，没有将上述已处理输入继续列为阻断。详见[输入核查及单行更正](stock-input-review.md)。

诊断汇总仍为：3标的，7项通过、3项缺失、4项版本不匹配。当前三个标的日线已齐；股票当前实际滚动输入已按已有来源契约交叉核查，并完成必要的单行更正。当前自动新买入成员范围为空，运行门禁中的数据条件是“不适用”，也不能据此声称持仓和候选的全部正式评分已通过。

后台仍有保留的未消费来源事件；本轮通过受控按日重检取得最新报告，没有把事件清空或历史缺证改成通过。未启用周期作业，也没有触发自动交易。

## 本轮写入范围与验证

- 评分新增三条：`79581`为510300 / 配置5 v1，发布时刻10-24 09:00:01（北京时间），审计3779；`79582`为平安银行、`79583`为中远海特 / 配置3 v1，发布时刻10-24 09:13:35，评分审计3794与3796。原有45条评分逐字段保留。
- 原始行情仅更正600428的2026-08-07一行，审计3795；更正前原值、来源、回执及哈希完整保留。000001和510300全部行情及600428其余290条行情均未变，没有新增或删除日线。
- 14 次核对仅新增报告审计 3780–3793。每次同时读取补证候选和确切报告版本的历史，14/14 关联匹配；候选均为 0，没有提交或审批补证申请。
- ETF评分脚本只允许查询及向`scores`、`data_governance_audit_events` INSERT；股票收尾脚本仅额外允许一次`daily_bars` UPDATE，语句限定到已有主键、600428和08-07，事务内核验仅一行受影响，其他UPDATE/DELETE全部拒绝。两者在重算期间禁止HTTP，冻结配置与80条输入哈希重新核实，并拒绝覆盖已存在评分批次。
- 模型核查SQL仅允许读取；前瞻日志仍为0/50、资格证书未发、模型未启用。离线来源差异计算不接触网络或数据库，最终三条评分的质量/时机/综合分均与离线方案逐值一致。
- 前后25张保护表、基础ETF行情、main整行、评分配置和旧评分逐行一致；日线差异严格只有前述单行更正。自动交易任务仍16、自动派发仍66，无排队/运行任务。审计水位3778→3796，共18条：3条评分、1条行情更正、14条重检。
- 安全验收后端 PID/服务未重启；后台作业、自动交易、交易写入和最终审核持续关闭。未变更交易开关、模型、成员或策略；**没有恢复交易、补单、接受新基线或解冻准入**。风险退出原权限保留，没有实际执行风险退出订单。

本轮未修改生产应用代码或前端资源，因此没有把此前 ETF 接入的 139 项隔离测试、旧页面走查或构建结果重复记作本轮新验证。本轮验证依据为实际有限写入、14 日核对/补证/历史查询、模型只读资格核查、离线评分复现和前后逐行保护对比。

## 与设计和 P0 的对应关系

| 依据 | 本轮核查结论 |
|---|---|
| 功能设计的数据根因诊断、按标的整合与逐对象重检 | 继续区分“行情齐全”“现行规则分数已算”“冻结模型输入未满足”；没有用规则分数消除模型版本阻断 |
| 功能设计 §5.7–5.10 的补证、未知历史、自动对账和基线边界 | 按队列重检 14 日；没有原件便不申请关联、不把未知算零差异；没有接受独立兜底基线 |
| v10 的当前审核 / 历史缺口 / 审计关联 | 当前审核仍不可确认；历史缺口保留三类来源的统一详情；每个新报告可按日期和版本回查，不重新引入逐项登记 |
| 独立 P0 双跑热修清单 | 本轮没有执行双跑、切换旧/新引擎链或解冻；模型候选确认也不代替引擎链真实性验收 |

前次本地备份取证范围继续引用 [10-23 历史恢复核查](../../portfolio-inspection-2026-10-23/main-history-recovery-and-candidates.md)，本轮没有重新导入或还原备份。完整文件哈希见 [manifest.json](manifest.json)。
'''
(destination / 'README.md').write_text(overview, encoding='utf-8')

model_text = '''# main 正式模型与策略候选：需要确认的准备方向

核查日期：2026-10-24（北京时间）。正式资格查询当前实际返回 **0 个模型**；不是已有合格模型等待点选启用。以下名称、ID、资格均来自真实库和冻结文件，没有“模型 A / B”占位数据。

## 现有模型资料

| 模型资料 | 当前判定 | 若继续此方向，还缺什么 |
|---|---|---|
| Ridge 换手率 Z20 v3，`ridge-d2aaa125d6cba57f468d0e44`；31,902 样本 / 128 股 / 250 日 | `pending_validation`，独立资格未完成，不可正式启用 | 新候选/新事先冻结验证周期；原方案 10-17 边界后的前瞻日志仍 0/50，10-19 至 10-23 首批闭市信号日没有保留事前预测，不能事后补录或跳过这些日子继续挑 50 天；费用后独立验证和资格证书也未完成 |
| 旧 warehouse Ridge，`ridge-6e021fca7df840b4be85bec0`；645,085 样本 / 5,526 股 / 120 日 | 标签 validated，但 `FACTOR_SET_NOT_READY`，不可正式启用 | 旧因子集的 `turnover_z20` 版本缺当前有效校验/执行兼容证明；需要有效新版本和新因子集重新准备，不能原地改冻结模型 |
| main S4 当前绑定“验证测试”，`rid-fs-set-3126b70f-2026-08-14-13a60cd83e0e` | offline_minimal，250 样本 / 0 股；来源无法核验，排除正式用途 | 只能保留历史绑定事实，不作为正式候选 |
| “可信128股换手率Z20 · 回溯研究”，`ridge-e561cd9a4b126e0ef589f0d2` | 明确 retrospective / research_only，rejected，排除正式用途 | 仅研究对照，名称和样本数量不赋予正式资格 |
| 其余 7 份模型 | 已 rejected，包含当时 IC 阈值未满足或空数据等原因 | 不提升为可启用候选，全部明细保留在证据 |
| ETF | 当前没有 ETF 训练模型 | 因子集 `etf-price-volume-v1` 不等于模型；现行“ETF均衡机会”是规则配置，不是已验收正式 ETF 模型 |

11 份模型的新鲜资格和数据范围见 [preflight.json](evidence/preflight.json)；冻结候选、前瞻日志、费用政策和未发布状态见 [candidate-review-details.json](evidence/candidate-review-details.json)。原方案 ID 为 `153f0acfa78e482437a65bcc7b19c9a15ccabac2765c801309ccf01c81ba5f40`，执行文件 SHA-256 为 `17b45c8b446fb9dfb8baed809fecf7667126727de1a686b8ec6e36eabe2d5cf4`，没有修改原方案。费用内容仅记为项目原保存配置，未当作已核实的现行市场费率。

## 可参考的策略草案

规则页当前活动规则为 **12 / Ridge流动性因子模型-MACD**；同参数旧规则 11 未激活。保存风险值：单标的 15%、单行业 30%、股票总上限 70%、ETF 总上限 50%、单笔最大损失 1%、最多 8 个持仓。旧 JSON 还保存回撤熔断 10%，以及股票 start/accel/overheat/cooldown 分阶段 8%/15%/5%/0、ETF 10%/20%/8%/0。仅列保存值，没有改动或宣称所有入口已执行这些字段。

该规则仍绑定不可正式使用的旧 Ridge；main 实际已应用的却是 S4 / rule 1 / research/best_effort /“验证测试”。因此规则页的活动标签不等于已经应用正式快照。

“中证500 / MACD / 风险平价 / 周调”是旧配置标签。此前代码核查已发现自动入口的“中证500”是代码/市场启发式过滤，不能证明精确成分及生效日期；没有证据证明该旧入口完整执行了同名 MACD、风险平价、周调分支。模型训练池 129 原始股/128 有效股也不等于精确中证500池。不得直接把这些名称拼成一份可上线策略。

独立活动信号规则 `balanced` 的 quality/timing tolerance=12、最小样本3、最大样本60，各范围限制开启；它不是另一份训练模型。平安银行和中远海特成员模式仍为 manual，ETF 为持仓，当前自动新买入范围为空，没有顺带改为自动成员。

## 请本人确认的范围

| 待确认决策 | 可审阅的具体方向 | 确认后的工作边界 |
|---|---|---|
| 股票模型方向 | 是否继续以换手率 Z20 v3 为新股票候选方向 | 准备新候选与新的前瞻周期，保留旧缺失日志事实；确认方向不等于启用 |
| 策略风险参数 | 是否以 rule 12 上述保存值为草案起点 | 建立正式可执行快照并验证，先不保存应用到 main |
| 股票范围和执行语义 | 当前成员、明确固定股票池或精确中证500；是否确实要求 MACD、风险平价、周调 | 按确定语义实现/核验对应数据和执行契约，不沿用名称推断 |
| ETF 方案 | 独立准备 ETF 模型，或明确采用无模型规则方案 | 单独定义可验证输入和验收，不能默用股票模型；本轮规则分数不代表已选择规则执行方案 |
| 自动执行成员 | 后续具体允许哪些资产和成员自动运行 | 单独审核执行范围，不能因为模型确认就把所有持仓改成自动 |

这些决定仅用于后续准备。当前没有“确认就立即可上线”的现成模型+策略组合。本轮未训练新模型、开启验证周期、修改配置/绑定或启用模型；任何方向确认均不包含交易恢复、补单、新基线接受、G5 引擎链切换或准入解冻。
'''
(destination / 'model-strategy-candidates.md').write_text(model_text, encoding='utf-8')

stock_text = '''# 股票历史输入核查与单日更正验收

本轮目标是 2026-10-23 现行规则评分。两只股票活动配置均为“趋势成长”v1，只启用内置/日K因子，无外部因子调用。实际算法依赖 50 日收盘均线、近20日动量/成交额、最近20个收益率等，核查覆盖了对应原始输入；中远海特更早的6–8月缺口不在当前实际计算窗口内，没有据此额外阻塞。

**本轮没有重新同步已成功的股票范围。仅更正600428的08-07一条已核实旧行情，其他行情保留。** 网络请求只用于来源比对，正式更正和重算期间禁止HTTP；更正前完整原值与哈希留存，按原活动配置新增两只股票10-23的规则评分，没有覆盖过去评分。

## 平安银行 000001：一分钱来源差异，不能先说某一家一定错

新浪严格前复权回执与本地比较，在 09-01 至 09-04 的收盘价相差 0.01 元；腾讯具备 `qfqday` 标记的独立回执在这四天与本地一致。腾讯在 08-07 至 08-21 的11个交易日则与本地相差约 0.01 元。核查到的是不同来源的历史前复权价格差异，现有证据不能直接认定其中一方为错误值或把差异认定为新的复权事件。

部分旧行成交额、成交量还有 float32 留存误差；相关金额日期不在本次近20日金额窗口内，没有将其夸大为当前金额单位错误。

| 离线价格方案 | 质量分 | 时机分 | 综合分 | 是否发布 |
|---|---:|---:|---:|---|
| 保留本地原始50日价格 | 56.90 | 65.34 | 60.85 | 已新增现行规则评分 |
| 最近50日统一新浪前复权价格，其余输入不动 | 56.91 | 65.34 | 60.86 | 否 |
| 最近50日统一腾讯前复权价格，其余输入不动 | 56.92 | 65.34 | 60.86 | 否 |

分数差异很小，三个方案的算法阶段/动作一致。最终依据为项目**已有**的跨来源价格容差`_CROSS_PROVIDER_PRICE_ATOL=0.02`元，未修改该常量或其他放行规则。新浪和腾讯最近50日OHLC相互最大差异0.01元；本地全部相应OHLC也均在既有容差内，最近20日成交额与新浪逐日完全相同。因此保留本地全部原值，完成规则评分，不把合法跨源精度差异再作为待办。最初半个价格档位的严格探测失败记录保留，但它是临时精确匹配探测，不替代项目已存在的跨来源契约；没有仅凭“分数影响小”放行。

## 中远海特 600428：定位到 08-07 的一条旧异常行情

本地 08-07 行来源为旧 `akshare`。从 08-10 起的其余49个当前窗口交易日，收盘价与两家核查来源均一致。08-07 的收盘价却是本地约 **8.09 元**、新浪前复权 **11.95 元**、腾讯前复权 **11.95 元**，不能带着该条旧值发布可靠的新规则评分。

| 字段 | 本地原值 | 新浪前复权原始回执的规范值 |
|---|---:|---:|
| 开盘 | 8.130000114440918 | 11.98 |
| 最高 | 8.25 | 12.13 |
| 最低 | 8.0 | 11.77 |
| 收盘 | 8.09000015258789 | 11.95 |
| 成交量（股） | 319711 | 66880281 |
| 成交额（元） | 260480608 | 798051001 |

腾讯的独立核实用于价格，不把其旧六列中含糊的数量列当成人民币成交额。新浪/腾讯50日OHLC相互差异为0，最近20日人民币成交额与本地差异为0；只有08-07本地旧行超出现有容差。最终**只更正600428的2026-08-07一行**，以经过原单位契约校验的新浪完整行写入OHLC、股数成交量、人民币成交额及有效股本推导的换手率，来源记为`akshare:sina:cn-stock:v1:qfq`，记录腾讯价格交叉证据。事务前锁定并逐字段核对原值，UPDATE同时限定原主键、标的和交易日，确认仅1行受影响，并与新评分和审计原子提交。没有重跑08-08以后的已成功同步范围。

| 离线方案 | 质量分 | 时机分 | 综合分 | 是否发布 |
|---|---:|---:|---:|---|
| 更正前本地旧值（离线留存） | 49.29 | 55.46 | 52.75 | 否 |
| 仅更正08-07一行 | 48.75 | 55.46 | 52.57 | 已新增现行规则评分 |
| 最近50日统一新浪或腾讯价格 | 48.75 | 55.46 | 52.57 | 否 |

单日更正后实际新评分与两家来源统一价格的离线分数完全一致，不需要全历史重跑。只追加当前计算时点的新评分并保留过去评分，没有给旧结果补发历史事前可用证明，也没有因此消除main的模型与策略门禁。平安银行、中远海特新评分ID分别79582、79583，原行情更正审计3795；两条评分审计分别3794、3796。

## 原始证据

- [最终沿用既有契约的核验及更正前后完整行](evidence/stock-inputs-final-proof.json)、[实际单行更正与股票新评分回执](evidence/stock-score-refresh-result.json)、[最终逐行保护对比](evidence/final-safety-comparison.json)。
- [逐字段历史差异](evidence/historical-bar-mismatches.json)、[独立腾讯价格复核](evidence/second-source-review.json)、[完全离线的分数敏感性分析](evidence/score-sensitivity.json)。
- 新浪回执：[000001](evidence/rule-input-receipts/000001/history.response)、[600428](evidence/rule-input-receipts/600428/history.response)；各目录同时保存流通股本和复权因子原始回执。
- 腾讯回执：[000001](evidence/second-source-receipts/000001/qfq.response)、[600428](evidence/second-source-receipts/600428/qfq.response)。
- 原本地输入、完整配置与哈希：[preflight.json](evidence/preflight.json)。所有文件另有清单哈希，未使用实时重取结果覆盖本轮回执。
'''
(destination / 'stock-input-review.md').write_text(stock_text, encoding='utf-8')

day_table = '\n'.join(f"| {row['trade_date']} | {row['audit_event_id']} | {row['dispatch_count']} | 证据不完整 / 0 / 已匹配 |" for row in results)
history_text = '''# main 14 日历史对账：无法补齐的范围与依据

2026-10-24 在安全验收后端重新核对既有队列全部14个交易日，包括此前未重检的10-23。未将09-28标记以前的 `history_only` 记录变为新强制义务，未修改核验范围或门禁。

| 交易日 | 本轮审计 | 调度上下文条数 | 实际结果 / 补证候选 / 报告历史 |
|---|---:|---:|---|
''' + day_table + '''

所有报告均为 `EVIDENCE_INCOMPLETE`、`DECISION_RUN_NOT_FOUND`、`can_confirm=false`。按实际日期读取的订单、成交、流水均没有可用于闭合该日执行链的原件；原订单可关联补证候选均为0，没有填写或批准虚假关联。报告 ID + 版本 + 交易日的历史定位全部匹配，保留了每一份新旧处理记录。

## 三类历史资料分别缺什么

1. **原自动运行、逐标的决策与确切计划链**：14日范围内未找到可核验记录。只保留的派发/任务上下文标记为 `CONTEXT_ONLY`、`proves_execution=false`；旧任务跨日复用、结果后来覆盖，不能按调度成功标签认定当日执行成功。
2. **当时生效的接管/调度/成员执行范围**：缺有明确生效日期的完整历史配置，当前开关不能倒推当时是否应该执行。因此不是已经证实漏跑，也不是已证实当天无需执行。
3. **当日逐标的持仓、账户期初期末和未完成义务**：当前两条持仓和现金汇总不能替代当日账户证据。已有净值汇总没有逐标的明细和不可变来源，也不能补成完整历史快照。

当前完整现金账与成交推导余额均为434,752.27元，差额0；该核对范围是当前完整账本，`historical_asof_certified=false`，没有将其扩展成14日历史资金持仓均已核对。

## 哪些来源已检查，为什么仍无法恢复

本轮只读确认当前库在09-29至10-23范围内没有新增原决策运行或明细，原订单计划仍0。与前次已冻结的备份核查结合，**实际恢复原运行/明细/计划/订单为0条**：

- 10-20完整MySQL备份已逐字节校验SHA-256及gzip CRC、只读解析；可保留原记录均已在当前库，没有可补回的目标日执行链。它本身也不能证明10-21至23发生的事情。
- 10-23迁移前局部备份的23份gzip和76份净值汇总已核验，无可恢复目标日完整执行证据；当前仍保留原汇总。
- 198个本地SQLite候选中70个为空、128个非空候选最终均已只读打开，未找到main；测试库也无main。没有导入测试数据或还原整个数据库。

完整盘点范围、备份哈希和差异留在[10-23已冻结历史取证报告](../../portfolio-inspection-2026-10-23/main-history-recovery-and-candidates.md)。本轮没有重复扫描全部大备份，也没有把旧取证日期说成本轮重新扫描日期。

“无法恢复”限于当前库及已经找到并核查的本地来源；不等于证明从未执行，也不排除尚未提供的外部原始账户回报、离机备份或完整历史快照。若以后取得这些原件，再按目标交易日、真实来源锚点和完整链条核验；不能只按代码/数量/日期相近自动关联。

## 后续处理边界

历史缺口详情和相关历史仍能查看，原缺口及其报告版本继续保留。若外部也确实没有原件，应由独立“历史无法恢复/新覆盖范围”政策作出后续决定；该路径需要明确授权、完整账户核验及独立接受/启用，不能靠备注、跳过日期或当前余额一致清除旧缺证。

**本轮没有接受新基线或启用新覆盖范围，没有把历史记为已确认未执行，没有补单或恢复交易。** 连续核验日期仍为09-28，main仍为对账阻断。周期作业继续关闭，仅执行授权的14次人工触发重检；来源事件保留，不借自动作业顺带放行。

完整结果：[summary.json](evidence/rechecks/summary.json)，各日的`*-report.json`、`*-supplements.json`、`*-history.json`均独立保存；股票评分完成后再次读取的[运行态](evidence/final-views/runtime.json)、[队列](evidence/final-views/queue.json)和[作业状态](evidence/final-views/job.json)也已留存。
'''
(destination / 'history-gap-review.md').write_text(history_text, encoding='utf-8')

files = []
for path in sorted(destination.rglob('*')):
    if path.is_file():
        files.append({'path': path.relative_to(destination).as_posix(), 'bytes': path.stat().st_size,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
manifest = {'scope': 'main safe current-rule score refresh and blocker review', 'local_date': '2026-10-24',
    'file_count': len(files), 'bytes': sum(row['bytes'] for row in files), 'files': files}
(destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
for row in files:
    assert hashlib.sha256((destination / row['path']).read_bytes()).hexdigest() == row['sha256']
print(json.dumps({'path': str(destination), 'files': len(files), 'bytes': manifest['bytes'], 'hashes_verified': True}, ensure_ascii=False))
