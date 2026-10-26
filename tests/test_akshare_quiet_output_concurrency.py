"""Public quiet-output context integrity, using an isolated interpreter."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.whitebox
ROOT = Path(__file__).resolve().parents[1]


def test_overlapping_quiet_contexts_restore_streams_and_preserve_other_threads():
    code = r'''
import io,json,sys,threading
from app.services.akshare_utils import quiet_akshare_output
console=sys.stdout
original_out,original_err=io.StringIO(),io.StringIO()
sys.stdout,sys.stderr=original_out,original_err
entered_first,entered_second,first_left=threading.Event(),threading.Event(),threading.Event()
errors=[]
def first():
 try:
  with quiet_akshare_output():
   print('hidden-first')
   entered_first.set()
   assert entered_second.wait(5)
 except Exception as exc:errors.append(type(exc).__name__)
 finally:first_left.set()
def second():
 try:
  assert entered_first.wait(5)
  with quiet_akshare_output():
   entered_second.set()
   assert first_left.wait(5)
   print('hidden-second')
 except Exception as exc:errors.append(type(exc).__name__)
a,b=threading.Thread(target=first),threading.Thread(target=second)
a.start();b.start();a.join(8);b.join(8)
restored=sys.stdout is original_out and sys.stderr is original_err
try:print('visible-after')
except Exception as exc:errors.append(type(exc).__name__)
out=original_out.getvalue()
sys.stdout=console
print(json.dumps({'restored':restored,'errors':errors,'out':out,'alive':a.is_alive() or b.is_alive()}))
'''
    run = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                         text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout.strip().splitlines()[-1])
    assert result == {"restored": True, "errors": [], "out": "visible-after\n", "alive": False}


def test_nested_exception_restores_streams_without_muting_non_quiet_thread(capsys):
    import threading
    from app.services.akshare_utils import quiet_akshare_output
    with pytest.raises(ValueError):
        with quiet_akshare_output():
            print("hidden-parent")
            with quiet_akshare_output():
                print("hidden-child")
            other=threading.Thread(target=lambda:print("other-thread-visible"))
            other.start()
            other.join(3)
            raise ValueError("controlled")
    print("visible-after-exception")
    assert capsys.readouterr().out=="other-thread-visible\nvisible-after-exception\n"
