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

## Try it yourself

```bash
python3 code/try_intent.py "Por favor, eliminen mi cuenta"   # intent verdict + the words that decided it
python3 code/try_intent.py                                    # interactive; add --llm to compare with Claude
python3 code/explain.py case_11                               # subject, records, states, evidence, steps
python3 code/explain.py --email novak.tomasz@icloud.com --text "Please erase my account" \
    --requested 2026-09-09T12:00:00Z --audit 2026-10-01T12:00:00Z  # audit a request you make up
```

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

---

## Challenge brief

The original challenge README follows for reference. The full contract is in `problem_statement.md`.


### Ghost Data Still Exists

Build an AI agent that audits whether a user's personal data was actually deleted.

A deletion workflow may report **Complete** while data still exists in databases, logs, support attachments, exports, search indexes, archives, or backups. Sync and restore processes may also bring deleted data back.

For each request, the agent must first determine whether deletion was actually requested. It then returns `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`, reports any remaining data or recreation risk, and produces the ordered remediation and verification steps required to finish an authorized deletion safely.

Read [`problem_statement.md`](./problem_statement.md) for the complete challenge contract.

---

### Quick Start

Clone the repository and move into the project directory:

```bash
git clone https://github.com/interviewstreet/hackerrank-orchestrate-october26.git
cd hackerrank-orchestrate-october26
```

Start your AI coding tool from the **repository root** so it automatically reads [`AGENTS.md`](./AGENTS.md) and logs your session turns to `log.txt` in the root directory.

Build your solution in `code/main.py`, or use another language and document its setup and run command clearly in your README.

Your solution must:

- Read the input files from `dataset/`
- Generate one prediction row for every request in `dataset/deletion_requests.csv`
- Write the final predictions to `output.csv` in the repository root

Run the starter Python entry point with:

```bash
python3 code/main.py
```

The blank template at `dataset/output.csv` is provided as a reference. Your final generated file for submission must be the root-level `output.csv`.


### Dataset

All participant-facing files are in `dataset/`:

| File | Contents |
| --- | --- |
| `deletion_requests.csv` | 518 requests |
| `sample_deletion_requests.csv` | 32 sample requests |
| `sample_output.csv` | Correct sample answers |
| `user_identities.csv` | Current user identities |
| `user_audit_log.csv` | Historical identity changes |
| `system_activity.csv` | Activity across services and data processes |
| `deletion_rules.csv` | Required treatment for each data category |
| `evidence_files.csv` | Manifest of supplied evidence files |
| `output.csv` | Blank output template |

Evidence is stored under `dataset/evidence/` and may include PDFs, images, logs, JSON, plain text, or SQL snapshot exports. SQL captures may contain a primary database, regional replicas, a reporting replica, or a failover replica so replication drift can be investigated directly. The `.sql` files are readable as text and executable with SQLite.

Only `deletion_requests.csv` requires answers. The other files provide evidence and context.

---

### Output

Create `output.csv` with these columns in this exact order:

```text
case_id,deletion_status,steps_json
```

Produce exactly one row per request. `deletion_status` is `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`. `steps_json` is an ordered JSON array containing findings, remediation actions, and verification checks. Use an empty array (`[]`) for `NOT_APPLICABLE`. Evidence references may be a `file_id`, `activity_id`, or `audit_id`. Determine the resource state at `audit_at` from the available evidence; reported workflow statuses and operation results are claims. Use `dataset/sample_output.csv` as the format reference.

The final file must contain 518 data rows sorted by `case_id` (plus the header row). The grader parses `steps_json` as JSON, so object-key order and whitespace do not matter; array order does. Order findings, remediation, and verification in that phase order. Within a phase, follow the operation order in the problem statement, then sort by `system_id` and `resource_id`.

---

### Evaluation

`output.csv` will be compared with hidden canonical answers. Scoring considers:

- deletion assessment accuracy;
- current and historical identity resolution;
- discovery of remaining data and recreation risks;
- correct policy application and evidence references; and
- completeness and ordering of remediation and verification steps.

