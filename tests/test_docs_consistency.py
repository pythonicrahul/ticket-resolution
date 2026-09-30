"""R1, R9: the documents an assessor follows must not contradict the code or each other.

These are not unit tests of a requirement; they are the checks that stop the README,
`.env.example` and `docs/decisions.md` drifting apart between sessions. Every one of them
exists because the three had already drifted (review rows R1 and R9).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


def test_T_R1_1_the_readme_does_not_promise_a_test_count(readme: str) -> None:
    """R1: a number that goes stale the next time a test is added is worse than no number.

    The README said 469 while the suite held 505. An assessor who runs the suite and counts
    a different number has been given a reason to distrust everything else in the file.
    """
    # "469 tests", "505 tests", "504 passing tests" ... in any of the forms we have used.
    stale = re.findall(r"\b\d{2,}\s+(?:\w+\s+)?tests?\b", readme, flags=re.IGNORECASE)
    assert not stale, f"README hardcodes a test count that will go stale: {stale}"
