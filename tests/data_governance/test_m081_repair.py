"""M08.1 revision 2: development-side regression tests for the review findings R01—R11.

Every case below states the expectation the governance document requires (交接单 8.2 /
主方案 14.3、14.4、14.12.3—4) and is keyed to the review item it locks down, so the
reviewer's own 20 cases are not the only guard against a regression.
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.db import governance_schema as s
from app.services.governance_data_access import DataAccessError, DataAccessService
from app.services.governance_fact_contracts import canonical
from tests.data_governance.test_as_of_and_snapshots import (
    DAILY_REQUIRED, NOW, SHARE_REQUIRED, all_profiles, annual_page, basic, candidate, fin, fresh, instruments,
    lab, notice_bytes, read_request,
)
from tests.test_governance_writer import system  # noqa: F401  (ETF world fixture dependency)

pytestmark = pytest.mark.whitebox


# --------------------------------------------------------------------- R01
def test_r01_later_supersession_keeps_the_frozen_replay(lab):
    """A later correction closes the old acceptance interval; the old snapshot must replay."""
    snapshot, version = basic(lab)
    first = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    lab.at(datetime(2026, 6, 2, 1, tzinfo=timezone.utc))
    lab.financial('2026-03-31', '80', published_at='2026-06-01T10:00:00+08:00', revision_order=2,
                  supersedes=version, acceptance_event='T09', revision=1)
    again = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert canonical(again) == canonical(first)
    assert again['rows'][0]['data_version'] == version


def test_r01_live_provenance_change_is_refused_for_another_dataset(lab):
    """R01 on share_capital: the frozen acceptance hash is consumed, not just compared."""
    lab.at(datetime(2026, 6, 1, 8, tzinfo=timezone.utc))
    notice = lab.announcement('A-R01', event_day='2026-06-01', published_at='2026-06-01T16:00:00+08:00')
    capital = lab.share_capital('2026-06-01', notice_version=notice, announcement_id='A-R01',
                                notice_raw=notice_bytes('A-R01', '2026-06-01'),
                                published_at='2026-06-01T16:00:00+08:00')
    snapshot = lab.publish({'announcement': [notice], 'share_capital': [capital]}, profiles=all_profiles(),
                           created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1),
                           instrument_links={str(lab.identity): ['A-R01']})
    request = read_request('share_capital', selector=instruments([lab.identity]), start='2026-06-01',
                           end='2026-06-02', as_of='2026-06-02T10:00:00Z', mode='system_known',
                           snapshot=snapshot, profile='share_capital/core', required=SHARE_REQUIRED)
    before = fresh(lab).read_as_of(request)
    assert before['rows']
    with lab.engine.begin() as db:
        db.execute(update(s.acceptance_history).where(s.acceptance_history.c.data_version == capital)
                   .values(source_state='backup_temp'))
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(request)
    assert exc.value.code == 'SNAPSHOT_DEPENDENCY_MISSING'
    assert exc.value.detail == 'ACCEPTANCE_HISTORY_MISMATCH:share_capital'


def test_r01_a_cached_instance_refuses_after_a_live_change(lab):
    """One long-lived service instance must not keep serving the pre-change answer."""
    snapshot, version = basic(lab)
    service = DataAccessService(lab.engine, clock=lambda: NOW)
    request = fin(lab, snapshot, '2026-04-30T15:00:00+08:00')
    assert service.read_as_of(request)['rows'][0]['net_profit_parent'] == '100.00000000'
    with lab.engine.begin() as db:
        db.execute(update(s.acceptance_history).where(s.acceptance_history.c.data_version == version)
                   .values(quality_status='quarantined'))
    with pytest.raises(DataAccessError) as exc:
        service.read_as_of(request)
    assert exc.value.code == 'SNAPSHOT_DEPENDENCY_MISSING'


# --------------------------------------------------------------------- R02
def test_r02_complete_bindings_are_readable(lab):
    """A snapshot that binds the members' contract and definition reads normally."""
    snapshot, _ = basic(lab)
    result = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert result['rows'][0]['net_profit_parent'] == '100.00000000'


