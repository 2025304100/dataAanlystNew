"""M08.1 independent-expectation cases: boundary, mode isolation and range edges.

These cases are written from the specification text (交接文档第 8 节 / 主方案
14.3、14.7、14.12.3), not from reading the implementation's control flow: every
assertion states the result the contract requires, so a passing suite means the
implementation matches the document instead of matching itself. They reuse the
synthetic-but-hash-real fixtures of ``test_as_of_and_snapshots`` (candidate world,
real FactWriter provenance, real published manifest) so nothing here bypasses the
formal read path: only ``DataAccessService.read_as_of`` is exercised.

Coverage gaps closed here (none of them exists in the main file):
  * as_of exactly at snapshot.published_at is inclusive, one second later is not;
  * a system-only correction never rewrites the public timeline (G04 symmetry);
  * announcement authority selectors: per-key missing, case sensitivity;
  * adj factor sets are mode-scoped and must never be reused across modes;
  * repeated reads are byte-identical and missing order is deterministic;
  * event_range is left-closed/right-open against the fact's own event_date;
  * an upper-case 64-hex snapshot_id is refused;
  * a non-trading-day window fabricates no slot and no missing row;
  * index and instrument ID namespaces are not interchangeable;
  * the announcement selector shape is strict;
  * every non-consumable source state (legacy/claimed/recovery_pending/conflict)
    is blocked with its own detail;
  * one long-lived service instance keeps snapshots apart (no cross-snapshot
    fact-set reuse);
  * the adj factor set is the latest one frozen at or before the knowledge
    cutoff, never a later one.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import update

from app.db import governance_schema as s
from app.services.governance_data_access import DataAccessError, DataAccessService
from app.services.governance_fact_contracts import canonical
from tests.data_governance.test_as_of_and_snapshots import (  # noqa: F401  (fixture reuse)
    DAILY_REQUIRED, NOW, all_profiles, basic, fin, fresh, instruments, lab, notice_bytes,
    read_request, reasons,
)
from tests.test_governance_master_data import annual_page  # noqa: F401  (fixture dependency of lab)
from tests.test_governance_schema import candidate  # noqa: F401  (fixture dependency of lab)

pytestmark = pytest.mark.whitebox

ANNOUNCE_REQUIRED = ['authority', 'announcement_id', 'content_hash']


# ------------------------------------------------------------------ 1. as_of edge
def test_as_of_at_publish_instant_is_inclusive_and_the_snapshot_stays_replayable(lab):
    """8.2/14.3.2 + R08: publication is not a knowledge ceiling for a fixed snapshot.

    as_of is a knowledge cutoff only; a published snapshot remains replayable after its
    publication (the candidate set stays bounded by the snapshot, never by current).
    """
    snapshot, _ = basic(lab)  # published_at = 2026-05-01T01:00Z == 2026-05-01T09:00+08

    at_instant = fresh(lab).read_as_of(fin(lab, snapshot, '2026-05-01T09:00:00+08:00'))
    assert [row['net_profit_parent'] for row in at_instant['rows']] == ['100.00000000']
    assert at_instant['missing'] == []
    assert at_instant['as_of'] == '2026-05-01T09:00:00+08:00'
    assert at_instant['snapshot_id'] == snapshot

    one_second_later = fresh(lab).read_as_of(fin(lab, snapshot, '2026-05-01T09:00:01+08:00'))
    assert canonical(one_second_later['rows']) == canonical(at_instant['rows'])
    much_later = fresh(lab).read_as_of(fin(lab, snapshot, '2026-06-30T15:00:00+08:00'))
    assert canonical(much_later['rows']) == canonical(at_instant['rows'])

    # 同一时刻用 UTC 表达必须等价，时区写法不改变边界判定。
    utc_instant = fresh(lab).read_as_of(fin(lab, snapshot, '2026-05-01T01:00:00Z'))
    assert canonical(utc_instant['rows']) == canonical(at_instant['rows'])


# ------------------------------- 2. public mode is bounded by public evidence only
def test_public_mode_keeps_the_latest_publicly_known_version_when_a_correction_is_system_only(lab):
    """8.2-2/14.3.2-3 + G04：public 取“当时可公开已知的最新版本”。

    V3 是一次只有系统认可、没有公开内容证据的更正：公开侧从未看到它，因此公开现状仍是 V1。
    这与 G04 的 04-30 情形同构（V2 尚未公开时 public=100/system=100），也是 14.3.2 明确禁止
    “按 today.is_obsolete 过滤”的另一面——不能用后来才知道的更正把公开历史改成 NOT_KNOWN。
    """
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    first = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00', revision_order=1)
    lab.at(datetime(2026, 6, 2, 1, tzinfo=timezone.utc))
    correction = lab.financial('2026-03-31', '80', published_at=None, precision='unknown',
                               revision_order=2, supersedes=first, acceptance_event='T09', revision=1)
    snapshot = lab.publish({'financial': [first, correction]}, profiles=all_profiles(),
                           created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1))

    system = fresh(lab).read_as_of(fin(lab, snapshot, '2026-06-02T12:00:00+08:00', 'system_known'))
    assert [row['net_profit_parent'] for row in system['rows']] == ['80.00000000']
    assert system['rows'][0]['data_version'] == correction

    public = fresh(lab).read_as_of(fin(lab, snapshot, '2026-06-02T12:00:00+08:00'))
    assert [row['net_profit_parent'] for row in public['rows']] == ['100.00000000']
    assert public['rows'][0]['data_version'] == first
    assert public['missing'] == []

    # 更早的公开时点同样只看当时已知的版本，不因后来的系统更正而变成 NOT_KNOWN。
    earlier = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert [row['net_profit_parent'] for row in earlier['rows']] == ['100.00000000']


# --------------------------------------------- 3. announcement authority selector
def test_announcement_authority_selector_reports_per_key_missing(lab):
    """14.12.3：已注册 authority 在该快照无事实 → 逐键 missing，不静默过滤、不换别的键。"""
    lab.at(datetime(2026, 6, 1, 8, tzinfo=timezone.utc))
    notice = lab.announcement('A-QA-1', event_day='2026-06-01', published_at='2026-06-01T16:00:00+08:00')
    snapshot = lab.publish({'announcement': [notice]}, profiles=all_profiles(),
                           created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1))

    def read(authority, announcement_ids):
        return fresh(lab).read_as_of(read_request(
            'announcement',
            selector={'kind': 'announcement', 'authority': authority, 'announcement_ids': announcement_ids},
            start='2026-06-01', end='2026-06-02', as_of='2026-06-02T09:00:00+08:00', mode='system_known',
            snapshot=snapshot, profile='announcement/core', required=ANNOUNCE_REQUIRED))

    mixed = read('SSE', ['A-QA-1', 'A-QA-404'])
    assert [row['announcement_id'] for row in mixed['rows']] == ['A-QA-1']
    assert [row['authority'] for row in mixed['rows']] == ['SSE']
    assert [(item['entity_key'], item['reason'], item['detail']) for item in mixed['missing']] == [
        ('SSE:A-QA-404', 'NOT_INGESTED', 'NO_VERSION_IN_SNAPSHOT')]
    assert 'event_date' not in mixed['missing'][0]

    # authority 不归一化：不同大小写不匹配库内 'SSE'，必须逐键 missing 而不是命中已知事实。
    lower = read('sse', ['A-QA-1'])
    assert lower['rows'] == []
    assert [(item['entity_key'], item['reason'], item['detail']) for item in lower['missing']] == [
        ('sse:A-QA-1', 'NOT_INGESTED', 'NO_VERSION_IN_SNAPSHOT')]

    # 未注册 authority 同样按逐键 missing 返回，不静默当成全部。
    unknown = read('SZSE', ['A-QA-1'])
    assert unknown['rows'] == []
    assert [(item['entity_key'], item['reason'], item['detail']) for item in unknown['missing']] == [
        ('SZSE:A-QA-1', 'NOT_INGESTED', 'NO_VERSION_IN_SNAPSHOT')]


@pytest.mark.parametrize('selector,detail', [
    ({'kind': 'announcement', 'authority': 'SSE', 'announcement_ids': []}, 'ANNOUNCEMENT_ID_INVALID'),
    ({'kind': 'announcement', 'authority': 'SSE', 'announcement_ids': ['A', 'A']},
     'ANNOUNCEMENT_ID_INVALID_DUPLICATE'),
    ({'kind': 'announcement', 'authority': 123, 'announcement_ids': ['A']}, 'AUTHORITY_INVALID'),
    ({'kind': 'announcement', 'authority': '  ', 'announcement_ids': ['A']}, 'AUTHORITY_INVALID'),
    ({'kind': 'announcement', 'announcement_ids': ['A']}, 'SELECTOR_FIELDS'),
    ({'kind': 'macro', 'indicator_keys': ['cn_cpi_yoy']}, 'SELECTOR_DATASET_MISMATCH'),
])
def test_announcement_selector_shape_is_strict(lab, selector, detail):
    """8.1/14.12.3：公告两种 selector 的键域严格 oneOf，空列表与重复 ID 都是请求错误。"""
    snapshot, _ = basic(lab)
    request = read_request('announcement', selector=selector, start='2026-06-01', end='2026-06-02',
                           as_of='2026-04-30T15:00:00+08:00', mode='system_known', snapshot=snapshot,
                           profile='announcement/core', required=ANNOUNCE_REQUIRED)
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(request)
    assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.status == 400
    assert exc.value.detail == detail


def test_index_and_instrument_namespaces_are_not_interchangeable(lab):
    """14.12.3：index 为其真实命名空间字符串，不能把 index_id 当股票 ID，也不能反过来。"""
    snapshot, _ = basic(lab)
    as_index = read_request('index', selector={'kind': 'index', 'index_ids': [600001]}, start='2026-01-01',
                            end='2026-02-01', as_of='2026-04-30T15:00:00+08:00', mode='system_known',
                            snapshot=snapshot, profile='index/core',
                            required=['index_open', 'index_high', 'index_low', 'index_close', 'index_id'])
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(as_index)
    assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.detail == 'INDEX_ID_INVALID'

    as_instrument = fin(lab, snapshot, '2026-04-30T15:00:00+08:00', selector=instruments(['CSI:000300']))
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(as_instrument)
    assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.detail == 'INSTRUMENT_ID_INVALID'

    # 未知品种的 selector kind 也必须是请求错误：kind 本身不在合同枚举里（R10 先校验类型/枚举）。
    wrong_kind = fin(lab, snapshot, '2026-04-30T15:00:00+08:00',
                     selector={'kind': 'indicators', 'indicator_keys': ['cn_cpi_yoy']})
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(wrong_kind)
    assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.detail == 'SELECTOR_INVALID'


# ------------------------------------------------ 4. adj factor sets are mode-scoped
def test_public_adjustment_never_reuses_a_system_known_factor_set(lab):
    """8.2-5/14.12.4：因子集按其知识模式与知识截止冻结，跨模式复用等于用未来信息造历史价格。"""
    from app.services.governance_prices import PriceService, install_price_schema
    install_price_schema(lab.engine)
    lab.at(datetime(2026, 6, 2, 8, tzinfo=timezone.utc))
    version = lab.daily('2026-06-02', published_at='2026-06-02T16:00:00+08:00')
    lab.at(datetime(2026, 6, 2, 9, 30, tzinfo=timezone.utc))
    prices = PriceService(lab.writer)
    spec = {'instrument_id': lab.identity, 'instrument_version': lab.instrument_version,
            'calendar_version': lab.calendar_version, 'base_date': '2026-06-02',
            'as_of': '2026-06-02T09:00:00Z', 'knowledge_mode': 'system_known',
            'algorithm_version': 'r1-qfq-v1', 'raw_data_versions': [version], 'event_inputs': []}
    factor = prices.freeze_factors(spec, evidence=b'SYNTHETIC M08.1 QA mode-scoped factor set')
    prices.derive(version, factor)
    snapshot = lab.publish({'daily_bars': [version]}, profiles=all_profiles(),
                           created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1),
                           dependencies=lab.dependencies(adjustment_sets=[factor]))

    def read(mode):
        return fresh(lab).read_as_of(read_request(
            'daily_bars', selector=instruments([lab.identity]), start='2026-06-02', end='2026-06-03',
            as_of='2026-06-02T09:00:00Z', mode=mode, snapshot=snapshot, profile='daily_bars/core',
            required=DAILY_REQUIRED, price_basis='adj', base_date='2026-06-02'))

    public = read('public_known')
    assert public['rows'] == []
    assert len(public['missing']) == 1
    blocked = public['missing'][0]
    assert (blocked['entity_key'], blocked['event_date'], blocked['reason'], blocked['detail']) == (
        lab.identity, '2026-06-02', 'ADJUSTMENT_UNAVAILABLE', 'ADJUSTMENT_FACTOR_SET_MISMATCH')

    # 同一请求换成因子集自己的知识模式就必须可读：证明拒绝是模式隔离，不是因子/派生缺失。
    system = read('system_known')
    assert [row['adj_close'] for row in system['rows']] == ['10.00000000']
    assert system['rows'][0]['adj_factor_version'] == factor
    assert system['rows'][0]['price_basis'] == 'adj' and system['rows'][0]['base_date'] == '2026-06-02'
    assert system['missing'] == []


# ------------------------------------------------- 5. repeatability / missing order
def test_repeated_reads_are_identical_and_missing_order_is_stable(lab):
    """8.5 一致性：重复读取结果完全相等，逐键 missing 顺序稳定（不依赖插入顺序）。"""
    lab.at(datetime(2026, 6, 1, 8, tzinfo=timezone.utc))
    version = lab.daily('2026-06-01', published_at='2026-06-01T16:00:00+08:00')
    outsider = lab.master.register_identity('authority:m081-qa-outsider', asset_type='stock', exchange='SSE')
    snapshot = lab.publish({'daily_bars': [version]}, profiles=all_profiles(),
                           created_at='2026-06-04T00:00:00.000000Z', published_at=datetime(2026, 6, 4, 2))
    request = read_request('daily_bars', selector=instruments([lab.identity, outsider]),
                           start='2026-06-01', end='2026-06-04', as_of='2026-06-04T09:00:00+08:00',
                           mode='system_known', snapshot=snapshot, profile='daily_bars/core',
                           required=DAILY_REQUIRED)

    results = [fresh(lab).read_as_of(request) for _ in range(3)]
    assert canonical(results[0]) == canonical(results[1]) == canonical(results[2])

    first = results[0]
    assert [row['event_date'] for row in first['rows']] == ['2026-06-01']
    missing = first['missing']
    assert [(item['reason'], item['detail'], item.get('event_date')) for item in missing] == [
        ('NOT_INGESTED', 'MISSING_TRADING_DAY', '2026-06-02'),
        ('NOT_INGESTED', 'MISSING_TRADING_DAY', '2026-06-03'),
        ('NOT_INGESTED', 'INSTRUMENT_NOT_IN_SNAPSHOT', None),
    ]
    # 顺序由 (entity_key 文本, event_date, reason) 决定，三次读取一致且有序。
    assert missing == sorted(missing, key=lambda item: (str(item['entity_key']),
                                                        item.get('event_date') or '', item['reason']))


# ----------------------------------------------------------- 6. event_range edges
def test_event_range_is_left_closed_and_right_open_on_event_date(lab):
    """8.1/14.7-G19：事件区间 [start,end_exclusive)，按事实自身的 event_date 判定。"""
    lab.at(datetime(2026, 7, 2, 2, tzinfo=timezone.utc))
    q1 = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00', revision_order=1)
    half = lab.financial('2026-06-30', '120', report_type='half_year', published_at='2026-07-02T10:00:00+08:00')
    lab.at(datetime(2026, 7, 5, 2, tzinfo=timezone.utc))
    snapshot = lab.publish({'financial': [q1, half]}, profiles=all_profiles(),
                           created_at='2026-07-10T00:00:00.000000Z', published_at=datetime(2026, 7, 10, 6))

    def read(start, end):
        request = fin(lab, snapshot, '2026-07-10T12:00:00+08:00', 'system_known')
        request['event_range'] = {'start': start, 'end_exclusive': end}
        return fresh(lab).read_as_of(request)

    # 上界事件日期恰好等于 end_exclusive → 排除，且不产生 missing（不是缺期）。
    right_open = read('2026-03-31', '2026-06-30')
    assert [row['event_date'] for row in right_open['rows']] == ['2026-03-31']
    assert right_open['missing'] == []

    # 下界事件日期恰好等于 start → 包含。
    left_closed = read('2026-06-30', '2026-07-01')
    assert [row['event_date'] for row in left_closed['rows']] == ['2026-06-30']
    assert [row['net_profit_parent'] for row in left_closed['rows']] == ['120.00000000']
    assert left_closed['missing'] == []

    # 覆盖两期时按 event_date 升序，且不缺期、不压缩。
    both = read('2026-03-31', '2026-07-01')
    assert [(row['event_date'], row['net_profit_parent']) for row in both['rows']] == [
        ('2026-03-31', '100.00000000'), ('2026-06-30', '120.00000000')]
    assert both['missing'] == []


# --------------------------------------------------------------- 7. snapshot_id
def test_uppercase_snapshot_id_is_refused(lab):
    """8.1/14.12.2：版本身份是规范小写 SHA-256 十六进制，大小写不同不是同一个快照。"""
    snapshot, _ = basic(lab)
    assert len(snapshot) == 64 and snapshot.upper() != snapshot
    request = fin(lab, snapshot, '2026-04-30T15:00:00+08:00')
    request['snapshot_id'] = snapshot.upper()
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(request)
    assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.status == 400
    assert exc.value.detail == 'SNAPSHOT_ID_INVALID'


# ------------------------------------------------------- 8. non-trading-day window
def test_non_trading_day_window_has_no_rows_and_no_missing(lab):
    """8.2-6/14.3.2-5：只保留真实缺失的交易日；窗口内没有交易日就没有槽位，更不该编造缺失。

    回归守卫：区间内交易日数为 0 与“标的生命周期不在区间内”是两件事，前者必须返回空 missing。
    """
    lab.at(datetime(2026, 6, 5, 8, tzinfo=timezone.utc))
    snapshot = lab.publish({'daily_bars': []}, profiles=all_profiles(),
                           created_at='2026-06-08T00:00:00.000000Z', published_at=datetime(2026, 6, 8, 2))

    def read(start, end):
        return fresh(lab).read_as_of(read_request(
            'daily_bars', selector=instruments([lab.identity]), start=start, end=end,
            as_of='2026-06-08T09:00:00+08:00', mode='system_known', snapshot=snapshot,
            profile='daily_bars/core', required=DAILY_REQUIRED))

    # 2026-06-06 是周六：区间只有 1 天且非交易日 → 无行、无槽位、无 missing。
    weekend = read('2026-06-06', '2026-06-07')
    assert weekend['rows'] == []
    assert weekend['missing'] == []

    # 对照：同样是空快照，交易日窗口必须留出逐日 missing（证明上一条不是“永远为空”）。
    friday = read('2026-06-05', '2026-06-06')
    assert friday['rows'] == []
    assert [(item['entity_key'], item['event_date'], item['reason'], item['detail'])
            for item in friday['missing']] == [
        (lab.identity, '2026-06-05', 'NOT_INGESTED', 'MISSING_TRADING_DAY')]


# ------------------------------------------- 10. snapshot isolation per instance
def test_one_service_instance_keeps_snapshots_apart(lab):
    """8.2-1/14.4/G12：快照是读取的唯一边界；同一应用服务实例连续读两个快照不得串味。

    回归点：事实行缓存必须以 (snapshot_id, dataset) 为键。已实测（按 dataset 为键的旧修订）：
    先读只含 V1 的 S1 再读含 V1/V2 的 S2，S2 会返回 rows=[] 并报 NO_VERSION_IN_SNAPSHOT，
    即“快照成员里有、但缓存里没有”的版本被静默丢掉。两个方向都保留断言，防止回归。
    """
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    v1 = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00', revision_order=1)
    s1 = lab.publish({'financial': [v1]}, profiles=all_profiles(),
                     created_at='2026-05-01T00:00:00.000000Z', published_at=datetime(2026, 5, 1, 1))
    lab.at(datetime(2026, 6, 2, 1, tzinfo=timezone.utc))
    v2 = lab.financial('2026-03-31', '80', published_at='2026-06-01T10:00:00+08:00',
                       revision_order=2, supersedes=v1, acceptance_event='T09', revision=1)
    s2 = lab.publish({'financial': [v1, v2]}, profiles=all_profiles(),
                     created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1))
    old_as_of, new_as_of = '2026-04-30T15:00:00+08:00', '2026-06-02T12:00:00+08:00'

    # 方向一：先旧后新 —— S2 必须给出自己成员集里的更正版本。
    forward = DataAccessService(lab.engine, clock=lambda: NOW)
    old = forward.read_as_of(fin(lab, s1, old_as_of, 'system_known'))
    assert [(row['data_version'], row['net_profit_parent']) for row in old['rows']] == [(v1, '100.00000000')]
    new = forward.read_as_of(fin(lab, s2, new_as_of, 'system_known'))
    assert [(row['data_version'], row['net_profit_parent']) for row in new['rows']] == [(v2, '80.00000000')]

    # 方向二：先新后旧 —— S1 不能看到 S1 之外的 V2。
    backward = DataAccessService(lab.engine, clock=lambda: NOW)
    assert [row['net_profit_parent'] for row in backward.read_as_of(
        fin(lab, s2, new_as_of, 'system_known'))['rows']] == ['80.00000000']
    replayed = backward.read_as_of(fin(lab, s1, old_as_of, 'system_known'))
    assert [(row['data_version'], row['net_profit_parent']) for row in replayed['rows']] == [(v1, '100.00000000')]

# --------------------------------------------------- 11. non-consumable source states
@pytest.mark.parametrize('state', ['legacy', 'claimed', 'recovery_pending', 'conflict'])
def test_every_non_consumable_source_state_is_blocked_with_its_own_detail(lab, state):
    """14.12.4：quarantined 与 legacy/claimed/conflict 候选都不可消费，且必须给出确切状态。

    R01：状态是快照冻结的认可历史的一部分，因此在发布前设定；发布后改动属于依赖失败。
    """
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    version = lab.financial('2026-03-31', '100', published_at='2026-04-20T10:00:00+08:00')
    with lab.engine.begin() as db:
        db.execute(update(s.acceptance_history).where(s.acceptance_history.c.data_version == version)
                   .values(source_state=state))
    snapshot = lab.publish({'financial': [version]}, profiles=all_profiles(),
                           created_at='2026-05-01T00:00:00.000000Z', published_at=datetime(2026, 5, 1, 1))
    result = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert result['rows'] == []
    assert [(item['reason'], item['detail']) for item in result['missing']] == [
        ('QUALITY_BLOCKED', 'SOURCE_STATE_NOT_CONSUMABLE:' + state)]
    assert result['missing'][0]['entity_key'] == lab.identity
    assert result['missing'][0]['event_date'] == '2026-03-31'


# --------------------------- 13. relation fallback must not fabricate announcements
def test_derived_announcement_relation_never_serves_a_non_member_announcement(lab):
    """8.1/14.12.3：缺扩展键时按成员事实重建关联，但重建出的公告必须是快照成员才可读。

    share_capital 成员事实声明了 A-QA-99，而该公告不在快照的 announcement 成员里：
    不能因为“有关联声明”就把一个非成员公告当真，必须逐键 missing。
    """
    lab.at(datetime(2026, 6, 1, 8, tzinfo=timezone.utc))
    notice = lab.announcement('A-QA-99', event_day='2026-06-01', published_at='2026-06-01T16:00:00+08:00')
    capital = lab.share_capital('2026-06-01', notice_version=notice, announcement_id='A-QA-99',
                               notice_raw=notice_bytes('A-QA-99', '2026-06-01'),
                               published_at='2026-06-01T16:00:00+08:00')
    other = lab.master.register_identity('authority:m081-qa-unrelated', asset_type='stock', exchange='SSE')

    def drop_extension(document):
        document['datasets']['announcement'].pop('instrument_links')
        return document

    # announcement 数据集成员为空：声明关系在，但公告事实不在快照里。
    snapshot = lab.publish({'announcement': [], 'share_capital': [capital]}, profiles=all_profiles(),
                           created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1),
                           instrument_links={str(lab.identity): ['A-QA-99']}, mutate=drop_extension)

    def read(instrument_id):
        return fresh(lab).read_as_of(read_request(
            'announcement', selector=instruments([instrument_id]), start='2026-06-01', end='2026-06-02',
            as_of='2026-06-02T09:00:00+08:00', mode='system_known', snapshot=snapshot,
            profile='announcement/core', required=ANNOUNCE_REQUIRED))

    declared = read(lab.identity)
    assert declared['rows'] == []
    assert [(item['entity_key'], item['reason'], item['detail']) for item in declared['missing']] == [
        (lab.identity, 'NOT_INGESTED', 'NO_ANNOUNCEMENT_IN_RANGE')]

    unrelated = read(other)
    assert unrelated['rows'] == []
    assert [(item['entity_key'], item['reason'], item['detail']) for item in unrelated['missing']] == [
        (other, 'NOT_INGESTED', 'NO_ANNOUNCEMENT_LINK')]


# -------------------------------------- 12. factor set is pinned by knowledge time
def test_factor_set_is_the_latest_one_frozen_at_or_before_the_cutoff(lab):
    """14.3.2-5/14.7-G02：adj 只能用“不晚于知识截止”的因子集，未来冻结的因子集必须被排除。

    快照里同时绑定两套 system_known 因子集（09:00Z 与 11:00Z）。09:00Z 的读取必须用 09:00Z
    那套（11:00Z 那套此刻还不存在）；11:00Z 的读取必须用 11:00Z 那套。响应里的
    ``adj_factor_version`` 是调用方自查“用了哪套”的唯一凭据，必须精确等于对应版本。
    """
    from app.services.governance_prices import PriceService, install_price_schema
    install_price_schema(lab.engine)
    lab.at(datetime(2026, 6, 2, 8, tzinfo=timezone.utc))
    version = lab.daily('2026-06-02', published_at='2026-06-02T16:00:00+08:00')
    lab.at(datetime(2026, 6, 2, 9, 30, tzinfo=timezone.utc))
    prices = PriceService(lab.writer)
    base_spec = {'instrument_id': lab.identity, 'instrument_version': lab.instrument_version,
                 'calendar_version': lab.calendar_version, 'base_date': '2026-06-02',
                 'knowledge_mode': 'system_known', 'algorithm_version': 'r1-qfq-v1',
                 'raw_data_versions': [version], 'event_inputs': []}
    early = prices.freeze_factors({**base_spec, 'as_of': '2026-06-02T09:00:00Z'},
                                  evidence=b'SYNTHETIC factor set frozen at 09:00Z')
    prices.derive(version, early)
    # 因子集只能在其 as_of 之后被冻结，所以先把写入时钟推到 11:30Z 再冻结第二套。
    lab.at(datetime(2026, 6, 2, 11, 30, tzinfo=timezone.utc))
    late = prices.freeze_factors({**base_spec, 'as_of': '2026-06-02T11:00:00Z'},
                                 evidence=b'SYNTHETIC factor set frozen at 11:00Z')
    prices.derive(version, late)
    assert early != late
    snapshot = lab.publish({'daily_bars': [version]}, profiles=all_profiles(),
                           created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1),
                           dependencies=lab.dependencies(adjustment_sets=[early, late]))

    def read(as_of):
        return fresh(lab).read_as_of(read_request(
            'daily_bars', selector=instruments([lab.identity]), start='2026-06-02', end='2026-06-03',
            as_of=as_of, mode='system_known', snapshot=snapshot, profile='daily_bars/core',
            required=DAILY_REQUIRED, price_basis='adj', base_date='2026-06-02'))

    before_late = read('2026-06-02T09:00:00Z')
    assert [row['adj_factor_version'] for row in before_late['rows']] == [early]
    assert before_late['rows'][0]['adj_close'] == '10.00000000'
    assert before_late['missing'] == []

    at_late = read('2026-06-02T11:00:00Z')
    assert [row['adj_factor_version'] for row in at_late['rows']] == [late]
    assert at_late['missing'] == []
