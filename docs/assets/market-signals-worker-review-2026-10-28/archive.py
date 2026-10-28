from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import shutil
import xml.etree.ElementTree as ET

folder = Path(__file__).resolve().parent
root = folder.parents[1]
archive = root / 'docs/assets/market-signals-worker-review-2026-10-28'
assert not archive.exists()
expected = {
    'offline-probes-final.xml': (13, 4),
    'mysql-probes-final.xml': (6, 4),
    'controls.xml': (34, 0),
}
stages, unique = [], {}
for name, (count, red) in expected.items():
    cases = list(ET.parse(folder / name).getroot().iter('testcase'))
    failures = sum(case.find('failure') is not None for case in cases)
    errors = sum(case.find('error') is not None for case in cases)
    skips = sum(case.find('skipped') is not None for case in cases)
    assert (len(cases), failures, errors, skips) == (count, red, 0, 0), name
    for case in cases:
        key = case.attrib.get('classname', '') + '::' + case.attrib['name']
        state = 'failed' if case.find('failure') is not None else 'passed'
        if key in unique:
            assert unique[key] == state, key
        unique[key] = state
    stages.append({'junit': name, 'cases': len(cases), 'passed': len(cases)-failures,
                   'defect_assertion_failures': failures, 'errors': errors, 'skipped': skips})
assert len(unique) == 52 and list(unique.values()).count('failed') == 8
business = json.loads((folder / 'business-final.json').read_text(encoding='utf-8'))
assert business['passed'] and business['business_unchanged']
archive.mkdir()
for name in ('business-before.json', 'business-final.json', 'runtime-before.json', 'source-baseline.json',
             'prepare.py', 'final_state.py', 'run_mysql.py', 'archive.py',
             'offline-probes.xml', 'offline-probes.log', 'mysql-probes.xml', 'mysql-probes.log',
             'offline-probes-final.xml', 'offline-probes-final.log', 'mysql-probes-final.xml',
             'mysql-probes-final.log', 'controls.xml', 'controls.log'):
    shutil.copy2(folder / name, archive / name)
for name in ('test_worker_delivery_review.py', 'test_worker_mysql_review.py', 'offline_delivery_review_worker.py'):
    shutil.copy2(folder / 'workspace/tests' / name, archive / name)
sources = ['app/services/notifications/dispatcher.py', 'app/services/notifications/outbox.py',
    'app/services/notifications/base.py', 'app/services/notifications/webhook.py',
    'app/services/notifications/in_app.py', 'app/services/market_signal_delivery.py',
    'app/services/market_signal_engine.py', 'app/api/routes/notifications.py',
    'app/models/notification.py', 'app/db/manager.py']
baseline = json.loads((folder / 'source-baseline.json').read_text(encoding='utf-8'))
for name in sources:
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == baseline[name], name
    target = archive / 'source-files' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / name, target)
observations = []
for name in ('offline-probes-final.log', 'mysql-probes-final.log', 'controls.log'):
    for line in (folder / name).read_text(encoding='utf-8').splitlines():
        marker = 'OBSERVATION '
        if marker in line:
            observations.append({'stage': name, **json.loads(line.split(marker, 1)[1])})
(archive / 'observations.json').write_text(json.dumps(observations, ensure_ascii=False, indent=2), encoding='utf-8')
summary = {'audit_completed': True, 'fixes_applied': False, 'quality_gate_passed': False,
    'findings': {'P0': 0, 'P1': 3}, 'unique_cases': len(unique), 'passed': 44,
    'defect_assertion_failures': 8, 'errors': 0, 'skipped': 0, 'stages': stages,
    'real_models': 0, 'real_notification_transports': 0, 'business_unchanged': True,
    'source_files_unchanged': business['source_files_verified_unchanged'],
    'current_business_listeners': business['listeners'], 'page_acceptance_executed': False}
(archive / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
(archive / 'README.md').write_text('''# 多worker、发送中断与重复推送审查证据

本轮没有修复/部署业务代码。19个新增探针中11通过、8个断言失败，
失败对应3个已确认根因在SQLite及MySQL中的复现；另33条既有边界回归通过。
controls额外包含重复执行的SQLite双进程探针，已从52个不同场景合计中去重。
summary的quality_gate_passed=false表示缺陷仍在，不能解读为验收通过。

最终结论使用offline-probes-final、mysql-probes-final及controls。
mysql-probes初轮两条手动重试用例受DATETIME秒精度影响、未进入目标窗口，
其失败不作缺陷证据；最终冻结整秒时钟后才确认2次替身调用。
offline-probes最初12条不含独立进程控制，最终为13条。中间记录保留，不重复累计。

测试调用的send_message全部是本地替身，不执行真实通知适配器，不发送HTTP通知。
MySQL只连接新建codex_worker_review_*库；业务数据、原规则版本、模型通知账目不变。
business-final的passed仅指审查隔离和数据保留检查通过，不指业务缺陷已经修复。
当前8000/5173无监听，本轮未启停服务、未执行真实页面验收。
source-files为审查时未改动源码，未归档.env、连接凭据或数据库文件。
''', encoding='utf-8')
report = 'docs/代码审查-行情异动多worker发送中断与重复推送-2026-10-28.md'
for target in re.findall(r'\]\((D:/[^)]+)\)', (root / report).read_text(encoding='utf-8')):
    if Path(target) == archive / 'evidence-manifest.json':
        continue
    assert Path(re.sub(r':\d+$', '', target)).exists(), target
manifest = {'created_at_utc': datetime.now(timezone.utc).isoformat(), 'audit_completed': True,
    'fixes_applied': False, 'report': {'path':report, 'sha256':hashlib.sha256((root / report).read_bytes()).hexdigest()},
    'test_summary': summary, 'files': []}
for path in sorted(archive.rglob('*')):
    if path.is_file():
        assert path.suffix.lower() not in ('.db', '.sqlite', '.sqlite3') and path.name != '.env'
        manifest['files'].append({'path':path.relative_to(archive).as_posix(), 'bytes':path.stat().st_size,
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
(archive / 'evidence-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
for item in manifest['files']:
    assert hashlib.sha256((archive / item['path']).read_bytes()).hexdigest() == item['sha256']
assert hashlib.sha256((root / report).read_bytes()).hexdigest() == manifest['report']['sha256']
print(json.dumps({'archive':str(archive), 'files':len(manifest['files']), 'unique_cases':52,
    'passed':44, 'defect_assertion_failures':8, 'findings_P1':3, 'hashes_verified':True}, ensure_ascii=False))
