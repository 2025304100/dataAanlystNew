from datetime import datetime,timezone
from pathlib import Path
from urllib.request import build_opener,ProxyHandler
import difflib,hashlib,json,re,shutil,sys
import xml.etree.ElementTree as ET
import psutil

folder=Path(__file__).resolve().parent
root=folder.parents[1]
sys.path.insert(0,str(root))
from scripts.restore_validation_environment import invariants
archive=root/'docs/assets/market-signals-worker-fixes-2026-10-28'
assert not archive.exists()
applied=json.loads((folder/'applied.json').read_text(encoding='utf-8'))
identities=set()
for stage in applied['stages']:
    cases=list(ET.parse(folder/stage['junit']).getroot().iter('testcase'))
    assert len(cases)==stage['passed']
    assert not any(c.find(tag) is not None for c in cases for tag in ('failure','error','skipped'))
    for case in cases:
        key=case.attrib.get('classname','')+'::'+case.attrib['name']
        assert key not in identities
        identities.add(key)
assert len(identities)==315
recheck=list(ET.parse(folder/'applied-regression.xml').getroot().iter('testcase'))
assert len(recheck)==94
assert not any(c.find(tag) is not None for c in recheck for tag in ('failure','error','skipped'))
assert all(c.attrib.get('classname','')+'::'+c.attrib['name'] in identities for c in recheck)
for name in ('business-final.json','readonly-ui/result.json','runtime-restore/restore.json'):
    assert json.loads((folder/name).read_text(encoding='utf-8'))['passed']
final=json.loads((folder/'business-final.json').read_text(encoding='utf-8'))
assert invariants()==final['after']
for name in ('backend','frontend'):
    expected=final['processes'][name]
    process=psutil.Process(expected['pid'])
    assert abs(process.create_time()-expected['created'])<0.001
    assert Path(process.cwd()).resolve()==Path(expected['cwd']).resolve()
    assert process.cmdline()==expected['command']
checks=[]
for url in ('http://127.0.0.1:8000/health','http://127.0.0.1:5173/api/v1/market-signals/settings'):
    with build_opener(ProxyHandler({})).open(url,timeout=8) as response:
        assert response.status==200 and response.headers.get('X-App-Validation-Mode')=='1'
        checks.append({'url':url,'status':200,'validation_mode':'1'})
(folder/'closing-check.json').write_text(json.dumps({'passed':True,'checked_at_utc':datetime.now(timezone.utc).isoformat(),
    'business_unchanged':True,'process_identity_unchanged':True,'http':checks},indent=2),encoding='utf-8')
archive.mkdir()
patch=[]
for item in applied['files']:
    name=item['path']
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==item['candidate_sha256']
    target=archive/'applied-sources'/name
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(root/name,target)
    backup=folder/'live-code-backup'/name
    assert backup.exists()==(item['baseline_sha256'] is not None)
    if backup.exists():
        assert hashlib.sha256(backup.read_bytes()).hexdigest()==item['baseline_sha256']
    old=backup.read_text(encoding='utf-8-sig').splitlines(keepends=True) if backup.exists() else []
    new=(root/name).read_text(encoding='utf-8-sig').splitlines(keepends=True)
    patch.extend(difflib.unified_diff(old,new,fromfile='a/'+name if backup.exists() else '/dev/null',tofile='b/'+name))
(archive/'applied.patch').write_text(''.join(patch),encoding='utf-8')
for pattern in ('*.xml','*.log','*.json','*.py'):
    for path in folder.glob(pattern):
        shutil.copy2(path,archive/path.name)
for name in ('runtime-restore','readonly-ui','prior-reports','live-code-backup'):
    shutil.copytree(folder/name,archive/name)
reports=['docs/验证报告-行情异动多worker中断与重复推送修复-2026-10-28.md',
    'docs/变更记录-行情消息P0-2026-10-25.md','docs/使用说明-行情异动提醒-2026-10-27.md',
    'docs/验证报告-后端受控重启与行情异动页面验收-2026-10-27.md','docs/交付与验证报告-行情异动提醒完整功能-2026-10-27.md']
for target in re.findall(r'\]\((D:/[^)]+)\)',(root/reports[0]).read_text(encoding='utf-8')):
    if Path(target)==archive/'evidence-manifest.json':
        continue
    assert Path(re.sub(r':\d+$','',target)).exists(),target
(archive/'README.md').write_text('''# 三项P1修复与受保护环境恢复证据

最终门槛为main-regression-final 93条、legacy-regression 206条、mysql-final 16条，
共315个不同用例，失败/错误/跳过均0。applied-regression的94条是其中用例的工作区复核，
不重复累计。新增专项为SQLite 23条和独立MySQL5.7 16条。

baseline-red为三项原缺陷的4条失败断言，completion-red为迟到/重复回执的2条失败断言。
worker-green、main-regression、mysql是中间批次，不能作为最终通过结果；其中恢复了
全新非法JSON的旧契约，校正统一异常类型及MySQL验证会话的旧RR快照读取。
最终源码和测试以applied-sources、应用清单及最终绿色批次为准。

新增运输均用内存替身；旧设置契约使用MockTransport，不执行真实通知运输或真实模型。
readonly-ui为8000及5173真实页面的只读验收，没有新增业务规则或版本。
business-final和closing-check核对原业务状态完全一致，0092未迁移，全部后台调度关闭。

live-code-backup保留4个既有文件；applied.patch包含8个文件本轮变更；prior-reports保留
更新前4份文档以核对旧证据中的历史文档哈希。没有归档.env、连接凭据或数据库文件。
''',encoding='utf-8')
manifest={'passed':True,'created_at_utc':datetime.now(timezone.utc).isoformat(),'unique_pytest_cases':315,
    'applied_recheck_cases':94,'stages':applied['stages'],'source_files':applied['files'],'reports':[],'files':[]}
for name in reports:
    manifest['reports'].append({'path':name,'sha256':hashlib.sha256((root/name).read_bytes()).hexdigest()})
for path in sorted(archive.rglob('*')):
    if path.is_file():
        assert path.suffix.lower() not in ('.db','.sqlite','.sqlite3') and path.name!='.env'
        manifest['files'].append({'path':path.relative_to(archive).as_posix(),'bytes':path.stat().st_size,
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
(archive/'evidence-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
for item in manifest['files']:
    assert hashlib.sha256((archive/item['path']).read_bytes()).hexdigest()==item['sha256']
for item in manifest['reports']:
    assert hashlib.sha256((root/item['path']).read_bytes()).hexdigest()==item['sha256']
print(json.dumps({'archive':str(archive),'files':len(manifest['files']),'unique_tests':315,
    'applied_recheck_cases':94,'hashes_verified':True,'business_unchanged':True},ensure_ascii=False))
