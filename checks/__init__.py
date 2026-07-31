"""Engineering gates for ai-batch-runner (modeled on snaplink's checks/).

Each check is a standalone module with a `run()` entry point returning an
exit code, plus its own unittest suite (checks/test_*.py) so the harness
verifies itself. Configuration is declarative in engineering.yaml — see
checks/config.py.
"""
