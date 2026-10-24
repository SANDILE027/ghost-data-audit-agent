# Evaluation Report - Ghost Data Still Exists deletion-audit agent

All numbers in this file are produced by `python3 code/evaluation/main.py --write`
(re-run it to reproduce). The evaluation has four parts: the 32 public samples,
our own hand-built scenarios, a contract check of the submitted `output.csv`, and
the intent-classifier cross-check.

## 1. How we score

A prediction is compared with the gold answer case by case:

* **Case-level status accuracy**: the predicted `deletion_status` equals gold.
* **Step-level precision / recall / F1**: steps are matched as multisets, once on all six
  fields (`phase, system_id, resource_id, operation, state, evidence_id`) and once without
  the evidence id. This separates "found the right resource and action" from "cited the
  same evidence file".
* **Field-level accuracy**: each gold step is aligned with the predicted step that has the
  same phase+system+resource+operation, and each field is compared. Gold steps with no
  aligned prediction count as wrong on every field.
* **Sequence accuracy**: the predicted ordered step list equals the gold list exactly. We
  also report **sequence similarity** (1 - normalised Levenshtein distance over steps),
  which gives partial credit for order and content.
* **Exact case match**: status and the full ordered step list are both correct.

## 2. Results on the public samples

### Public samples (32 cases, dataset/sample_output.csv)

| Metric | Value |
|---|---|
| Cases | 32 |
| Case-level status accuracy | 100.00% |
| Exact case match (status + full ordered steps) | 90.62% |
| Step-level P / R / F1 (all 6 fields incl. evidence_id) | 0.995 / 0.985 / 0.990 |
| Step-level P / R / F1 (phase, system, resource, operation) | 0.995 / 0.985 / 0.990 |
| Sequence accuracy (exact ordered list) | 90.62% |
| Sequence similarity (1 - normalised edit distance) | 99.00% |

Field-level accuracy over gold steps (aligned on phase+system+resource+operation; unmatched gold steps count as wrong):

| Field | Accuracy |
|---|---|
| phase | 98.45% |
| system_id | 98.45% |
| resource_id | 98.45% |
| operation | 98.45% |
| state | 98.45% |
| evidence_id | 98.45% |

Status confusion (gold -> predicted): `COMPLETE->COMPLETE`: 10, `INCOMPLETE->INCOMPLETE`: 22


Remaining sample differences:

- **case_18** predicted `INCOMPLETE`, gold `INCOMPLETE`
  - missing: `VERIFICATION / security_service / audit_record_6645 / VERIFY_RECORD_PRESENT / PRESENT / `
- **case_20** predicted `INCOMPLETE`, gold `INCOMPLETE`
  - missing: `VERIFICATION / security_service / audit_record_5569 / VERIFY_RECORD_PRESENT / PRESENT / `
- **case_23** predicted `INCOMPLETE`, gold `INCOMPLETE`
  - missing: `VERIFICATION / account_service / account_410 / VERIFY_RECORD_ABSENT / ABSENT / `
  - extra: `VERIFICATION / account_service / account_7015 / VERIFY_RECORD_ABSENT / ABSENT / `

Why the remaining differences are left as they are:

* `audit_record_6645` (case_18) and `audit_record_5569` (case_20) appear nowhere in the
  participant-visible data: not in any evidence file, the activity stream, or the
  identity files. No evidence-grounded system can produce them, and inventing ids would
  violate the evidence rules.
* case_23 vs. case_547/548: three primary-profile deletions with result `FAILED` and no
  identifier at all happen in the same reporting batch for three `NOT_STARTED` requests.
  The data carries no signal that separates them, so the attribution is a 1-in-3 choice.
  We keep a deterministic rule (request order) rather than tuning to the sample.

## 3. Additional evaluation cases created by us

`code/evaluation/custom_cases.py` writes a small, fully synthetic dataset in the same
format as `dataset/` (identities, audit log, activity stream, SQL / log / JSON / text
evidence) and runs the unchanged pipeline on it. The expected answers were written by
hand from `deletion_rules.csv` and the ordering contract before running the code.

| Case | Behaviour under test | Expected |
|---|---|---|
| C1 | Request uses a **historical e-mail**; the primary profile is gone, but a **failover replica** still holds the row after a sync. A full primary capture must not "prove" the replica copy deleted. | INCOMPLETE: remaining account record (cites replica capture) -> delete -> verify |
| C2 | A search entry shows `404/miss` before `audit_at` but is present in a file **captured after `audit_at`**; the log also contains a `cache_lookup result=miss` noise line. | COMPLETE: verify absent, citing the pre-audit log only |
| C3 | "Send me an export... **I am not asking for deletion**." | NOT_APPLICABLE `[]` |
| C4 | Spanish deletion request; security audit record (**retain**), support ticket exposing e-mail (**redact**), connector still **enabled**. | INCOMPLETE: disable flow before redacting; verify retained record present |
| C5 | **Account tombstone** still holding name and e-mail; account itself gone. | INCOMPLETE: redact tombstone, keep stable reference |
| C6 | Japanese request; analytics profile with **no identifier left**, attributable only through the deletion-workflow batch (a second complete request in the same batch did not run its workflow there). | COMPLETE: verify anonymized |
| C7 | **Restorable backup** plus a customer statement document that names the export only by its **catalog alias** (`NST-...`). | INCOMPLETE: block restore before deleting; delete the export |

### Custom scenarios (7 cases, synthetic fixture)

