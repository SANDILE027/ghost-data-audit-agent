# Token Usage and Cost Report

## Final full-dataset run (the run that produced `output.csv`)

Command: `python3 code/main.py`. It processed 518 requests from `dataset/deletion_requests.csv`. The 32 sample requests were loaded as inputs for workflow attribution only.

| Provider | Model | Purpose | Model calls | Input tokens | Output tokens | Total tokens | Est. cost (USD) |
|---|---|---|---|---|---|---|---|
| Anthropic | claude-haiku-4-5 | request-intent classification | 0 | 0 | 0 | 0 | $0.00 |
| Anthropic | claude-sonnet-4-5 | screenshot reading (VLM) | 0 | 0 | 0 | 0 | $0.00 |
| Local | Tesseract OCR 5 + pdftotext | screenshots / PDFs | 15 documents | n/a | n/a | n/a | $0.00 |
| **Overall** | | | **0** | **0** | **0** | **0** | **$0.00** |

* Average tokens per request: **0**
* Estimated cost per request: **$0.00**
* Wall-clock time: about 13 s on one CPU core. No GPU, no network.

The submitted `output.csv` was produced entirely by the deterministic engine. It used local OCR for the screenshots and the rule-based multilingual intent classifier. No API key was available in the build environment, and the evaluation in `evaluation_report.md` shows the engine at 100% status accuracy on the public samples without model calls.

Machine-readable usage for every run is written to `code/runs/usage.json` by `UsageTracker` (`code/ghost_audit/usage.py`). That includes calls, input and output tokens, cache hits, and cost per model and purpose.

## Optional AI paths (`--llm`, `--vlm`): measured footprint and projected cost

The model-backed components are wired in and cached by content hash under `code/cache/`. A re-run never pays twice for the same text or image.

| Component | Model (env override) | Calls for the full dataset | Input tokens (est.) | Output tokens (est.) | Est. cost |
|---|---|---|---|---|---|
| Intent (`--llm`) | `claude-haiku-4-5` (`GHOST_INTENT_MODEL`) | 398 (unique request texts among 550; duplicates are cache hits) | ~84,000 | ~12,000 | ~$0.14 |
| Screenshot reader (`--vlm`) | `claude-sonnet-4-5` (`GHOST_VLM_MODEL`) | 11 (one per screenshot) | ~20,000 | ~3,300 | ~$0.11 |
| **Overall (both enabled)** | | **409** | **~104,000** | **~15,300** | **~$0.25** (about $0.0005 per request, about 230 tokens per request) |

How these estimates were made:

* Intent prompt: system prompt of about 150 tokens plus the request text and a wrapper of about 60 tokens on average. The reply is JSON of about 30 tokens.
* VLM: one 1800x1150 screenshot of about 1,600 image tokens plus a prompt of about 200 tokens. The reply is a JSON row list of about 300 tokens.
* Prices assumed (USD per million input / output tokens): Haiku 4.5 at $1 / $5, Sonnet 4.5 at $3 / $15. They are set in `PRICING` in `code/ghost_audit/usage.py`; update them if provider pricing changes.

After a run with `--llm --vlm`, replace the first table with the measured values from `code/runs/usage.json`.

## Token-efficiency design

* Models never see whole evidence files. Parsing, identity resolution, state reconstruction, and planning are deterministic code, so cost does not grow with the evidence corpus.
* The model is used only where judgment is needed: free-text intent in seven languages, and reading screenshots. Both are batched per unique input and cached on disk.
* The rule classifier and OCR stay on as cross-checks and fallbacks. A missing key, rate limit, or bad JSON never breaks a run.
