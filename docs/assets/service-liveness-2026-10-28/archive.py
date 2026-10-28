from datetime import datetime,timezone
from pathlib import Path
from urllib.request import build_opener,ProxyHandler
import difflib,hashlib,json,re,shutil,sys,tempfile
import xml.etree.ElementTree as ET
import psutil

folder=Path(__file__).resolve().parent
root=folder.parents[1]
archive=root/'docs/assets/service-liveness-2026-10-28'
assert not archive.exists()
sys.path.insert(0,str(root))
from scripts.restore_validation_environment import invariants
from scripts import validation_service_host as host
applied=json.loads((folder/'applied.json').read_text(encoding='utf-8'))
assert applied['unique_tests']==49
ids=set()
for stage in applied['stages']:
    cases=list(ET.parse(folder/stage['junit']).getroot().iter('testcase'))
    assert len(cases)==stage['passed'] and not any(c.find(t) is not None for c in cases for t in ('failure','error','skipped'))
    for c in cases:
        key=c.attrib.get('classname','')+'::'+c.attrib['name']
        assert key not in ids;ids.add(key)
assert len(ids)==49
recheck=[]
for name in ('applied-final-cli.xml','applied-native-final.xml'):
    cases=list(ET.parse(folder/name).getroot().iter('testcase'))
    assert not any(c.find(t) is not None for c in cases for t in ('failure','error','skipped'))
    recheck.extend(cases)
assert len(recheck)==49
assert {c.attrib.get('classname','')+'::'+c.attrib['name'] for c in recheck}==ids
business=json.loads((folder/'business-final.json').read_text(encoding='utf-8'))
assert business['passed'] and invariants()==business['after']
state=host.read(host.RUNTIME/'state.json')
assert state['status']=='ready' and host.matches(state['host']) and state['host']==business['host']
assert datetime.now(timezone.utc).timestamp()-state['heartbeat']<8
for name,record in state['services'].items():
    assert host.matches(record) and record==business['services'][name]
    assert psutil.Process(record['pid']).ppid()==state['host']['pid']
checks=[]
for url in ('http://127.0.0.1:8000/health','http://127.0.0.1:5173/api/v1/market-signals/settings'):
    with build_opener(ProxyHandler({})).open(url,timeout=8) as response:
        assert response.status==200 and response.headers.get('X-App-Validation-Mode')=='1'
        checks.append({'url':url,'status':200,'validation_mode':'1'})
tasks=json.loads((folder/'final-task-check.json').read_text(encoding='utf-8-sig'))
if isinstance(tasks,dict):tasks=[tasks]
assert len(tasks)==1 and tasks[0]['name']==host.TASK_NAME and tasks[0]['state']==4 and tasks[0]['triggers']==0
assert json.loads((folder/'readonly-ui/result.json').read_text(encoding='utf-8'))['passed']
(folder/'closing-check.json').write_text(json.dumps({'passed':True,'checked_at_utc':datetime.now(timezone.utc).isoformat(),
    'business_unchanged':True,'host_identity_unchanged':True,'http':checks},indent=2),encoding='utf-8')
shutil.copy2(host.RUNTIME/'events.jsonl',folder/'host-events.jsonl')
archive.mkdir()
patch=[]
for item in applied['files']:
    name=item['path']
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==item['candidate_sha256']
    destination=archive/'applied-sources'/name
    destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(root/name,destination)
    old=folder/'live-backup'/name
    assert old.exists()==(item['baseline_sha256'] is not None)
    if old.exists():assert hashlib.sha256(old.read_bytes()).hexdigest()==item['baseline_sha256']
    lines=old.read_text(encoding='utf-8-sig').splitlines(keepends=True) if old.exists() else []
    patch.extend(difflib.unified_diff(lines,(root/name).read_text(encoding='utf-8-sig').splitlines(keepends=True),
        fromfile='a/'+name if old.exists() else '/dev/null',tofile='b/'+name))
(archive/'applied.patch').write_text(''.join(patch),encoding='utf-8')
for pattern in ('*.json','*.jsonl','*.xml','*.py','*.log'):
    for path in folder.glob(pattern):shutil.copy2(path,archive/path.name)
for name in ('readonly-ui','prior-reports','live-backup','managed-restore','managed-restore-final','upgrade-control'):
    shutil.copytree(folder/name,archive/name)
native=archive/'native-proofs';native.mkdir()
temp=Path(tempfile.gettempdir())/'pytest-of-demo'
for run in temp.glob('pytest-*'):
    if not run.is_dir() or run.is_symlink():continue
    for path in run.glob('test_scheduler_owned_listener_*/native-result.json'):
        out=native/(run.name+'.json')
        shutil.copy2(path,out)
reports=['docs/验证报告-前后端退出原因与服务存活修复-2026-10-28.md',
    'docs/变更记录-行情消息P0-2026-10-25.md','docs/使用说明-行情异动提醒-2026-10-27.md',
    'docs/验证报告-后端受控重启与行情异动页面验收-2026-10-27.md','docs/交付与验证报告-行情异动提醒完整功能-2026-10-27.md']
for target in re.findall(r'\]\((D:/[^)]+)\)',(root/reports[0]).read_text(encoding='utf-8')):
    if Path(target)==archive/'evidence-manifest.json':continue
    assert Path(re.sub(r':\d+$','',target)).exists(),target
(archive/'README.md').write_text('''# 前后端存活修复证据

最终49个不同pytest场景全部通过：regression-final-cli 48条，native-complete 1条。
工作区applied-final-cli 48条、applied-native-final 1条为复核，不重复计数。
新增托管边界17条及原生任务1条，其余为既有启动/策略/验收保护。

nested-job-repro是额外真实故障复现：旧启动器脱离内层后仍受外层kill-on-close约束。
default/escalated诊断进程均只睡眠并自动结束；未关闭真实执行器Job。
native-proofs为临时任务健康监听在调用方Job关闭后仍存活的真实结果，任务均清理。
actual-task.xml和任务清单为唯一正式最低权限按需任务，无登录/开机触发器。

早期regression/native等批次可能包含已修正的失败。具体包含工作目录创建、
SID名称规范化、esbuild直属conhost保护和文件入口导入路径；最终结果仅采用上述绿色批次。
第一次前端迁移被身份守卫拒绝并保留服务，事件保留在host-events；随后精确修复、受控迁移通过。

managed-restore是首版接管，managed-restore-final及business-runtime为最终独立托管。
readonly-ui仅GET检查，业务数据、0092、模型通知计数、原任务/分区/190证据均不变。
全程无真实模型/通知，业务后台调度关闭。当前仍需用户登录，重登后按需恢复。

applied-sources保留7个应用文件，live-backup保留3个旧脚本，prior-reports保留4份旧文档。
未归档数据库、.env或连接凭据。host-events为结构化运维记录，不复制含请求令牌的原日志。
历史Application日志查询资源错误及Security无匹配事件均保留，不作无崩溃证明。
''',encoding='utf-8')
manifest={'passed':True,'created_at_utc':datetime.now(timezone.utc).isoformat(),'unique_tests':49,
    'applied_recheck_cases':49,'stages':applied['stages'],'source_files':applied['files'],'reports':[],'files':[]}
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
print(json.dumps({'archive':str(archive),'files':len(manifest['files']),'unique_tests':49,
    'applied_recheck_cases':49,'hashes_verified':True,'business_unchanged':True},ensure_ascii=False))