def test_r02_a_binding_that_does_not_cover_the_member_is_refused(lab):
    """A contract binding no member uses is not a binding: the manifest and the stored
    row agree here, so the refusal has to come from member coverage."""
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    version = lab.financial('2026-03-31', '100')

    def wrong_binding(document):
        document['datasets']['financial']['contract_versions'] = {'f' * 64: 'financial/net_profit'}
        return document

    snapshot = lab.publish({'financial': [version]}, profiles=all_profiles(), mutate=wrong_binding,
                           created_at='2026-05-01T00:00:00.000000Z', published_at=datetime(2026, 5, 1, 1))
    with lab.engine.begin() as db:
        db.execute(update(s.dataset_versions).where(s.dataset_versions.c.dataset == 'financial')
                   .values(contract_versions={'f' * 64: 'financial/net_profit'}))
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert exc.value.code == 'SNAPSHOT_DEPENDENCY_MISSING'
    assert exc.value.detail == 'CONTRACT_NOT_BOUND:financial'


def test_r02_a_definition_binding_that_does_not_cover_the_member_is_refused(lab):
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    version = lab.financial('2026-03-31', '100')

    def wrong_binding(document):
        document['datasets']['financial']['definition_versions'] = ['e' * 64]
        return document

    snapshot = lab.publish({'financial': [version]}, profiles=all_profiles(), mutate=wrong_binding,
                           created_at='2026-05-01T00:00:00.000000Z', published_at=datetime(2026, 5, 1, 1))
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert exc.value.code == 'SNAPSHOT_DEPENDENCY_MISSING'
    assert exc.value.detail == 'DEFINITION_NOT_BOUND:financial'


def test_r02_the_dataset_version_must_be_the_manifest_content_identity(lab):
    """The dataset version is derived from (members, acceptance, build time). Rewriting the
    build time after the entries were computed leaves every hash intact but breaks the
    identity, and the read must refuse instead of serving the members anyway."""
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    version = lab.financial('2026-03-31', '100')

    def forged_build_time(document):
        document['created_at'] = '2026-04-01T00:00:00.000000Z'
        return document

    snapshot = lab.publish({'financial': [version]}, profiles=all_profiles(), mutate=forged_build_time,
                           created_at='2026-05-01T00:00:00.000000Z', published_at=datetime(2026, 5, 1, 1))
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))
    assert exc.value.code == 'SNAPSHOT_NOT_PUBLISHED'
    assert exc.value.detail == 'DATASET_VERSION_IDENTITY_MISMATCH:financial'


def test_r02_the_dataset_version_is_derived_content_not_a_caller_label():
    """Same members and acceptance at a different build time is a different version."""
    from app.services.governance_snapshot_manifest import dataset_identity
    base = dict(dataset='financial', members=['a' * 64], acceptance_history_hash='b' * 64,
                created_at='2026-05-01T00:00:00.000000Z')
    identity = dataset_identity(**base)
    assert identity != 'd' * 64
    assert identity == dataset_identity(**{**base, 'members': frozenset({'a' * 64})})
    assert identity != dataset_identity(**{**base, 'created_at': '2026-05-02T00:00:00.000000Z'})
    assert identity != dataset_identity(**{**base, 'members': ['c' * 64]})
    assert identity != dataset_identity(**{**base, 'acceptance_history_hash': 'e' * 64})


# --------------------------------------------------------------------- R03
def test_r03_the_same_member_becomes_readable_once_its_announcement_is_in_the_snapshot(lab):
    lab.at(datetime(2026, 6, 1, 8, tzinfo=timezone.utc))
    notice = lab.announcement('A-R03', event_day='2026-06-01', published_at='2026-06-01T16:00:00+08:00')
    capital = lab.share_capital('2026-06-01', notice_version=notice, announcement_id='A-R03',
                                notice_raw=notice_bytes('A-R03', '2026-06-01'),
                                published_at='2026-06-01T16:00:00+08:00')
    request = read_request('share_capital', selector=instruments([lab.identity]), start='2026-06-01',
                           end='2026-06-02', as_of='2026-06-02T10:00:00Z', mode='system_known',
                           snapshot=None, profile='share_capital/core', required=SHARE_REQUIRED)
    incomplete = lab.publish({'share_capital': [capital]}, profiles=all_profiles())
    request['snapshot_id'] = incomplete
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(request)
    assert exc.value.code == 'SNAPSHOT_DEPENDENCY_MISSING'
    assert exc.value.detail.startswith('REFERENCE_OUTSIDE_SNAPSHOT:')

    complete = lab.publish({'share_capital': [capital], 'announcement': [notice]}, profiles=all_profiles(),
                           created_at='2026-06-04T00:00:00.000000Z', published_at=datetime(2026, 6, 4, 1),
                           instrument_links={str(lab.identity): ['A-R03']})
    request['snapshot_id'] = complete
    served = fresh(lab).read_as_of(request)
    assert served['rows'][0]['announcement_data_version'] == notice


