# Ghost Data Still Exists: Deletion-Audit Agent

This agent proves whether a customer's personal data is actually gone. For every request in `dataset/deletion_requests.csv` it decides `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`, and emits the ordered findings, remediation, and verification steps, each backed by evidence.

## Quick start

```bash
# from the repository root; Python 3.10+
pip install -r code/requirements.txt          # optional: only needed for --llm / --vlm
sudo apt-get install tesseract-ocr poppler-utils   # OCR for screenshots, pdftotext for PDFs (macOS: brew install tesseract poppler)

python3 code/main.py                          # -> output.csv (518 rows) + code/runs/{audit_trace.jsonl,usage.json}
python3 code/evaluation/main.py --write       # samples + custom cases + contract check -> evaluation_report.md
```

To use the optional AI paths, set `ANTHROPIC_API_KEY` in the environment (never in files) and run:

```bash
python3 code/main.py --llm --vlm
```

`--llm` classifies request intent with Claude, and `--vlm` reads screenshots with Claude vision. Both are cached in `code/cache/` and their token usage is recorded.

The deterministic engine needs only the Python standard library. Tesseract and pdftotext are used for the 15 screenshot and PDF evidence files. Without them, those files are skipped and everything else still runs.

## Architecture

```
request ──► 1 intent ──► NOT_APPLICABLE []      (rules + optional LLM, multilingual, negation-aware)
              │ authorizes deletion
              ▼
   2 evidence store      every file in evidence_files.csv -> normalised Observations + Captures
              │           SQL (sqlite3, per table and per physical copy), 8 log dialects (trace-merged),
              │           JSON exports and bundles, text handovers, PDFs (pdftotext), screenshots (row-aware OCR / VLM)
   3 identity            current snapshot + audit log -> all emails / phones / sha256(email) / user_id
   4 discovery           resources linked by identifiers, data-catalog links and aliases,
              │           and deletion-workflow activity (batch attribution for identifier-less records)
   5 state @ audit_at    latest citable observation per copy (primary, failover, read, reporting, regional replicas);
              │           captures prove absence (scoped bundles only for their subjects); nothing captured after audit_at
   6 policy + planner    deletion_rules.csv -> target state per category; findings -> remediation -> verification,
                          canonical ordering, evidence choice (the file that substantiates the most resources of the case)
```

| Module | Responsibility |
|---|---|
| `ghost_audit/logparse.py` | One parser for logfmt, JSON, syslog, pipe, bracket, and cloud-runtime (escaped message) logs |
| `ghost_audit/evidence.py` | Observations and captures from all structured evidence, state derivation (`derive`), noise filtering |
| `ghost_audit/vision.py` | PDFs and screenshots: row-aware OCR, id validation, e-mail snapping, catalog aliases, optional VLM |
| `ghost_audit/identity.py` | Identity graph with historical identifiers |
| `ghost_audit/auditor.py` | Discovery, workflow attribution, per-copy state reconstruction, citations, plan |
| `ghost_audit/policy.py` | Rule table -> target state; step vocabulary and canonical ordering |
| `ghost_audit/intent.py` | Rule classifier and LLM classifier (prompt in `SYSTEM_PROMPT`), with cache |
| `ghost_audit/usage.py` | Token and cost accounting |
| `evaluation/` | Metrics, custom scenarios, report generator, `evaluation_report.md`, `usage_report.md` |

## Key decisions

* **Reported statuses and worker results are claims.** State comes only from evidence: lookups, captures, documents. Activities are used to know which resources a request's workflow touched, never to decide whether they are gone.
* **Time discipline.** Only files captured at or before `audit_at` are used for state and citations. Evidence about a subject belongs to the request unless it predates the audit of that subject's previous request.
* **Copies, not just records.** A deleted primary row does not erase a failover or read-replica copy. If any copy survives, the record remains.
* **Absence must be provable.** Whole-store SQL captures prove absence. Records-assurance bundles prove absence only for the subjects they were produced for. Retained records (security, legal hold) are never inferred deleted from a capture gap.
* **Policy is data.** Target states are read from `deletion_rules.csv`: delete, redact, anonymize, retain (backups retained with restore blocked), and tombstones redacted to the stable `user_id`.
* **Determinism.** No randomness; sorted iteration; canonical JSON serialization. Same input gives byte-identical `output.csv`.
* **No labels used.** `sample_output.csv` is read only by the evaluation workflow. Nothing in the pipeline is keyed to case ids or answers.

## Outputs

* `output.csv`: the submission (repository root).
* `code/runs/audit_trace.jsonl`: per case, the subject, intent verdict, every resource considered with its state, evidence, and why it was attributed. Use it to explain any answer.
* `code/runs/usage.json`: model calls, tokens, and cost for the run.
