"""Explain one case: who the subject is, which records were found, their state at audit_at,
which evidence proves it, and the resulting steps.

    python3 code/explain.py case_11
    python3 code/explain.py case_11 case_534      # several cases
    python3 code/explain.py --email someone@example.com --text "Please erase my account" \\
        --requested 2026-09-09T12:00:00Z --reported 2026-09-12T12:00:00Z --audit 2026-10-01T12:00:00Z
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("ghost_pipeline", os.path.join(HERE, "main.py"))
pipeline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pipeline)
REPO = os.path.dirname(HERE)


def explain(q, auditor, intents, evidence):
    v = intents[q["case_id"]]
    r = auditor.audit(q, v["authorizes_deletion"])
    print("=" * 78)
    print(f"{q['case_id']}  email={q['email']}  audit_at={q['audit_at']}  reported={q['reported_status']}")
    print(f"request: {q['request_text'][:160]}")
    print(f"intent : {'deletion' if v['authorizes_deletion'] else 'NOT a deletion'} ({v.get('method')})")
    if r.user_id:
        ident = auditor.ix.users[r.user_id]
        print(f"subject: {r.user_id} {ident.name} | emails: {', '.join(sorted(ident.emails))}")
    if r.resources:
        print("records found:")
        for rv in sorted(r.resources, key=lambda x: (x.system, x.resource_id)):
            target = auditor.policy.target(rv.category)
            f = evidence.files.get(rv.evidence_id, {})
            src = f.get("file_path", rv.evidence_id or "-")
            print(f"  - {rv.system}/{rv.resource_id} [{rv.category}] state={rv.state} must_be={target}")
            print(f"      evidence: {rv.evidence_id or '-'} {src}")
            print(f"      linked by: {', '.join(sorted(rv.via))}")
    print(f"STATUS : {r.status}")
    for s in r.steps:
        print(f"  {s['step_no']:>2}. {s['phase']:<12} {s['operation']:<26} {s['system_id']}/{s['resource_id']}"
              f"  {s['evidence_id']}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="*")
    ap.add_argument("--email"); ap.add_argument("--text")
    ap.add_argument("--requested"); ap.add_argument("--reported"); ap.add_argument("--audit")
    ap.add_argument("--status", default="COMPLETE")
    a = ap.parse_args()
    ds = os.path.join(REPO, "dataset")
    extra = []
    if a.email:
        extra.append({"case_id": "case_custom", "email": a.email, "requested_at": a.requested,
                      "reported_status": a.status, "reported_at": a.reported or a.requested,
                      "audit_at": a.audit, "request_text": a.text or "Please delete my account."})
    auditor, intents, _, evidence = pipeline.build(ds, extra_requests=extra)
    by_id = {q["case_id"]: q for q in auditor.requests}
    for cid in a.cases + [q["case_id"] for q in extra]:
        if cid not in by_id:
            print(f"{cid}: not found"); continue
        explain(by_id[cid], auditor, intents, evidence)


if __name__ == "__main__":
    main()