# --------------------------------------------------------------------- R04
ETF_PRODUCTS = (
    ('nav', 'nav_official'),
    ('shares', 'total_shares'),
    ('iopv', 'iopv'),
    ('size', 'fund_size_cny'),
    ('tracking', 'tracking_error_pct'),
)


def etf_world(system):
    """The reviewer's ETF world: real accepted products, never a synthetic fact row."""
    from app.services.governance_master_data import MasterDataService
    from tests.data_governance import test_etf_profiles as etf
    from tests.data_governance.test_as_of_and_snapshots import Lab
    service, args, clock, contracts, _ = etf.setup(system)
    world = Lab(service.writer.engine, MasterDataService(service.writer.engine), clock,
                args['instrument_id'], args['instrument_version'], args['calendar_version'], contracts)
    return etf, service, args, contracts, world


def etf_members(etf, service, args, contracts, product):
    """Accepted members for one product; the product fact is always the last one."""
    if product in {'nav', 'shares', 'iopv'}:
        return [etf.accept(service, etf.source(service, args, contracts, product, '2'))]
    if product == 'size':
        # R03: a size fact points at its inputs, so the inputs are snapshot members too.
        nav = etf.accept(service, etf.source(service, args, contracts, 'nav', '2'))
        shares = etf.accept(service, etf.source(service, args, contracts, 'shares', '3'))
        return [nav, shares, etf.accept(service, etf.derive(
            service, args, contracts, 'size', nav_data_version=nav, shares_data_version=shares))]
    nav = etf.register(service, etf.series(service, args))
    benchmark = etf.register(service, etf.series(service, args, 'total_return'))
    return [etf.accept(service, etf.derive(service, args, contracts, 'tracking',
                                           nav_return_version=nav, benchmark_return_version=benchmark,
                                           benchmark_id='CSI300-TR'))]


@pytest.mark.parametrize('product,field', ETF_PRODUCTS)
def test_r04_every_etf_product_is_readable_without_a_nonexistent_core(system, product, field):
    """R04: `etf` has no `<dataset>/core` profile; its own product profile is the contract."""
    from app.services.data_contract_profiles import get_profile
    etf, service, args, contracts, world = etf_world(system)
    members = etf_members(etf, service, args, contracts, product)
    snapshot = world.publish({'etf': members}, profiles=all_profiles())
    profile = 'etf/' + product
    result = fresh(world).read_as_of(read_request(
        'etf', selector=instruments([world.identity]), start='2026-06-01', end='2026-06-02',
        as_of='2026-06-03T10:00:00Z', mode='system_known', snapshot=snapshot, profile=profile,
        required=list(get_profile(profile).required_fields)))
    assert [row['data_version'] for row in result['rows']] == [members[-1]], (product, result)
    assert result['rows'][0][field] is not None
    assert result['rows'][0]['quality_status'] == 'complete'
    assert result['rows'][0]['request_quality_status'] == 'complete'


def test_r04_an_etf_row_its_own_profile_cannot_fill_is_still_blocked(system):
    """The fix opens the five products; it does not certify a row the profile cannot fill."""
    etf, service, args, contracts, world = etf_world(system)
    members = etf_members(etf, service, args, contracts, 'nav')
    snapshot = world.publish({'etf': members}, profiles=all_profiles())
    result = fresh(world).read_as_of(read_request(
        'etf', selector=instruments([world.identity]), start='2026-06-01', end='2026-06-02',
        as_of='2026-06-03T10:00:00Z', mode='system_known', snapshot=snapshot, profile='etf/nav',
        required=['nav_official', 'valuation_date', 'currency', 'premium_official_pct']))
    assert result['rows'] == []
    assert [(item['reason'], item['detail']) for item in result['missing']] == [
        ('QUALITY_BLOCKED', 'REQUEST_FIELD_MISSING:premium_official_pct')]


