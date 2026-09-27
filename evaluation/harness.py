"""FR-14: unattended evaluation run.

Usage:
    uv run python -m evaluation.harness --input PATH --output DIR

Takes paths as arguments (never a hardcoded file name), processes every ticket in
isolation so one failure cannot stop the run, and writes a metrics report.
"""
