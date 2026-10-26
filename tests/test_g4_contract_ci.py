"""Public CI command must refuse partial or skipped governance coverage."""
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

pytestmark=pytest.mark.whitebox
ROOT=Path(__file__).resolve().parents[1]
# The accepted suite is an independently specified fixture, not loaded from the checker.
CASES=[
 'test_g4_api_01_status','test_g4_api_02_reconcile','test_g4_api_03_confirm','test_g4_api_04_transition',
 'test_g4_api_08a_excludes_today','test_g4_api_08b_skips_incomplete_day','test_g4_api_08c_us_region_does_not_count',
 'test_g4_api_08d_falls_back_when_universe_empty','test_g4_api_08e_route_uses_default_only_when_payload_omits_date',
 'test_g4_http_confirmation_contract_rejections','test_g4_http_stale_and_wrong_report_rejected',
 'test_g4_http_generic_ready_never_clears_reconciliation','test_g4_http_incomplete_execution_stays_blocked',
 'test_g4_http_calendar_close_weekend_and_holiday_boundaries',
 'test_g4_http_explicit_date_diagnosis_does_not_invent_calendar_pass',
 'test_g4_http_confirmation_retains_other_dimensions',
 'test_g4_http_confirmation_does_not_accept_unreviewed_applied_binding',
 'test_g4_http_unknown_calendar_no_fallback[absent]',
 'test_g4_http_unknown_calendar_no_fallback[unproven]',
 'test_g4_http_unknown_calendar_no_fallback[incomplete]',
 'test_g4_http_unknown_calendar_no_fallback[wrong-source]',
 'test_g4_http_unknown_calendar_no_fallback[wrong-actor]',
 'test_g4_http_unknown_calendar_no_fallback[future-stamp]',
]


@pytest.mark.parametrize('mode',['valid','missing-case','skipped','failure','error','empty','malformed','suite-error'])
def test_ci_command_rejects_incomplete_or_nonpassing_reports(tmp_path,mode):
    path=tmp_path/'report.xml'
    suite=ET.Element('testsuite')
    names=[] if mode=='empty' else CASES[:-1] if mode=='missing-case' else CASES
    for name in names:
        case=ET.SubElement(suite,'testcase',name=name,classname='tests.test_g4_api_governance')
        if mode in {'skipped','failure','error'} and name==CASES[0]:ET.SubElement(case,mode)
    if mode=='suite-error':suite.set('errors','1')
    path.write_text('<broken' if mode=='malformed' else ET.tostring(suite,encoding='unicode'),encoding='utf8')
    run=subprocess.run([sys.executable,str(ROOT/'scripts/verify_g4_contracts.py'),'--check-junit',str(path)],
        cwd=ROOT,capture_output=True,text=True,encoding='utf8')
    assert run.returncode==(0 if mode=='valid' else 1),run.stdout+run.stderr
    assert json.loads(run.stdout)['passed'] is (mode=='valid')
