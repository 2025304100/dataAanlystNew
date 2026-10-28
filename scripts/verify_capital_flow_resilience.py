"""Regression and fault replay isolated from business DB, model and recipients."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from sqlalchemy.engine import URL

REGRESSION=["tests/test_capital_flow_retry_contract.py","tests/test_capital_flow_score_read_consistency.py",
    "tests/test_capital_flow_preview_consistency.py","tests/test_whitebox_capital_flow_market_snapshot.py",
    "tests/test_whitebox_external_data_sync_task.py","tests/test_external_dataset_sync_contract.py",
    "tests/test_external_input_sync_audit.py","tests/test_missing_only_writes.py","tests/test_sync_io_optimization.py",
    "tests/test_whitebox_akshare_http.py","tests/test_whitebox_akshare_cfg_row_race.py","tests/test_validation_market_gap_backfill.py"]
SOURCES=["app/services/capital_flow_history_provider.py","app/services/capital_flow_data.py",
    "app/services/external_data_sync_task.py","tests/test_capital_flow_retry_contract.py",
    "tests/test_capital_flow_resilience_mysql57.py","tests/test_sync_io_optimization.py",
    "scripts/replay_capital_flow_resilience.py","scripts/verify_capital_flow_resilience.py"]


def hashes():
    return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-connection-config",type=Path)
    parser.add_argument("--evidence-dir",type=Path,required=True)
    parser.add_argument("--stage",choices=["all","mysql","regression","replay"],default="all")
    args=parser.parse_args()
    folder=args.evidence_dir.resolve()
    folder.mkdir(parents=True,exist_ok=True)
    if (folder/"result.json").exists():parser.error("Use a new evidence directory")
    started=datetime.now(timezone.utc).isoformat()
    before=hashes()
    stages=["regression","mysql","replay"] if args.stage=="all" else [args.stage]
    env=dict(os.environ,DATABASE_URL="sqlite:///:memory:",AUTO_TRADE_CALENDAR_REFRESH="0",
        AUTO_UNIVERSE_SYNC_ON_STARTUP="0",PYTHONIOENCODING="utf-8",FACTOR_WAREHOUSE_PATH=str(folder/"isolated.duckdb"))
    if "mysql" in stages:
        if not args.local_connection_config:parser.error("mysql requires explicit local connection configuration")
        cfg=json.loads(args.local_connection_config.read_text(encoding="utf-8-sig"))["mysql"]
        if cfg["host"] not in {"localhost","127.0.0.1"}:parser.error("Loopback MySQL only")
        url=URL.create("mysql+pymysql",host=cfg["host"],port=int(cfg.get("port",3306)),username=cfg["user"],
            password=cfg.get("password",""),query={"charset":"utf8mb4"})
        env["CAPITAL_FLOW_RESILIENCE_MYSQL57_URL"]=url.render_as_string(hide_password=False)
    results=[]
    for stage in stages:
        command=[sys.executable,"-m","pytest"]
        if stage=="regression":command+=REGRESSION+["-m","not slow"]
        elif stage=="mysql":command+=["tests/test_capital_flow_resilience_mysql57.py"]
        else:
            command=[sys.executable,"scripts/replay_capital_flow_resilience.py","--output",str(folder/"replay.json")]
            for key in ["CAPITAL_FLOW_REQUEST_INTERVAL_SECONDS","CAPITAL_FLOW_BACKOFF_BASE_SECONDS",
                        "CAPITAL_FLOW_BACKOFF_MAX_SECONDS","CAPITAL_FLOW_RETRY_BUDGET_SECONDS","CAPITAL_FLOW_MAX_ATTEMPTS"]:
                env.pop(key,None)
            env["CAPITAL_FLOW_RETRY_JITTER_RATIO"]="0"
        if stage!="replay":
            command += ["-q","-o","addopts=","-o","junit_family=legacy","--tb=short","--junitxml="+str(folder/(stage+".xml"))]
        with (folder/(stage+".log")).open("w",encoding="utf-8") as log:
            proc=subprocess.run(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
        result={"stage":stage,"exit_code":proc.returncode,"passed":proc.returncode==0}
        if stage!="replay":
            junit=ET.parse(folder/(stage+".xml")).getroot()
            suites=[junit] if junit.tag=="testsuite" else list(junit.iter("testsuite"))
            for key in ["tests","failures","errors","skipped"]:
                result[key]=sum(int(item.attrib.get(key,0)) for item in suites)
            result["passed"] = result["passed"] and result["tests"]>0 and not any(result[key] for key in ["failures","errors","skipped"])
        results.append(result)
    final={"passed":all(item["passed"] for item in results) and before==hashes(),"stages":results,
        "started_at":started,"finished_at":datetime.now(timezone.utc).isoformat(),"source_hashes":before,
        "source_unchanged":before==hashes(),"business_database_selected":False,"real_models":False,"notifications_sent":False}
    (folder/"result.json").write_text(json.dumps(final,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(final,ensure_ascii=False))
    return 0 if final["passed"] else 1


if __name__=="__main__":raise SystemExit(main())
