"""Trusted helper process: a CLI command whose first ledger rescan waits for another writer.

Usage: python scan_worker.py <pause-dir> <cli args...>

The rescan runs inside the ledger transaction after its UTC clock was fixed.
The first one writes `<pause-dir>/reached` (that clock), waits until the
parent creates `<pause-dir>/go` (after it wrote or finished a run record in
its own process), then scans. Prints nothing itself; the exit code is the CLI's.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import apprentice.controls.authority as authority
from apprentice.cli import main

if TYPE_CHECKING:
    from datetime import datetime

pause = Path(sys.argv[1])
scan = authority.scan_records
waited: list[bool] = []


def paused_scan(store_dir: Path, now: datetime) -> list[object]:
    if not waited:
        waited.append(True)
        (pause / "reached").write_text(now.isoformat())
        while not (pause / "go").exists():
            time.sleep(0.01)
    return scan(store_dir, now)  # type: ignore[return-value]


authority.scan_records = paused_scan  # type: ignore[assignment]
sys.exit(main(sys.argv[2:]))
