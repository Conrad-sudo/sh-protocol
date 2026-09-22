"""
Shared plumbing for the test scripts in this folder.

Importing it puts app/ on sys.path, so a test can `import db`, `import api` and the rest exactly as
the app's own modules do. `python app/tests/test_x.py` only puts app/tests/ there, so every test
must import this BEFORE any app module.
"""
import sys
from pathlib import Path

APP_DIR = str(Path(__file__).resolve().parent.parent)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = ""):
    """Prints one PASS/FAIL line. A failure is recorded, not raised, so one run reports every broken check."""
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{': ' + detail if detail else ''}")
        failures.append(label)


def finish(success_message: str):
    """Prints the run's summary and exits non-zero if any check failed."""
    print()
    if failures:
        print(f"FAILED ({len(failures)}): {failures}")
        sys.exit(1)
    print(success_message)
