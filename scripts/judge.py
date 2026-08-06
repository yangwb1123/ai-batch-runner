#!/usr/bin/env python3
"""scripts/judge.py — LLM-as-judge validator wrapper (H line / N3).

Runs the configured agent on a rubric + artifact and enforces the same
VERDICT protocol as the built-in judge validators:

    VERDICT: PASS - <reasons>
    VERDICT: FAIL - <reasons>
    VERDICT: REJECT - <reasons>

A bare "VERDICT: PASS" (no reason) or a reply without any verdict fails
closed (exit 1) — a verdict must justify itself. Exit 0 = PASS, exit 1 =
FAIL/REJECT/missing verdict, so the script can be used as an ordinary
validator (exit-code semantics) or registered with `judge: true` for the
built-in verdict parsing (stdout passes through either way).

Usage:
    python scripts/judge.py -p "RUBRIC" ARTIFACT [--agent-bin BIN] [--model M] [--timeout N]

Example pi-batch.yaml entry (file-scoped, per-artifact):
    validators:
      quality_judge:
        cmd: 'python {cwd}/scripts/judge.py -p "Is this design sound? " \
             '"Answer with VERDICT: PASS|FAIL - reason." {output}'
        judge: true
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pbatch.config import AGENT_BIN, log  # noqa: E402
from pbatch.runner import judge_verdict, run_argv  # noqa: E402
from pbatch.text_io import read_text_bounded  # noqa: E402

ARTIFACT_MAX_BYTES = 64 * 1024  # rubric input budget (full artifact stays on disk)


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="LLM-as-judge validator: run the agent on a rubric + artifact "
                    "and enforce the VERDICT: PASS|FAIL - reason protocol.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-p", "--prompt", required=True,
                   help="judge rubric (the agent is asked to answer with "
                        "VERDICT: PASS|FAIL - <reason>)")
    p.add_argument("artifact", help="artifact file to judge")
    p.add_argument("--agent-bin", default="",
                   help="agent binary (default: pi-batch.yaml agent.bin)")
    p.add_argument("--model", default="", help="model override")
    p.add_argument("--timeout", type=int, default=600, help="agent timeout seconds (default: 600)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    artifact = Path(args.artifact)
    if not artifact.is_file():
        print("VERDICT: FAIL - artifact not found: %s" % args.artifact)
        return 1
    try:
        content = read_text_bounded(artifact, ARTIFACT_MAX_BYTES, "judge artifact")
    except ValueError as exc:
        print("VERDICT: FAIL - %s" % exc)
        return 1
    prompt = "%s\n\nARTIFACT (%s):\n%s" % (
        args.prompt, args.artifact, content)
    cmd = [args.agent_bin or AGENT_BIN, "-p", prompt]
    if args.model:
        cmd += ["--model", args.model]
    log.info("JUDGE: %s (artifact=%s, %d bytes)", cmd[0], args.artifact, len(content))
    result = run_argv(cmd, str(Path.cwd()), timeout=args.timeout)
    text = result.stdout
    verdict = judge_verdict(text)
    if verdict in ("FAIL", "REJECT"):
        line = next((ln.strip() for ln in text.splitlines()
                     if ln.strip().upper().startswith("VERDICT:")), "")
        print("VERDICT: %s - %s" % (verdict, line))
        return 1
    if verdict is None:
        # fail closed: no verdict or a bare one — never treat as PASS
        print("VERDICT: FAIL - judge produced no 'VERDICT: PASS|FAIL - reason' line")
        if text:
            sys.stderr.write(text[-2000:])
        return 1
    line = next((ln.strip() for ln in text.splitlines()
                 if ln.strip().upper().startswith("VERDICT:")), "")
    print(line or "VERDICT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