# --------------------------------------------------------------------- R05
def test_r05_a_present_extra_required_field_stays_readable(lab):
    lab.at(datetime(2026, 6, 1, 8, tzinfo=timezone.utc))
    version = lab.daily('2026-06-01', published_at='2026-06-01T16:00:00+08:00')
    snapshot = lab.publish({'daily_bars': [version]}, profiles=all_profiles(),
                           created_at='2026-06-03T00:00:00.000000Z', published_at=datetime(2026, 6, 3, 1))
    result = fresh(lab).read_as_of(read_request(
        'daily_bars', selector=instruments([lab.identity]), start='2026-06-01', end='2026-06-02',
        as_of='2026-06-02T09:00:00Z', mode='system_known', snapshot=snapshot, profile='daily_bars/core',
        required=DAILY_REQUIRED + ['amount_cny']))
    assert result['rows'] and result['rows'][0]['amount_cny'] is not None
    assert result['rows'][0]['request_quality_status'] == 'complete'


def test_r05_an_absent_extra_required_field_is_quality_blocked(lab):
    snapshot, _ = basic(lab)
    result = fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00',
                                       required=['net_profit_parent', 'revenue_operating']))
    assert result['rows'] == []
    assert [(item['reason'], item['detail']) for item in result['missing']] == [
        ('QUALITY_BLOCKED', 'REQUEST_FIELD_MISSING:revenue_operating')]
    # The same fact stays readable when only its own profile fields are required.
    assert fresh(lab).read_as_of(fin(lab, snapshot, '2026-04-30T15:00:00+08:00'))['rows']


# --------------------------------------------------------------------- R06
def adjusted_world(lab):
    from app.services.governance_prices import PriceService, install_price_schema
    install_price_schema(lab.engine)
    lab.at(datetime(2026, 6, 2, 8, tzinfo=timezone.utc))
    raw = lab.daily('2026-06-02', published_at='2026-06-02T16:00:00+08:00')
    lab.at(datetime(2026, 6, 2, 9, 30, tzinfo=timezone.utc))
    prices = PriceService(lab.writer)
    factor = prices.freeze_factors({'instrument_id': lab.identity, 'instrument_version': lab.instrument_version,
                                    'calendar_version': lab.calendar_version, 'base_date': '2026-06-02',
                                    'as_of': '2026-06-02T09:00:00Z', 'knowledge_mode': 'system_known',
                                    'algorithm_version': 'r1-qfq-v1', 'raw_data_versions': [raw],
                                    'event_inputs': []}, evidence=b'SYNTHETIC repair factor proof')
    prices.derive(raw, factor)
    snapshot = lab.publish({'daily_bars': [raw]}, profiles=all_profiles(),
                           dependencies=lab.dependencies(adjustment_sets=[factor]))
    request = read_request('daily_bars', selector=instruments([lab.identity]), start='2026-06-02',
                           end='2026-06-03', as_of='2026-06-02T09:00:00Z', mode='system_known',
                           snapshot=snapshot, profile='daily_bars/core', required=DAILY_REQUIRED,
                           price_basis='adj', base_date='2026-06-02')
    return raw, factor, request


def test_r06_untampered_adjustment_is_served_with_unadjusted_volume(lab):
    raw, factor, request = adjusted_world(lab)
    result = fresh(lab).read_as_of(request)
    row = result['rows'][0]
    assert row['adj_close'] == '10.00000000' and row['adj_factor_version'] == factor
    assert row['base_date'] == '2026-06-02' and row['price_basis'] == 'adj'
    assert row['volume_shares'] == '100.00000000' and row['amount_cny'] == '1000.00000000'


def test_r06_tampered_factor_coefficients_are_refused(lab):
    """A tampered coefficient is re-derived, detected and reported per key — never served."""
    raw, factor, request = adjusted_world(lab)
    with lab.engine.begin() as db:
        db.execute(update(s.adjustment_factor_rows)
                   .where(s.adjustment_factor_rows.c.adj_factor_version == factor)
                   .values(factor=Decimal('1.5000000000000000')))
    result = fresh(lab).read_as_of(request)
    assert result['rows'] == []
    assert [(item['reason'], item['detail'].split(':')[0]) for item in result['missing']] == [
        ('ADJUSTMENT_UNAVAILABLE', 'ADJUSTMENT_INTEGRITY')]