Your `code.zip` must include:

```text
code/evaluation/evaluation_report.md
code/evaluation/usage_report.md
```

- `evaluation_report.md` must cover case, row, field, and sequence accuracy, at least three additional evaluation cases created by you, failures found, and improvements made.
- `usage_report.md` must cover the final full-dataset run, including model providers and names, model calls, input and output tokens, total and average tokens per request, and estimated total and per-request cost. Include per-model and overall totals when using multiple models.

**Important:** Place `evaluation_report.md` and `usage_report.md` inside `code/evaluation/` (matching `code/evaluation/main.py` in this starter layout), write your complete evaluation metrics, custom evaluation test cases, and token/cost breakdowns directly inside those Markdown files, and keep prompts in source code or Markdown files. Automated code evaluation ignores raw data files (`.csv`, `.tsv`, `.json`, `.jsonl`, `.sql`, `.log`), so content stored only in those formats will not be visible to the code judge or AI interviewer.

---

### Chat Transcript

This repository includes [`AGENTS.md`](./AGENTS.md), which instructs compatible AI coding tools to append conversation summaries to `log.txt` in the repository root:

| Platform | Path |
| --- | --- |
| macOS / Linux | `<repo root>/log.txt` |
| Windows | `<repo root>\log.txt` |

- Always launch your AI coding agent from the repository root (or explicitly ask it to read `./AGENTS.md` first) so every turn is logged to `log.txt` with the required `User Prompt`, `Agent Response Summary`, `Actions`, and `Context` sections.
- Upload `log.txt` directly as your `chat_transcript` submission on HackerRank.
- Do not include API keys, tokens, credentials, or secrets in prompts, code, configuration, or `log.txt`.

---

### Packaging `code.zip` and Submission

| Item | Description |
| --- | --- |
| `code.zip` | Runnable solution, prompts, configuration, `README.md`, and `evaluation/` reports |
| `output.csv` | One answer row for each of the 518 requests in `dataset/deletion_requests.csv` |
| `chat_transcript` | The root `log.txt` file generated by your coding agent |
| AI interview | Live explanation of your architecture, approach, and evaluation |

You may use any programming language, model, framework, or retrieval approach. Include clear setup and execution instructions in `README.md` inside `code.zip`.

To package `code.zip` from the repository root without bundling the dataset:

**macOS / Linux:**

```bash
zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"
```

**Windows (PowerShell):**

```powershell
Compress-Archive -Path README.md, code -DestinationPath code.zip -Force
```

When packaging `code.zip`:

- Keep your evaluation reports in `code/evaluation/` so they are bundled automatically with `code` (or add `evaluation` to the archive command above if you placed them in a top-level `evaluation/` directory).
- Do **not** include `dataset/`, virtual environments (`.venv`, `venv`, `node_modules`), or build caches (`__pycache__`). On Windows, keep `.venv` at the repository root (outside `code/`) and remove any `code/__pycache__` folders before running `Compress-Archive`.
- Do **not** include nested archives (`.zip`, `.tar`, `.gz`, `.tgz`, `.7z`, `.rar`) inside `code.zip` — submissions containing nested archive files will be rejected.

Before submitting, confirm that:

- `output.csv` has the exact header `case_id,deletion_status,steps_json` and contains all 518 data rows sorted by `case_id`;
- every `steps_json` value is a valid JSON array with consecutive step numbers starting at `1`;
- findings cite valid evidence IDs, and `COMPLETE` verification steps cite evidence proving the current state;
- proposed remediation and future verification steps use an empty `evidence_id` (`""`);
- `code.zip` includes your runnable code, `README.md`, `code/evaluation/evaluation_report.md`, and `code/evaluation/usage_report.md`;
- `log.txt` follows the `AGENTS.md` turn format and is uploaded to the `chat_transcript` slot; and
- no API keys, credentials, real personal data, or sensitive configuration values are included.
