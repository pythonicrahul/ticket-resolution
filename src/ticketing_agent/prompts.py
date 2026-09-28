"""Loads prompts from `prompts/` by id and version, so the register is the source of truth.

`prompts/README.md`: "Change the text → bump the version, rename the file, add a line to its
change history. Code loads prompts by id and version and writes `prompt_version` (e.g.
`PR-01 v1.0`) to the decision log."

That only holds if the code reads the file rather than carrying its own copy of the words. A
prompt pasted into a module drifts from its register entry silently, and then the decision log
names a version whose text nobody can reconstruct. Here the file is read, its `SYSTEM:` and
`USER:` halves are split, and its text is fingerprinted — so a run can prove which words were
sent, not merely which filename was intended.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
#: The fenced block holding the prompt itself, and the two roles inside it. Every `text` block is
#: considered and the one carrying a `SYSTEM:` line wins: taking the first block blindly would load
#: a bad-output example shown above the prompt, silently (row-11 review).
_BLOCK = re.compile(r"```text\n(.*?)```", re.DOTALL)
_SYSTEM = re.compile(r"^SYSTEM:\s*\n(.*?)(?=^USER:\s*$)", re.DOTALL | re.MULTILINE)
_USER = re.compile(r"^USER:\s*\n(.*)\Z", re.DOTALL | re.MULTILINE)
#: Whether a block declares a role at all. `_SYSTEM` cannot answer that: its lookahead needs a
#: `USER:` line, so a block with only a SYSTEM: marker matches neither pattern and would have
#: been read as a role-less single-message prompt — instructions and data run together.
_MARKER = re.compile(r"(?m)^(SYSTEM|USER):\s*$")


class PromptError(Exception):
    """A prompt file is missing or is not in the shape the register promises."""


@dataclass(frozen=True)
class Prompt:
    """One versioned prompt: the words that were sent, and a fingerprint of them."""

    prompt_id: str
    version: str
    system: str
    user_template: str
    fingerprint: str
    path: Path

    @property
    def label(self) -> str:
        """What FR-13 writes as `prompt_version`: `PR-01 v1.0`."""
        return f"{self.prompt_id} v{self.version}"

    def placeholders(self) -> set[str]:
        """The `{name}` slots the user template declares, for a drift check in the tests."""
        return set(re.findall(r"\{(\w+)\}", self.user_template))


@lru_cache(maxsize=16)
def load(prompt_id: str, version: str, directory: Path | None = None) -> Prompt:
    """The prompt with this id and version, or a `PromptError` naming what was wrong."""
    root = directory or PROMPTS_DIR
    matches = sorted(root.glob(f"*/{prompt_id}_*_v{version}.md"))
    if not matches:
        raise PromptError(
            f"no prompt file for {prompt_id} v{version} under {root}. Prompts are versioned "
            "files (prompts/README.md): changing the text means a new file, not an edit.")
    if len(matches) > 1:
        raise PromptError(f"{prompt_id} v{version} matches {len(matches)} files: {matches}")

    path = matches[0]
    text = path.read_text(encoding="utf-8")
    blocks = _BLOCK.findall(text)
    if not blocks:
        raise PromptError(f"{path} has no ```text block, so there is no prompt to send")

    # Two shapes are in the register. The build prompts the system sends carry SYSTEM: and USER:
    # sections; the development and evaluation prompts are a single instruction with no roles,
    # and refusing those would make the loader unusable for most of its own register.
    roles = [{m.group(1) for m in _MARKER.finditer(b)} for b in blocks]
    paired = [b for b, found in zip(blocks, roles, strict=True) if found == {"SYSTEM", "USER"}]
    half = [b for b, found in zip(blocks, roles, strict=True) if len(found) == 1]
    if len(paired) > 1:
        raise PromptError(
            f"{path} has {len(paired)} blocks with both sections; which one is the prompt "
            "cannot be guessed")
    if paired:
        body = paired[0]
    elif half:
        raise PromptError(
            f"{path} has a SYSTEM: or a USER: marker but not both, so where the instructions "
            "end cannot be told")
    elif len(blocks) == 1:
        body = blocks[0]
    else:
        raise PromptError(
            f"{path} has {len(blocks)} prompt blocks and no roles to tell them apart")

    system, user = _SYSTEM.search(body), _USER.search(body)
    if not user:
        # A single-message prompt: the whole block is what gets sent.
        return Prompt(prompt_id=prompt_id, version=version, system="",
                      user_template=body.strip(),
                      fingerprint=hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
                      path=path)

    return Prompt(
        prompt_id=prompt_id,
        version=version,
        system=system.group(1).strip(),
        user_template=user.group(1).strip(),
        # Over the block only: the surrounding notes are documentation, and a typo fixed in them
        # should not read as a changed prompt.
        fingerprint=hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
        path=path,
    )
