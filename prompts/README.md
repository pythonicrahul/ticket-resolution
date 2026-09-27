# Prompt register

Every prompt the project uses. Change the text → bump the version, rename the file, add a line to its change history here. Code loads prompts by id and version and writes `prompt_version` (e.g. `PR-01 v1.0`) to the decision log.

| ID | Version | Category | Serves | File |
|---|---|---|---|---|
| PR-01 | 1.0 | Build | FR-11, FR-03, FR-12 (supports FR-06) | `prompts/build/PR-01_answer_draft_v1.0.md` |
| PR-02 | 1.0 | Build | FR-01 (supports FR-05) | `prompts/build/PR-02_escalation_handover_v1.0.md` |
| PR-03 | 1.0 | Build (guardrail); reused in Evaluation | FR-12, FR-11; NFR-03 (citation accuracy) | `prompts/build/PR-03_grounding_check_v1.0.md` |
| PR-04 | 1.0 | Build (experimental) | FR-10, FR-04; NFR-06 (non-fluent tickets) | `prompts/build/PR-04_query_rewrite_v1.0.md` |
| PR-05 | 1.0 | Evaluation | PRD §8 satisfaction proxy; NFR-03 | `prompts/evaluation/PR-05_answer_quality_judge_v1.0.md` |
| PR-06 | 1.0 | Specification | All FRs (one run per requirement) | `prompts/development/PR-06_spec_from_requirement_v1.0.md` |
| PR-07 | 1.0 | Specification (implementation, development only) | All FRs (one run per requirement) | `prompts/development/PR-07_implement_requirement_v1.0.md` |
| PR-08 | 1.0 | Review | All FRs; NFR-04, NFR-05 | `prompts/development/PR-08_review_against_requirement_v1.0.md` |