| Metric | Value |
|---|---|
| Cases | 7 |
| Case-level status accuracy | 100.00% |
| Exact case match (status + full ordered steps) | 100.00% |
| Step-level P / R / F1 (all 6 fields incl. evidence_id) | 1.000 / 1.000 / 1.000 |
| Step-level P / R / F1 (phase, system, resource, operation) | 1.000 / 1.000 / 1.000 |
| Sequence accuracy (exact ordered list) | 100.00% |
| Sequence similarity (1 - normalised edit distance) | 100.00% |

Field-level accuracy over gold steps (aligned on phase+system+resource+operation; unmatched gold steps count as wrong):

| Field | Accuracy |
|---|---|
| phase | 100.00% |
| system_id | 100.00% |
| resource_id | 100.00% |
| operation | 100.00% |
| state | 100.00% |
| evidence_id | 100.00% |

Status confusion (gold -> predicted): `COMPLETE->COMPLETE`: 2, `INCOMPLETE->INCOMPLETE`: 4, `NOT_APPLICABLE->NOT_APPLICABLE`: 1


Differences on custom cases:

_none_

## 4. Submission contract check (`output.csv`)

`validate_output` checks the exact header, one row per request sorted by `case_id`
(518 rows), valid JSON, consecutive step numbers, allowed phase/operation pairs,
phase order, `[]` for NOT_APPLICABLE, evidence-backed verification-only COMPLETE
rows, empty evidence on future steps, findings with evidence, and that no cited file
was captured after the case's `audit_at`.

Result: **0 violations**

## 5. Intent classification cross-check

The rule classifier marks 10 of 550 requests (samples + evaluation set) as
non-deletion. All 32 samples (gold: all deletion) are classified correctly, including
follow-up requests that never say "delete" ("my old profile still appears when I
search for it..."). The 10 non-deletion requests in the evaluation set all state another
purpose: correction, export, copy of invoices, login help, marketing-only opt-out, or an
explicit "not asking for deletion". When `--llm` is used, every LLM verdict is stored
next to the rule verdict, and disagreements are flagged in `code/runs/audit_trace.jsonl`
(`rules_agree: false`) for review.

## 6. Failures found and improvements made

Exact-match progress on the 32 samples while developing: **8 -> 18 -> 24 -> 28 -> 29 / 32**.

| # | Failure observed | Root cause | Fix |
|---|---|---|---|
| 1 | Accounts and support records reported ABSENT although they existed | Log traces were merged with `cache_lookup result=miss` lines (a cache miss, not a record miss) | Infrastructure events (cache, pool, retry, checkpoint) are excluded before deriving state |
| 2 | Accounts wrongly marked deleted in many cases | A records-assurance *bundle* was treated as a whole-store capture; it is produced for specific subjects | Bundles prove absence only for subjects they contain (`scope_idents`); SQL captures are whole-store |
| 3 | Replica drift missed (case_05): a later primary capture "proved" deletion | One state per resource | State is tracked per physical copy (primary, failover / read / reporting / regional replica). Any surviving copy keeps the record present |
| 4 | Answers changed by evidence captured after the audit (case_27) | No time cut-off | State and citations use only files captured at or before `audit_at`; observations must also be timestamped at or before `audit_at` |
| 5 | Resources from a subject's earlier request leaked into a later one (case_17) | Identity-only matching | Evidence for a subject belongs to a request unless it predates the audit of that subject's previous request |
| 6 | Primary profiles and anonymized analytics with no identifier left were never found | They can only be linked through the deletion workflow | Workflow activities are attributed by owner when known; the rest are matched to the reporting batch that closes next. Accounts are paired by worker result vs. reported status (SUCCESS/COMPLETE, PARTIAL/PARTIALLY_COMPLETE, FAILED/NOT_STARTED). Anonymized analytics go to requests whose workflow ran in that batch and that are otherwise complete |
| 7 | Correct steps but wrong evidence ids | Always citing the latest observation | Cite the file that substantiates the most resources of the case (the case's evidence). For absence: scoped capture, then non-empty capture, then most recent |
| 8 | Retained security records reported ABSENT | Capture gaps were read as deletions | Records the policy says to retain are never inferred absent from a capture gap |
| 9 | OCR produced phantom ids (`support_9638`, `support_538`) and misspelled e-mails | Tesseract noise | Row-aware OCR from word boxes; ids are validated against ids corroborated elsewhere (one-character misreads snapped); e-mails snapped to known identifiers within edit distance 2 |
| 10 | A statement PDF and a reference-registry screen named no resource id | Resource known only by a business reference | Catalog aliases from `LINK_RESOURCE_IDENTITY` (`export_1842 (NST-2026-0718-1842)`). A bare customer key alias counts only on its own system's screens |
| 11 | Hand-built family accounts (`account_004`) absent from all data except a capture | Naming convention | Learned from evidence rows where `account_0NN` belongs to `user_NN`; applied only when no account was found by any other route |
| 12 | Custom case C6 initially failed: anonymized analytics went to the wrong complete request | Batch zip by request order only | Prefer requests whose deletion workflow actually ran in that batch |

## 7. Known limitations

* Identifier-less resources in a crowded batch are attributed by a deterministic rule.
  When several requests in a batch are indistinguishable, that rule can choose wrong.
* Screenshot facts depend on OCR quality unless `--vlm` is enabled. The VLM path returns
  structured rows and is cached.
* Ids that appear only in the organiser's truth (see section 2) cannot be produced.
