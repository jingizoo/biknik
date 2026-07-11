"""Executable operational-acceptance checks (#174 PR E).

Ops-runnable smoke scripts that prove a production capability end to end — run
directly (``python -m hockey_scheduler.acceptance.<name>``) against a real
deployment, and exercised in CI by a matching test. Distinct from the unit
suite: these drive the whole store, not a single function.
"""
