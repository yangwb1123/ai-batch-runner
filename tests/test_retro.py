"""retro 子命令回归：日志扫描计数与阶段失败判定。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pbatch.retro import scan_target  # noqa: E402

SAMPLE = """\
2026-08-01 10:00:00 [INFO] OK  done  [12.5s]  [output=out.md]
2026-08-01 10:01:00 [WARNING] FAIL  [code=1]  [30.0s]
2026-08-01 10:02:00 [WARNING] VALIDATION FAILED (exit=1): gate; output NOT saved
2026-08-01 10:03:00 [INFO] Stage 'impl' completed: 1/1 tasks succeeded
2026-08-01 10:04:00 [INFO] Stage 'review' completed: 0/1 tasks succeeded
2026-08-01 10:05:00 [ERROR] GATE REJECTED at stage 'gate'
2026-08-01 10:06:00 [INFO] RETRY 1/3 for task [1/1] in 27s
2026-08-01 10:07:00 [ERROR] Pipeline halted by failed stage: review
2026-08-01 10:08:00 [INFO] REUSED+VALIDATED: out.md
"""


def test_retro_counts_and_stage_failure_semantics(tmp_path: Path):
    log = tmp_path / "run.log"
    log.write_text(SAMPLE, encoding="utf-8")
    counts, evidence, durations = scan_target(str(log))
    assert counts["task_ok"] == 1
    assert counts["task_failed"] == 1
    assert counts["validation_failed"] == 1
    assert counts["gate_rejected"] == 1
    assert counts["retry"] == 1
    assert counts["stage_halted"] == 1
    # stage_failed must count ONLY partial/failed stage completions
    assert counts["stage_failed"] == 1, counts["stage_failed"]
    assert counts["reuse"] == 1
    assert durations == [12.5]