# --------------------------------------------------------------------- R07
def test_r07_a_nonpublic_intermediate_never_leaks_its_payload(lab):
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    first = lab.financial('2026-03-31', '100', revision_order=1)
    lab.at(datetime(2026, 5, 2, 1, tzinfo=timezone.utc))
    hidden = lab.financial_unknown('2026-03-31', '90', revision_order=2, supersedes=first,
                                   acceptance_event='T09', revision=1)
    lab.at(datetime(2026, 5, 11, 1, tzinfo=timezone.utc))
    visible = lab.financial('2026-03-31', '80', published_at='2026-05-10T10:00:00+08:00',
                            revision_order=3, supersedes=hidden, acceptance_event='T09', revision=2)
    snapshot = lab.publish({'financial': [first, hidden, visible]}, profiles=all_profiles())
    result = fresh(lab).read_as_of(fin(lab, snapshot, '2026-05-20T15:00:00+08:00'))
    assert [row['data_version'] for row in result['rows']] == [visible]
    assert '90.00000000' not in canonical(result)
    assert hidden not in canonical(result['rows'])


def test_r07_two_successors_of_one_version_are_a_branch(lab):
    """Two versions superseding the same parent are a fork, never 'pick the newest'."""
    lab.at(datetime(2026, 4, 21, 1, tzinfo=timezone.utc))
    first = lab.financial('2026-03-31', '100', revision_order=1)
    lab.at(datetime(2026, 5, 2, 1, tzinfo=timezone.utc))
    left = lab.financial('2026-03-31', '80', published_at='2026-05-01T10:00:00+08:00', revision_order=2,
                         supersedes=first, acceptance_event='T09', revision=1)
    lab.at(datetime(2026, 5, 3, 1, tzinfo=timezone.utc))
    # T03 re-accepts a version that still names the old parent: the writer keeps both,
    # so the published lineage forks and the reader must refuse instead of choosing.
    right = lab.financial('2026-03-31', '70', published_at='2026-05-02T10:00:00+08:00', revision_order=3,
                          supersedes=first, acceptance_event='T03', revision=2)
    snapshot = lab.publish({'financial': [first, left, right]}, profiles=all_profiles())
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(fin(lab, snapshot, '2026-05-05T15:00:00+08:00'))
    assert exc.value.code == 'VERSION_AMBIGUOUS' and exc.value.detail == 'PUBLIC_BRANCH'


# --------------------------------------------------------------------- R09
def test_r09_a_registered_index_without_facts_is_per_key_missing(lab):
    snapshot = lab.publish({'index': []}, profiles=all_profiles())
    request = read_request('index', selector={'kind': 'index', 'index_ids': ['CSI:000300']},
                           start='2026-06-01', end='2026-06-02', as_of='2026-06-03T10:00:00Z',
                           mode='system_known', snapshot=snapshot, profile='index/core',
                           required=['index_open', 'index_high', 'index_low', 'index_close', 'index_id'])
    result = fresh(lab).read_as_of(request)
    assert result['rows'] == []
    assert [(item['entity_key'], item['reason']) for item in result['missing']] == [
        ('CSI:000300', 'NOT_INGESTED')]

    unknown = dict(request, selector={'kind': 'index', 'index_ids': ['NOT_REGISTERED:XYZ']})
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(unknown)
    assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.detail == 'INDEX_NOT_REGISTERED'


# --------------------------------------------------------------------- R10
@pytest.mark.parametrize('field,value', [
    ('dataset', []),
    ('dataset', None),
    ('selector', []),
    ('event_range', 5),
    ('as_of', True),
    ('knowledge_mode', {}),
    ('snapshot_id', None),
    ('quality_profile', []),
    ('required_fields', 'net_profit_parent'),
    ('price_basis', 1),
    ('base_date', {}),
])
def test_r10_illegal_argument_types_are_contract_errors(lab, field, value):
    snapshot, _ = basic(lab)
    request = fin(lab, snapshot, '2026-04-30T15:00:00+08:00')
    request[field] = value
    with pytest.raises(DataAccessError) as exc:
        fresh(lab).read_as_of(request)
    assert exc.value.code == 'INVALID_ARGUMENT' and exc.value.status == 400
