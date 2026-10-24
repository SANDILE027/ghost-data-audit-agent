"""Evaluation workflow.

    python3 code/evaluation/main.py            # samples + custom cases + output contract checks
    python3 code/evaluation/main.py --write    # also (re)write code/evaluation/evaluation_report.md

1. Runs the full pipeline on dataset/sample_deletion_requests.csv and scores it
   against dataset/sample_output.csv (status, step P/R/F1, field accuracy, sequence).
2. Runs our hand-built scenarios (custom_cases.py) on a synthetic fixture dataset.
3. Validates the submission file output.csv against the output contract.
4. Compares the rule-based and LLM intent classifiers when the LLM cache is populated.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
REPO = os.path.dirname(CODE)
sys.path.insert(0, CODE)
sys.path.insert(0, HERE)

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("ghost_pipeline", os.path.join(CODE, "main.py"))
pipeline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pipeline)
from custom_cases import build_fixture, expected_with_numbers  # noqa: E402
from metrics import score, validate_output  # noqa: E402

from ghost_audit.intent import rule_intent  # noqa: E402


def load_gold(path):
    out = {}
    for r in csv.DictReader(open(path, encoding="utf-8")):
        out[r["case_id"]] = (r["deletion_status"], json.loads(r["steps_json"]))
    return out


def run_on(dataset_dir, requests_path, use_llm=False, use_vlm=False, cache_dir=None):
    reqs = pipeline.load_csv(requests_path)
    auditor, intents, usage, evidence = pipeline.build(dataset_dir, use_llm, use_vlm, cache_dir=cache_dir, extra_requests=reqs)
    results = pipeline.run(reqs, auditor, intents)
    return {q["case_id"]: (r.status, r.steps) for q, _, r in results}, intents, results


def fmt_metrics(m, title):
    lines = [f"### {title}", "", "| Metric | Value |", "|---|---|"]
    lines.append(f"| Cases | {m['cases']} |")
    lines.append(f"| Case-level status accuracy | {m['status_accuracy']:.2%} |")
    lines.append(f"| Exact case match (status + full ordered steps) | {m['case_exact_match']:.2%} |")
    p, r, f = m["step_prf_full"]
    lines.append(f"| Step-level P / R / F1 (all 6 fields incl. evidence_id) | {p:.3f} / {r:.3f} / {f:.3f} |")
    p, r, f = m["step_prf_without_evidence"]
    lines.append(f"| Step-level P / R / F1 (phase, system, resource, operation) | {p:.3f} / {r:.3f} / {f:.3f} |")
    lines.append(f"| Sequence accuracy (exact ordered list) | {m['sequence_exact']:.2%} |")
    lines.append(f"| Sequence similarity (1 - normalised edit distance) | {m['sequence_similarity']:.2%} |")
    lines += ["", "Field-level accuracy over gold steps (aligned on phase+system+resource+operation; unmatched gold steps count as wrong):", "",
              "| Field | Accuracy |", "|---|---|"]
    for k, v in m["field_accuracy"].items():
        lines.append(f"| {k} | {v:.2%} |")
    lines += ["", "Status confusion (gold -> predicted): " + ", ".join(f"`{k}`: {v}" for k, v in m["confusion"].items()), ""]
    return "\n".join(lines)


def fmt_diffs(m):
    lines = []
    for cid, pc in sorted(m["per_case"].items(), key=lambda kv: int(kv[0].split("_")[-1]) if kv[0].split("_")[-1].isdigit() else 0):
        if pc["exact"]:
            continue
        lines.append(f"- **{cid}** predicted `{pc['status']}`, gold `{pc['gold_status']}`")
        for t in pc["missing"]:
            lines.append(f"  - missing: `{' / '.join(t)}`")
        for t in pc["extra"]:
            lines.append(f"  - extra: `{' / '.join(t)}`")
    return "\n".join(lines) if lines else "_none_"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=os.path.join(REPO, "dataset"))
    ap.add_argument("--write", action="store_true", help="write evaluation_report.md")
    ap.add_argument("--llm", action="store_true")
    args = ap.parse_args(argv)
    t0 = time.time()

    # 1) public samples
    gold = load_gold(os.path.join(args.dataset, "sample_output.csv"))
    pred, intents, _ = run_on(args.dataset, os.path.join(args.dataset, "sample_deletion_requests.csv"), use_llm=args.llm)
    m_samples = score(pred, gold)

    # 2) custom scenarios
    fx = build_fixture(args.dataset)
    try:
        pred_c, _, _ = run_on(fx, os.path.join(fx, "deletion_requests.csv"), cache_dir=os.path.join(fx, "_cache"))
    finally:
        shutil.rmtree(fx, ignore_errors=True)
    gold_c = expected_with_numbers()
    m_custom = score(pred_c, gold_c)

    # 3) submission contract
    out_path = os.path.join(REPO, "output.csv")
    contract = ["output.csv not found - run code/main.py first"]
    n_rows = 0
    if os.path.exists(out_path):
        rows = list(csv.DictReader(open(out_path, encoding="utf-8")))
        n_rows = len(rows)
        reqs = pipeline.load_csv(os.path.join(args.dataset, "deletion_requests.csv"))
        files = {r["file_id"]: r for r in csv.DictReader(open(os.path.join(args.dataset, "evidence_files.csv"), encoding="utf-8"))}
        contract = validate_output(rows, [q["case_id"] for q in reqs], files, {q["case_id"]: q["audit_at"] for q in reqs})

    # 4) intent classifier agreement (rules vs. LLM) over every request
    all_reqs = pipeline.load_csv(os.path.join(args.dataset, "deletion_requests.csv")) + \
        pipeline.load_csv(os.path.join(args.dataset, "sample_deletion_requests.csv"))
    rule_na = sum(1 for q in all_reqs if not rule_intent(q["request_text"])["authorizes_deletion"])

    print(json.dumps({k: v for k, v in m_samples.items() if k != "per_case"}, indent=1))
    print("custom:", json.dumps({k: v for k, v in m_custom.items() if k != "per_case"}, indent=1))
    print("contract violations:", len(contract), contract[:5])
    print(f"done in {time.time() - t0:.1f}s")

    if args.write:
        from report_text import render
        report = render(
            samples=fmt_metrics(m_samples, "Public samples (32 cases, dataset/sample_output.csv)"),
            sample_diffs=fmt_diffs(m_samples),
            custom=fmt_metrics(m_custom, "Custom scenarios (7 cases, synthetic fixture)"),
            custom_diffs=fmt_diffs(m_custom),
            contract=contract, n_rows=n_rows, rule_na=rule_na, n_reqs=len(all_reqs),
        )
        with open(os.path.join(HERE, "evaluation_report.md"), "w", encoding="utf-8") as fh:
            fh.write(report)
        print("wrote", os.path.join(HERE, "evaluation_report.md"))


if __name__ == "__main__":
    main()
