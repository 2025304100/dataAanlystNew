"""Re-run the reviewed read-only candidate inspection into this date's evidence folder."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')
original = ROOT / 'tmp/main-history-investigation/read_candidate_status.py'
source = original.read_text(encoding='utf-8')
old = "root=Path('tmp/main-history-investigation')"
new = "root=Path('tmp/main-score-review-2026-10-24')"
assert source.count(old) == 1
exec(compile(source.replace(old, new), str(original), 'exec'))
