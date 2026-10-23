"""Serial in-tree campaign cutoff: guard, cancellation and publication."""

from __future__ import annotations

import threading
import time
from contextlib import nullcontext

from .campaign_budget import CampaignDeadlineExpired, CampaignGuardFailed
from .campaign_deadline import check_deadline, deadline_error
from .campaign_execution_guard import SerialDeadlineGuard
from .campaign_isolated_outcome import stopped_outcome
from .config import log
from .process_exec import kill_owned_process
from .runner import proc_snapshot


class SerialChildKiller:
    """Signal each registered agent child at most once, never after reaping.

    The cutoff guard polls repeatedly; re-signalling a handle whose PID was
    already reaped could hit a reused group. A handle observed as reaped is
    skipped, and a live handle is signalled once while its PID is still owned
    by the runner (the kill reaps the direct child itself).
    """

    def __init__(self):
        self.unobserved = False

    def __call__(self) -> None:
        for proc in proc_snapshot():
            # The shared cleanup registry dedups handles, so object-id reuse
            # cannot suppress a new child and repeated polls stay safe.
            if kill_owned_process(proc) is False:
                self.unobserved = True  # ownership retained; retry later


def execute_serial_supervised(settings, direction, root, pipeline_path, logfile,
                              model, provider, timeout, reuse, execute):
    """Run one in-tree serial direction under the campaign cutoff.

    ``execute`` is the direction pipeline callable; it receives the campaign
    cancel event so stage/meta/gate-fix work can stop at the cutoff.
    """
    deadline = getattr(settings, "_execution_deadline", None)
    cancel_event = threading.Event() if deadline is not None else None
    killer = SerialChildKiller()
    guard = (SerialDeadlineGuard(deadline, cancel_event, stop=killer)
             if deadline is not None else None)
    with guard if guard is not None else nullcontext():
        result = execute(pipeline_path, model, provider, timeout, reuse, direction,
                         cancel_event)
        if deadline is not None and deadline.expired(time.monotonic()):
            if killer.unobserved:
                log.warning("serial cutoff: child termination unobserved; "
                            "ownership retained for a later retry")
            raise deadline_error(deadline, direction, root, pipeline_path, logfile,
                                 phase="serial_supervision", stop_requested=True)
    if killer.unobserved:
        log.warning("serial cutoff: child termination unobserved; ownership retained")
    return result


def published_outcome(factory, settings, direction, root, pipeline_path, logfile,
                      branch, baseline, start, status, reason, evidence, recovery,
                      probe):
    """Final cutoff check after evidence I/O; late results never publish PASS."""
    deadline = getattr(settings, "_execution_deadline", None)
    try:
        repository = probe(root, excludes=(settings.output_dir,))
        check_deadline(settings, direction, root, pipeline_path, logfile,
                       phase="publication")
    except (CampaignDeadlineExpired, CampaignGuardFailed) as exc:
        return stopped_outcome(factory, exc, direction, root, branch, baseline,
                               start, probe, deadline)
    return factory(
        direction, status, reason, time.monotonic() - start, evidence,
        commit=repository["head_commit"], baseline_commit=baseline,
        clean_commit=repository["clean_commit"],
        dirty_evidence=repository["dirty_evidence"],
        dirty_digest=repository["dirty_digest"],
        recovery_status=recovery.status, recovery_reason=recovery.reason,
        recovery_evidence=recovery.evidence)
