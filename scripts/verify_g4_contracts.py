"""Fail closed on missing, skipped or failing G4 HTTP contract coverage."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
REQUIRED={
    'test_g4_http_applied_binding_is_immediately_effective_at_fractional_second',
    'test_g4_api_01_status','test_g4_api_02_reconcile','test_g4_api_03_confirm','test_g4_api_04_transition',
    'test_g4_api_08a_excludes_today','test_g4_api_08b_skips_incomplete_day',
    'test_g4_api_08c_us_region_does_not_count','test_g4_api_08d_falls_back_when_universe_empty',
    'test_g4_api_08e_route_uses_default_only_when_payload_omits_date',
    'test_g4_http_confirmation_contract_rejections','test_g4_http_stale_and_wrong_report_rejected',
    'test_g4_http_generic_ready_never_clears_reconciliation','test_g4_http_incomplete_execution_stays_blocked',
    'test_g4_http_calendar_close_weekend_and_holiday_boundaries',
    'test_g4_http_explicit_date_diagnosis_does_not_invent_calendar_pass',
    'test_g4_http_confirmation_retains_other_dimensions',
    'test_g4_http_confirmation_does_not_accept_unreviewed_applied_binding',
    *('test_g4_http_unknown_calendar_no_fallback['+kind+']' for kind in
      ('absent','unproven','incomplete','wrong-source','wrong-actor','future-stamp')),
}


def validate_junit(path):
    try:
        root=ET.parse(path).getroot()
    except (OSError,ET.ParseError):
        return {'passed':False,'problems':['JUnit missing or unreadable']}
    cases=list(root.iter('testcase'))
    counts={'tests':len(cases),**{key:sum(case.find(tag) is not None for case in cases)
            for key,tag in (('failures','failure'),('errors','error'),('skipped','skipped'))}}
    seen={case.get('name') for case in cases if case.get('classname','').endswith('test_g4_api_governance')}
    missing=sorted(REQUIRED-seen)
    # Collection/setup errors may appear at testsuite level rather than a test case.
    suite_errors=sum(int(suite.get('errors','0')) for suite in root.iter('testsuite'))
    passed=counts['tests']>0 and not missing and not suite_errors and not any(counts[key] for key in ('failures','errors','skipped'))
    return {'passed':passed,**counts,'missing_required_cases':missing,'suite_errors':suite_errors}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--evidence-dir',type=Path)
    mode.add_argument('--check-junit',type=Path)
    args=parser.parse_args()
    if args.check_junit:
        report=validate_junit(args.check_junit)
    else:
        folder=args.evidence_dir.resolve()
        if folder.exists() and any(folder.iterdir()):parser.error('Use a new evidence directory; old reports are retained')
        folder.mkdir(parents=True,exist_ok=True)
        env=dict(os.environ,DATABASE_URL='sqlite:///:memory:',PYTHONIOENCODING='utf-8',
                 AUTO_UNIVERSE_SYNC_ON_STARTUP='0',AUTO_TRADE_CALENDAR_REFRESH='0')
        env.pop('G4_MYSQL57_URL',None)
        with (folder/'pytest.log').open('w',encoding='utf8') as log:
            run=subprocess.run([sys.executable,'-m','pytest','tests/test_g4_api_governance.py',
                'tests/test_portfolio_inspection_workflow_http.py','tests/test_portfolio_inspection_reconciliation_services.py',
                'tests/test_g4_contract_ci.py',
                '-q','--tb=short','--junitxml',str(folder/'pytest.xml')],cwd=ROOT,env=env,
                stdout=log,stderr=subprocess.STDOUT)
        report={**validate_junit(folder/'pytest.xml'),'pytest_exit_code':run.returncode}
        report['passed']=report['passed'] and run.returncode==0
        (folder/'acceptance-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
