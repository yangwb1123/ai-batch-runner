"""Bounded parallel task scheduling kept outside the main runner module."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from .config import AGENT_DEFAULT_WORKERS, log
from .metering import budget_allows
from .runner_cancel import task_cancel_event
from .parallel_plan import validate_parallel_plan


def run_parallel(tasks: list, workers: int, validate_cmd: str,
                 retries: int, retry_delay: float, backoff: float,
                 min_interval: float, waves: Optional[list], cancel_event,
                 run_retried, wait_for_cancel, kill_active_procs,
                 cancelled_result, budget_message, result_callback=None) -> list:
    wave_plan = validate_parallel_plan(tasks, waves)
    if waves is not None and not wave_plan:
        return []
    workers = workers or AGENT_DEFAULT_WORKERS
    total = len(tasks)
    log.info("PARALLEL x%d  (%d tasks)", workers or AGENT_DEFAULT_WORKERS,
             len(tasks))
    batches = ([list(range(total))] if waves is None
               else [[int(node_id) for node_id in wave] for wave in wave_plan])
    results, capped = _run_batches(
        batches, tasks, workers, total, validate_cmd, retries, retry_delay,
        backoff, min_interval, cancel_event, run_retried, wait_for_cancel,
        kill_active_procs, cancelled_result, budget_message, result_callback)
    if capped:
        raise SystemExit(3)
    return results


def _run_batches(batches, tasks, workers, total, validate_cmd, retries,
                 retry_delay, backoff, min_interval, cancel_event,
                 run_retried, wait_for_cancel, kill_active_procs,
                 cancelled_result, budget_message, result_callback=None):
    results, capped = [], False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for position, batch in enumerate(batches):
            if _cancel_requested(cancel_event):
                results.extend(cancelled_result(tasks[index])
                              for later in batches[position:]
                              for index in later)
                break
            futures, submitted, hit_cap = _submit_batch(
                pool, batch, tasks, total, validate_cmd, retries,
                retry_delay, backoff, cancel_event, budget_message,
                run_retried)
            capped = capped or hit_cap
            wave_results, capped = _collect(
                futures, total, min_interval, capped, cancel_event,
                wait_for_cancel, kill_active_procs, budget_message,
                result_callback)
            results.extend(wave_results)
            results.extend(cancelled_result(tasks[index])
                           for index in batch if index not in submitted
                           and _cancel_requested(cancel_event))
            if capped:
                break
            if _cancel_requested(cancel_event):
                results.extend(cancelled_result(tasks[index])
                              for later in batches[position + 1:]
                              for index in later)
                break
    return results, capped


def _submit_batch(pool, batch, tasks, total, validate_cmd, retries,
                  retry_delay, backoff, cancel_event, budget_message,
                  run_retried):
    futures, submitted = {}, set()
    for index in batch:
        if _cancel_requested(cancel_event):
            break
        if not budget_allows():
            _log_budget(budget_message)
            return futures, submitted, True
        future = pool.submit(
            _run_one, run_retried, tasks[index], index + 1, total,
            validate_cmd, retries, retry_delay, backoff, cancel_event)
        futures[future] = tasks[index]
        submitted.add(index)
    return futures, submitted, False


def _run_one(run_retried, task, index, total, validate_cmd, retries,
             retry_delay, backoff, cancel_event):
    try:
        return run_retried(task, validate_cmd, index, total, retries,
                           retry_delay, backoff, parallel=True,
                           cancel_event=task_cancel_event(cancel_event, task))
    except SystemExit as exc:
        if exc.code == 3:
            return None
        raise


def _collect(futures, total, min_interval, capped, cancel_event,
             wait_for_cancel, kill_active_procs, budget_message,
             result_callback=None):
    results, successes = [], 0
    for future in as_completed(futures):
        result = future.result()
        if result is None:
            capped = True
            _log_budget(budget_message)
        else:
            if result.success:
                if successes > 0 and min_interval > 0:
                    wait_for_cancel(min_interval, cancel_event)
                successes += 1
            results.append(result)
            if result_callback is not None:
                result_callback(result)
        if capped:
            kill_active_procs()
    return results, capped


def _log_budget(message: str) -> None:
    log.error(message)


def _cancel_requested(cancel_event) -> bool:
    try:
        return bool(cancel_event is not None and cancel_event.is_set())
    except (AttributeError, TypeError):
        return False
