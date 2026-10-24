"""Ghost Data Still Exists - deletion audit agent.

Usage (from the repository root):
    python3 code/main.py                      # audit dataset/deletion_requests.csv -> output.csv
    python3 code/main.py --llm --vlm          # also use Claude for intent + screenshot reading
    python3 code/main.py --requests dataset/sample_deletion_requests.csv --out code/evaluation/sample_predictions.csv

Pipeline:
  1. intent    : does the request text authorize deletion?  (LLM, cached; rule classifier as fallback/cross-check)
  2. evidence  : parse every evidence file (SQL, JSON, logs, text, PDF, screenshots) into observations
  3. identity  : resolve current + historical identifiers of the subject
  4. discovery : find every resource that belongs to this deletion (identifiers, catalog links, workflow activity)
  5. state     : reconstruct each copy's state at audit_at from evidence captured at/before audit_at
  6. policy    : apply deletion_rules.csv, build findings -> remediation -> verification, deterministic order
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ghost_audit.auditor import Auditor  # noqa: E402
from ghost_audit.evidence import EvidenceStore  # noqa: E402
from ghost_audit.identity import IdentityIndex  # noqa: E402
from ghost_audit.intent import LLMIntentClassifier, classify_requests  # noqa: E402
from ghost_audit.policy import Policy  # noqa: E402
from ghost_audit.usage import UsageTracker  # noqa: E402
from ghost_audit.vision import DocumentReader  # noqa: E402

REPO = os.path.dirname(HERE)


def load_csv(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build(dataset_dir, use_llm=False, use_vlm=False, cache_dir=None, extra_requests=None):
    cache_dir = cache_dir or os.path.join(HERE, "cache")
    usage = UsageTracker()
    identities = IdentityIndex(dataset_dir)
    reader = DocumentReader(identities, cache_dir, usage=usage, use_vlm=use_vlm)
    evidence = EvidenceStore(dataset_dir, vision=reader)
    policy = Policy(dataset_dir)
    # Every request in the queue shares the same activity stream; the auditor needs the full
    # list of requests (inputs only, never answers) to attribute workflow activity.
    all_requests = []
    for name in ("sample_deletion_requests.csv", "deletion_requests.csv"):
        p = os.path.join(dataset_dir, name)
        if os.path.exists(p):
            all_requests += load_csv(p)
    for q in extra_requests or []:
        if q["case_id"] not in {r["case_id"] for r in all_requests}:
            all_requests.append(q)
    llm = LLMIntentClassifier(os.path.join(cache_dir, "intent_cache.json"), usage) if use_llm else None
    intents = classify_requests(all_requests, llm)
    if llm:
        llm.save()
    authorized = {cid for cid, v in intents.items() if v["authorizes_deletion"]}
    auditor = Auditor(dataset_dir, evidence, identities, policy, all_requests, authorized)
    return auditor, intents, usage, evidence


def run(requests, auditor, intents):
    results = []
    for q in sorted(requests, key=lambda r: r["case_id"]):
        v = intents[q["case_id"]]
        results.append((q, v, auditor.audit(q, v["authorizes_deletion"])))
    return results


def write_output(results, out_path):
    rows = sorted(results, key=lambda t: t[0]["case_id"])
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for q, _, r in rows:
            w.writerow([q["case_id"], r.status, json.dumps(r.steps, ensure_ascii=False, separators=(",", ":"))])


def write_trace(results, path):
    """Per-case audit trail: intent verdict, resources considered and why."""
    with open(path, "w", encoding="utf-8") as fh:
        for q, v, r in results:
            fh.write(json.dumps({
                "case_id": q["case_id"], "user_id": r.user_id, "status": r.status,
                "intent": {k: v[k] for k in v if k in ("authorizes_deletion", "method", "request_type", "rules_agree", "score")},
                "resources": [{"system": rv.system, "resource_id": rv.resource_id, "category": rv.category,
                               "state": rv.state, "evidence_id": rv.evidence_id, "via": sorted(rv.via)}
                              for rv in sorted(r.resources, key=lambda x: (x.system, x.resource_id))],
            }, ensure_ascii=False) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=os.path.join(REPO, "dataset"))
    ap.add_argument("--requests", default=None, help="requests CSV (default: dataset/deletion_requests.csv)")
    ap.add_argument("--out", default=os.path.join(REPO, "output.csv"))
    ap.add_argument("--llm", action="store_true", help="classify intent with Claude (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--vlm", action="store_true", help="read screenshots with Claude vision (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--trace", default=os.path.join(HERE, "runs", "audit_trace.jsonl"))
    ap.add_argument("--usage", default=os.path.join(HERE, "runs", "usage.json"))
    args = ap.parse_args(argv)

    t0 = time.time()
    import shutil
    for tool, what in (("tesseract", "screenshots"), ("pdftotext", "PDF documents")):
        if not shutil.which(tool):
            print(f"WARNING: '{tool}' not found - {what} will be skipped and results will differ. "
                  f"Install it (Ubuntu: sudo apt install tesseract-ocr poppler-utils).")
    req_path = args.requests or os.path.join(args.dataset, "deletion_requests.csv")
    requests = load_csv(req_path)
    auditor, intents, usage, evidence = build(args.dataset, args.llm, args.vlm, extra_requests=requests)
    results = run(requests, auditor, intents)
    write_output(results, args.out)
    os.makedirs(os.path.dirname(args.trace), exist_ok=True)
    write_trace(results, args.trace)
    usage.dump(args.usage, len(requests))
    from collections import Counter
    c = Counter(r.status for _, _, r in results)
    print(f"audited {len(results)} requests in {time.time() - t0:.1f}s -> {args.out}")
    print("status:", dict(c))
    print(f"evidence: {len(evidence.obs)} observations, {len(evidence.coverage)} captures, "
          f"{len(evidence.parse_errors)} parse errors")
    s = usage.summary(len(requests))
    print(f"model calls: {s['total_calls']}  tokens: {s['total_tokens']}  est. cost: ${s['total_cost_usd']:.4f}")


if __name__ == "__main__":
    main()
