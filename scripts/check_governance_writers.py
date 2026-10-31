"""Read-only CI guard; never regenerates or approves its own inventory."""
from pathlib import Path
import argparse,json,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.services.governance_writer_audit import audit_writes
def main():
    p=argparse.ArgumentParser();p.add_argument('--inventory',type=Path,default=ROOT/'app/services/governance_writer_inventory.json');p.add_argument('--output',type=Path);args=p.parse_args()
    report=audit_writes(ROOT,json.loads(args.inventory.read_text(encoding='utf-8')))
    if args.output:
        if args.output.exists():raise FileExistsError(args.output)
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report['status']=='pass' else 1
if __name__=='__main__':raise SystemExit(main())
