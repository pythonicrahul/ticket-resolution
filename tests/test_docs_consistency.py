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


# --- R9: one default provider, named the same way everywhere -----------------------------

ENV_EXAMPLE = ROOT / ".env.example"
DECISIONS = ROOT / "docs" / "decisions.md"

#: The default the author chose in review row R9, with the evidence in D-55, D-46 and D-54.
DEFAULT_PROVIDER_HOST = "api.openai.com"
FREE_ALTERNATIVES = ("api.groq.com", "openrouter.ai")


def test_T_R9_1_the_readme_env_example_and_d55_name_the_same_default_provider(readme: str):
    """R9: the README said "**Groq is the provider that works**" while `.env.example` shipped
    OpenAI values and D-55 recorded the decision to pay for OpenAI.

    An assessor substitutes "a working key" and follows the README literally, so this is not a
    tidiness problem: it is the setup instruction pointing at a provider the project measured
    and rejected (21 of 80 tickets escalated without being attempted, D-54).
    """
    env = ENV_EXAMPLE.read_text(encoding="utf-8")
    decisions = DECISIONS.read_text(encoding="utf-8")

    # `.env.example` ships the default active, the alternatives commented.
    active = [ln.strip() for ln in env.splitlines()
              if ln.strip().startswith("LLM_BASE_URL=")]
    assert active == [f"LLM_BASE_URL=https://{DEFAULT_PROVIDER_HOST}/v1"], active
    for host in FREE_ALTERNATIVES:
        for line in env.splitlines():
            if host in line:
                assert line.lstrip().startswith("#"), f"{host} must be commented: {line!r}"

    # The README names the same one, as the default, before it mentions any alternative.
    assert DEFAULT_PROVIDER_HOST in readme, "the README must name the default provider's host"
    first_default = readme.index(DEFAULT_PROVIDER_HOST)
    for host in FREE_ALTERNATIVES:
        if host in readme:
            assert first_default < readme.index(host), (
                f"the README mentions {host} before the default {DEFAULT_PROVIDER_HOST}")
    assert "Groq is the provider that works" not in readme, (
        "the claim R9 exists to remove: Groq's free tier escalated 21 of 80 tickets untried")

    # And D-55 carries the reason and links its evidence.
    assert "free tiers throttled so heavily" in decisions
    for marker in ("D-46", "D-54"):
        assert marker in decisions.split("## D-55")[1].split("## D-56")[0], (
            f"D-55 must link {marker}, which is where the free-tier evidence is")


def test_T_R9_1b_the_readme_states_the_cost_and_keeps_the_free_path(readme: str):
    """R9: the Build Specification says free tiers only, so paying has to be visible.

    The README has to say what a run costs and leave the free route usable, because the whole
    point of raising it rather than quietly paying is that a reader can choose.
    """
    # In step 3 itself, not anywhere in the file: the figure also appears in the Pace section,
    # so asserting it against the whole README could not fail on one deletion.
    step3 = readme[readme.index("3. Configure:"):readme.index("4. **Train the classifier**")]
    assert "$0.03" in step3, "the cost per run has to be in the setup step that incurs it"
    assert any(host in readme for host in FREE_ALTERNATIVES), (
        "the free alternative stays documented")
    assert "escalates rather than fails" in readme or "escalate rather than fail" in readme, (
        "A11: a throttled run escalates its tickets, it does not crash")


def test_T_R9_1c_the_readme_pace_section_does_not_describe_a_removed_setting(readme: str):
    """The rate limiter was removed at the author's instruction, so nothing may document it.

    R9's own "Do" list asks for Groq's `PROVIDER_TOKENS_PER_MINUTE` /
    `PROVIDER_REQUESTS_PER_MINUTE` settings to be documented. They do not exist: the author had
    the pacing removed ("remove that throttling and all which you added"), and D-54 records
    that it was replaced by not counting a 429 against the circuit breaker. Documenting a
    setting that does not exist is worse than documenting none.
    """
    for gone in ("PROVIDER_TOKENS_PER_MINUTE", "PROVIDER_REQUESTS_PER_MINUTE"):
        assert gone not in readme, f"{gone} does not exist in the code"
    pace = readme[readme.index("**Pace.**"):]
    pace = pace[:pace.index("**Stopping it.**")]
    assert DEFAULT_PROVIDER_HOST in pace or "OpenAI" in pace, (
        "the Pace section described only the free tier's token budget")


def test_T_R9_3_the_readmes_measured_claims_match_a_recorded_run(readme: str):
    """Every number the README quotes about a run has to come from one.

    My first version of the Pace section said "two model calls for an answered ticket and a
    third for an escalated one", which implies 195 calls for the 80-ticket run. The run made
    **155**: a ticket escalated by rule never reaches the drafter and costs one call, which is
    exactly the NFR-07 property the section exists to explain. Numbers in a setup document are
    claims, and this test is what makes them checkable.
    """
    import json

    recorded = ROOT / "evaluation" / "results" / "gate-openai" / "metrics.json"
    if not recorded.exists():  # pragma: no cover - the repo ships this run
        pytest.skip("no recorded live run in the repository")
    run = json.loads(recorded.read_text(encoding="utf-8"))

    assert run["volume"]["tickets_processed"] == 80
    # The README's figures come from the 2026-10-01 --no-cache run, whose own numbers are in
    # D-68; the committed gate-openai run is the same shape (158 calls over 80 tickets).
    calls = run["governance"]["model_calls"]
    assert 1.5 < calls / 80 < 2.5, (
        f"{calls} calls over 80 tickets is {calls / 80:.2f} per ticket; the README's table has "
        f"to describe that shape, not three calls per ticket")
    assert "155 calls" in readme, "the README quotes the measured figure"
    assert "1.94 per ticket, not 3" in readme, "and says what it is not, because I got it wrong"

    for claim in ("319 s", "$0.03", "4.1 s", "6.5 s", "misses NFR-01"):
        assert claim in readme, f"the README dropped a measured claim: {claim}"


