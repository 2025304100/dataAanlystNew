from datetime import datetime, timezone
from pathlib import Path
import difflib
import hashlib
import json
import re
import shutil
import xml.etree.ElementTree as ET

folder = Path(__file__).resolve().parent
root = folder.parents[1]
archive = root / 'docs/assets/market-signals-boundary-fixes-2026-10-28'
assert not archive.exists(), 'Archive must be a fresh directory'
applied = json.loads((folder / 'applied.json').read_text(encoding='utf-8'))
assert applied['passed'] and applied['unique_tests'] == 311
stages = applied['stages']
identities = set()
for item in stages:
    cases = list(ET.parse(folder / item['junit']).getroot().iter('testcase'))
    assert len(cases) == item['passed'], item['junit']
    assert not any(case.find(tag) is not None for case in cases for tag in ('failure', 'error', 'skipped')), item['junit']
    for case in cases:
        identity = case.attrib.get('classname', '') + '::' + case.attrib['name']
        assert identity not in identities, identity
        identities.add(identity)
assert len(identities) == 311
recheck = list(ET.parse(folder / 'applied-regression.xml').getroot().iter('testcase'))
assert len(recheck) == 64
assert not any(case.find(tag) is not None for case in recheck for tag in ('failure', 'error', 'skipped'))
assert all(case.attrib.get('classname', '') + '::' + case.attrib['name'] in identities for case in recheck)
for name in ('business-ui/ui-result.json', 'vite-readonly/result.json', 'business-final.json',
             'runtime-restore/restore.json', 'final-check.json'):
    assert json.loads((folder / name).read_text(encoding='utf-8'))['passed'], name
archive.mkdir()
patch = []
for item in applied['files']:
    name = item['path']
    source = root / name
    assert hashlib.sha256(source.read_bytes()).hexdigest() == item['candidate_sha256'], name
    target = archive / 'applied-sources' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    previous = folder / 'live-code-backup' / name
    assert previous.exists() == (item['baseline_sha256'] is not None), name
    if previous.exists():
        assert hashlib.sha256(previous.read_bytes()).hexdigest() == item['baseline_sha256'], name
    old = previous.read_text(encoding='utf-8-sig').splitlines(keepends=True) if previous.exists() else []
    new = source.read_text(encoding='utf-8-sig').splitlines(keepends=True)
    patch.extend(difflib.unified_diff(old, new, fromfile='a/' + name if previous.exists() else '/dev/null', tofile='b/' + name))
(archive / 'applied.patch').write_text(''.join(patch), encoding='utf-8')
names = ['applied.json', 'deployment-ready.json', 'source-baseline.json', 'business-before.json',
         'business-final.json', 'final-check.json', 'signal-regression.xml', 'signal-regression.log',
         'applied-regression.xml', 'applied-regression.log', 'native.xml', 'native.log',
         'ui-and-legacy.xml', 'ui-and-legacy.log', 'fund-regression-driver.log', 'fund-mysql-driver.log',
         'observations.json', 'prepare.py', 'apply_verified.py', 'check_business.py',
         'run_native.py', 'verify_frontend.py', 'final_check.py', 'archive.py']
names += [path.name for path in folder.glob('br*.xml')]
names += [path.name for path in folder.glob('br*.log')]
names += ['initial-green.xml', 'initial-green.log']
for name in names:
    shutil.copy2(folder / name, archive / name)
for name in ('fund-regression', 'fund-mysql', 'runtime-restore', 'business-ui', 'vite-readonly',
             'live-code-backup', 'prior-reports'):
    shutil.copytree(folder / name, archive / name)
ui_base = folder / 'workspace/tmp/validation-runtime-isolated-ui'
ui_folders = [p for p in ui_base.iterdir() if p.is_dir()]
assert len(ui_folders) == 1
shutil.copytree(ui_folders[0], archive / 'isolated-ui')
reports = ['docs/验证报告-行情异动并发跨日取消与重试边界修复-2026-10-28.md',
           'docs/变更记录-行情消息P0-2026-10-25.md', 'docs/使用说明-行情异动提醒-2026-10-27.md',
           'docs/验证报告-后端受控重启与行情异动页面验收-2026-10-27.md']
for name in reports:
    body = (root / name).read_text(encoding='utf-8')
    if name == reports[0]:
        for target in re.findall(r'\]\((D:/[^)]+)\)', body):
            if Path(target) == archive / 'evidence-manifest.json':
                continue
            assert Path(re.sub(r':\d+$', '', target)).exists(), target
readme = '''# 五项并发、跨日、取消与重试边界修复证据

311个不同pytest用例全部通过；applied-regression的64条属于工作区整合后的复核，
已包含在311条中，不重复累计。本轮新增33条普通边界回归和3条MySQL并发回归。

br01/br02/br03/br04/br05的red文件保留修复前失败断言，属于预期的缺陷复现；
initial-green是中间回归。最终结论以signal-regression、fund-regression、fund-mysql、
native、ui-and-legacy及applied-regression为准。3条真实供应商slow探针未选择，
没有把它们计入通过或跳过数。observations.json是隔离回放观测，不代表业务送达。

business-ui为8000真实生产页面验收，vite-readonly为5173只读验收，
isolated-ui为临时数据库/临时端口页面端到端测试。所有真实模型及业务通知投递均为0。
测试内成功/失败回执为本地持久化替身，不执行通知适配器。业务仅允许新增2条具名
验收规则和5个版本，最终均暂停且shadow；0092、190证据、原任务和5367分区、
模型配置与通知账目等指纹均保留。完整核对见business-final和final-check。

applied-sources保留本轮候选；live-code-backup保留5个旧文件；applied.patch仅包含本轮
7个文件的差异。prior-reports保留更新前3份历史文档，旧阶段证据及审查报告未改写。
source-baseline记录准备时整个项目Python源码哈希，未归档业务.env、凭据或数据库文件。
evidence-manifest记录归档文件、四份最新报告和7个实际应用文件的SHA256。
'''
(archive / 'README.md').write_text(readme, encoding='utf-8')
manifest = {'created_at_utc': datetime.now(timezone.utc).isoformat(), 'passed': True,
            'unique_pytest_cases': len(identities), 'applied_recheck_cases': len(recheck),
            'stages': stages, 'source_files': applied['files'], 'reports': [], 'files': []}
for name in reports:
    manifest['reports'].append({'path': name, 'sha256': hashlib.sha256((root / name).read_bytes()).hexdigest()})
for path in sorted(archive.rglob('*')):
    if path.is_file():
        assert path.suffix.lower() not in ('.db', '.sqlite', '.sqlite3'), path
        assert path.name != '.env', path
        manifest['files'].append({'path': path.relative_to(archive).as_posix(), 'bytes': path.stat().st_size,
                                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
(archive / 'evidence-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
for item in manifest['files']:
    assert hashlib.sha256((archive / item['path']).read_bytes()).hexdigest() == item['sha256'], item['path']
for item in manifest['reports']:
    assert hashlib.sha256((root / item['path']).read_bytes()).hexdigest() == item['sha256'], item['path']
print(json.dumps({'archive': str(archive), 'files': len(manifest['files']), 'unique_pytest_cases': len(identities),
                  'applied_recheck_cases': len(recheck), 'all_hashes_verified': True, 'report_links_valid': True}, ensure_ascii=False))
