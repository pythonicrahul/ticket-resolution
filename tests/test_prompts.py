"""The prompt register's own contract: code loads prompts by id and version.

`prompts/README.md` makes the register the source of truth, and the decision log names a
`prompt_version` on every model call (FR-13). That only means something if the loader reads the
file, refuses a file it cannot read confidently, and fingerprints what it sent.
"""
from pathlib import Path

import pytest

from ticketing_agent.prompts import Prompt, PromptError, load

ROOT = Path(__file__).resolve().parents[1]
BLOCK = """```text
SYSTEM:
{system}

USER:
{user}
```"""


def write(directory: Path, name: str, body: str) -> Path:
    (directory / "build").mkdir(parents=True, exist_ok=True)
    path = directory / "build" / name
    path.write_text(body, encoding="utf-8")
    return path


def test_the_register_and_the_prompts_on_disk_agree():
    """Every row of prompts/README.md must name a file this loader can actually read."""
    readme = (ROOT / "prompts" / "README.md").read_text(encoding="utf-8")
    rows = [line for line in readme.splitlines() if line.startswith("| PR-")]
    assert len(rows) >= 8

    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        prompt_id, version = cells[0], cells[1]
        prompt = load(prompt_id, version, ROOT / "prompts")
        assert prompt.label == f"{prompt_id} v{version}"
        assert prompt.user_template, f"{prompt_id}: nothing would be sent"
        if cells[2].startswith("Build"):
            assert prompt.system, f"{prompt_id}: a build prompt carries its rules in SYSTEM:"
        assert cells[4].strip("`") == str(prompt.path.relative_to(ROOT))


def test_PR_01_declares_exactly_the_slots_the_drafter_fills():
    """FR-11: a prompt version declaring a new slot is one the drafter has not been taught."""
    from ticketing_agent.generate import PROMPT_ID, PROMPT_NUMBER

    prompt = load(PROMPT_ID, PROMPT_NUMBER, ROOT / "prompts")
    assert prompt.placeholders() == {"chunk_id", "title", "text", "channel", "subject", "body"}


def test_a_missing_prompt_names_the_version_that_was_wanted(tmp_path):
    with pytest.raises(PromptError, match="no prompt file for PR-99 v1.0"):
        load("PR-99", "1.0", tmp_path)


def test_two_files_for_one_version_is_refused_rather_than_guessed(tmp_path):
    body = BLOCK.format(system="rules", user="{body}")
    write(tmp_path, "PR-77_one_v1.0.md", body)
    write(tmp_path, "PR-77_two_v1.0.md", body)
    with pytest.raises(PromptError, match="matches 2 files"):
        load("PR-77", "1.0", tmp_path)


def test_a_file_with_no_prompt_block_is_refused(tmp_path):
    write(tmp_path, "PR-76_notes_v1.0.md", "# Notes only, no fenced block\n")
    with pytest.raises(PromptError, match="no ```text block"):
        load("PR-76", "1.0", tmp_path)


def test_a_block_with_half_a_role_pair_is_refused(tmp_path):
    """Half a pair means the end of the instructions cannot be told from the start of the data."""
    write(tmp_path, "PR-75_half_v1.0.md", "```text\nSYSTEM:\nrules but no user section\n```")
    with pytest.raises(PromptError, match="not both"):
        load("PR-75", "1.0", tmp_path)


def test_a_single_message_prompt_loads_as_its_whole_block(tmp_path):
    """The development and evaluation prompts carry no roles; the loader serves them too."""
    write(tmp_path, "PR-70_single_v1.0.md", "```text\nDo the thing with {input}.\n```")
    prompt = load("PR-70", "1.0", tmp_path)

    assert prompt.system == ""
    assert prompt.user_template == "Do the thing with {input}."
    assert prompt.placeholders() == {"input"}


def test_an_example_block_before_the_prompt_does_not_become_the_prompt(tmp_path):
    """A prompt file that shows a bad reply first must not have the example loaded silently."""
    write(tmp_path, "PR-74_example_v1.0.md",
          "Here is what a bad reply looks like:\n\n```text\nnot a prompt at all\n```\n\n"
          "## Prompt text\n\n" + BLOCK.format(system="the real rules", user="{body}"))

    prompt = load("PR-74", "1.0", tmp_path)
    assert prompt.system == "the real rules"


def test_two_candidate_blocks_are_refused_rather_than_guessed(tmp_path):
    write(tmp_path, "PR-73_two_v1.0.md",
          BLOCK.format(system="first", user="{body}") + "\n\n"
          + BLOCK.format(system="second", user="{body}"))
    with pytest.raises(PromptError, match="blocks with both sections"):
        load("PR-73", "1.0", tmp_path)


def test_the_fingerprint_changes_with_the_text_and_not_with_the_prose(tmp_path):
    """It is the words that were sent that must be identifiable, not the file's commentary."""
    write(tmp_path, "PR-72_a_v1.0.md", "Notes.\n\n" + BLOCK.format(system="rules", user="{body}"))
    first = load("PR-72", "1.0", tmp_path)

    write(tmp_path, "PR-72_a_v1.1.md",
          "Completely different notes.\n\n" + BLOCK.format(system="rules", user="{body}"))
    same_text = load("PR-72", "1.1", tmp_path)
    assert same_text.fingerprint == first.fingerprint

    write(tmp_path, "PR-72_a_v1.2.md", BLOCK.format(system="rules, changed", user="{body}"))
    changed = load("PR-72", "1.2", tmp_path)
    assert changed.fingerprint != first.fingerprint
    assert len(changed.fingerprint) == 16


def test_a_prompt_is_frozen_so_a_cached_one_cannot_be_edited(tmp_path):
    """`load` caches; a mutable Prompt would let one caller change another's instructions."""
    write(tmp_path, "PR-71_frozen_v1.0.md", BLOCK.format(system="rules", user="{body}"))
    prompt = load("PR-71", "1.0", tmp_path)
    assert isinstance(prompt, Prompt)
    with pytest.raises(Exception, match="frozen|immutable|cannot assign"):
        prompt.system = "different rules"  # type: ignore[misc]