def test_T_R9_4_the_readme_names_every_env_var_its_own_steps_require(readme: str):
    """A1/NFR-09: following the README literally from a clean checkout has to work.

    Step 4 runs `train_classifier.py`, which calls `settings.require_path(...)` and refuses
    without `TRAINING_TICKETS_PATH`. Copying `.env.example` supplies it, so the documented path
    works — but a reader editing `.env` by hand had no way to know which keys were
    load-bearing, and review row R8 added a second consumer of that one.
    """
    env = ENV_EXAMPLE.read_text(encoding="utf-8")
    required = ("TRAINING_TICKETS_PATH", "DOCS_PATH")
    for name in required:
        assert f"{name}=" in env, f"{name} must be in .env.example for step 3 to be sufficient"
        assert name in readme, f"the README never names {name}, which its own step 4 requires"


def test_T_R9_5_the_only_secret_has_a_placeholder_and_a_missing_one_refuses(readme: str):
    """R9 review (high): `.env.example` had no `LLM_API_KEY=` line at all.

    Step 3 says "then set `LLM_API_KEY`" and the variable was nowhere in the file to set. A
    reader following A1 literally copied it, found nothing to fill in, and ran — and a missing
    key made every ticket escalate `provider_unavailable` with exit 0, so `metrics.md` reported
    80 of 80 escalated as though it were a result. CLAUDE.md requires placeholders here, and
    the only secret was the one without one.
    """
    from ticketing_agent.config import ConfigError, Settings

    env = ENV_EXAMPLE.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in env.splitlines() if ln.strip().startswith("LLM_API_KEY=")]
    assert lines == ["LLM_API_KEY="], lines
    assert "LLM_API_KEY" in readme

    # And an unset key is a setup error, as the classifier already is.
    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        Settings(model_name="m", llm_api_key="").require_api_key()
    assert Settings(model_name="m", llm_api_key="k").require_api_key() == "k"


def test_T_R9_6_the_grounding_judge_is_wired_to_the_model_the_documents_name(readme: str):
    """R9 review (high): `JUDGE_MODEL_NAME` was dead configuration.

    `.env.example` has carried it since D-55 with the comment "a **different** model on purpose:
    FR-12's grounding check is not independent" otherwise — and nothing read it.
    `GroundingJudge.check` passed no `model=`, so PR-03 ran on `MODEL_NAME`: every one of the
    220 recorded responses in the cache was `gpt-4o-mini`, and no run this project had made
    ever used a second model. Review row R9 then put the provider in every report, which
    published the independence claim without making it true.
    """
    from ticketing_agent.guardrails import GroundingJudge

    assert "JUDGE_MODEL_NAME" in ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "JUDGE_MODEL_NAME" in readme

    # The judge asks the model it was given, and says plainly when it has none.
    assert GroundingJudge(client=None, model="gpt-4.1-mini").model == "gpt-4.1-mini"
    assert GroundingJudge(client=None, model="").model is None
    assert GroundingJudge(client=None).model is None


def test_T_R9_7_env_example_and_the_readme_agree_about_the_cost(readme: str):
    """R9's Do list: "same wording as the README".

    `.env.example` said "roughly 250 calls at about 2k tokens each — a few tens of cents" while
    the README said 155 calls and $0.03. The repo's own recorded responses average ~800 tokens
    per call, so the README was right and a reader budgeting from `.env.example` would plan for
    ten times the real spend. Two files cannot both be cited as evidence for NFR-07.
    """
    env = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "250 calls" not in env and "tens of cents" not in env
    assert "155" in env and "$0.03" in env
    assert "$0.03" in readme


def test_T_R9_8_the_prd_records_the_amendment_it_was_told_to_record():
    """R9 review (high): the PRD still said "Zero spend: free tiers only".

    D-55 says in its own words that "the PRD revision has to record it rather than let it drift,
    because the assessment gate checks the claim" — and the drift was in the tree, with the
    source-of-truth document on the wrong side of it. `test_T_R9_1` reads the README,
    `.env.example` and D-55 and deliberately does not look at the PRD, so nothing caught it.
    """
    prd = (ROOT / "docs" / "PRD.md").read_text(encoding="utf-8")

    assert "Revision log" in prd, "an amendment buried in a table row is the drift D-55 warned of"
    nfr07 = [ln for ln in prd.splitlines() if ln.startswith("| NFR-07 ")]
    assert len(nfr07) == 1, nfr07
    assert "D-55" in nfr07[0] and "Amended" in nfr07[0]
    assert "~~Zero spend: free tiers only.~~" in nfr07[0], (
        "the original wording stays visible, struck through: an amendment that erases what it "
        "amended cannot be reviewed")
    # NFR-01's miss is recorded too, since the same gate checks it.
    assert "D-68" in prd and "measured and missed" in prd
    # And CLAUDE.md's own copy of the rule is flagged rather than silently edited.
    assert "CLAUDE.md" in prd and "author's" in prd
