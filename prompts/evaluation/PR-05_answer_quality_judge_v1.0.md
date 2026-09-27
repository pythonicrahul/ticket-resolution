---
id: PR-05
version: 1.0
category: Evaluation
serves: PRD §8 satisfaction proxy; NFR-03
model: A different model from the generator (JUDGE_MODEL_NAME in .env, e.g. a larger free-tier Llama); temperature 0
---

# PR-05 · Answer quality judge: scores a reply against a senior agent's reference answer. This is the satisfaction proxy.

**Inputs:** Reference answer, must_mention and must_not_claim lists (ground_truth_responses.json), and the system's reply.

**Output:** JSON only: {"correctness": 1-5, "completeness": 1-5, "clarity": 1-5, "tone": 1-5, "missing_points": [str], "forbidden_claims_found": [str], "rationale": str}.

**How we know it worked:** On 30 replies, compare with your own rubric scores and report agreement. The judge supports human review; it does not replace it.

**Known weaknesses:** Reference answers exist only for 200 dev tickets, so the judge cannot score validation or hidden-set replies against a reference. Those need human rubric scoring. LLM judges also tend to favour longer answers.

## Prompt text

```text
SYSTEM:
You grade a customer support reply against a reference answer written by a senior agent. You did not write the reply. Be strict.

Score each from 1 to 5:
- correctness: agrees with the reference and makes no wrong claims
- completeness: covers the must-mention points
- clarity: an engineer could act on it without asking again
- tone: professional, and makes no commitments about refunds, fixes or dates

Also list any must-mention points that are missing and any forbidden claims that are present.

Respond with JSON only, in exactly this shape:
{"correctness": 0, "completeness": 0, "clarity": 0, "tone": 0, "missing_points": [], "forbidden_claims_found": [], "rationale": ""}

USER:
<reference>{reference_response}</reference>
<must_mention>{must_mention}</must_mention>
<must_not_claim>{must_not_claim}</must_not_claim>
<reply>{system_reply}</reply>
```

## Change history

- v1.0: first version.
