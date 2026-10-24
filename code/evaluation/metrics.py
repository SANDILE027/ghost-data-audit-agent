"""Scoring utilities shared by the evaluation workflow."""
from __future__ import annotations

import json
from collections import Counter

FIELDS = ["phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def _key(s):
    return (s["system_id"], s["resource_id"], s["operation"])


def _full(s):
    return tuple(s[f] for f in FIELDS)


def levenshtein(a: list, b: list) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def score(pred: dict, gold: dict) -> dict:
    """pred/gold: case_id -> (status, steps list)."""
    n = len(gold)
    status_ok = 0
    tp_full = fp_full = fn_full = 0
    tp_core = fp_core = fn_core = 0
    field_hits = Counter()
    field_total = 0
    seq_exact = 0
    seq_sim = 0.0
    case_exact = 0
    per_case = {}
    confusion = Counter()
    for cid, (gs, gsteps) in gold.items():
        ps, psteps = pred.get(cid, ("MISSING", []))
        confusion[(gs, ps)] += 1
        status_ok += ps == gs
        g_full, p_full = Counter(map(_full, gsteps)), Counter(map(_full, psteps))
        tp = sum((g_full & p_full).values())
        tp_full += tp
        fp_full += sum(p_full.values()) - tp
        fn_full += sum(g_full.values()) - tp
        g_core = Counter((s["phase"],) + _key(s) for s in gsteps)
        p_core = Counter((s["phase"],) + _key(s) for s in psteps)
        tpc = sum((g_core & p_core).values())
        tp_core += tpc
        fp_core += sum(p_core.values()) - tpc
        fn_core += sum(g_core.values()) - tpc
        # field-level accuracy over gold steps aligned to a predicted step with the same
        # (phase, system, resource, operation); unmatched gold steps count as misses on all fields
        pmap = {(s["phase"],) + _key(s): s for s in psteps}
        for gstep in gsteps:
            field_total += 1
            p = pmap.get((gstep["phase"],) + _key(gstep))
            if p is None:
                continue
            for f in FIELDS:
                field_hits[f] += p[f] == gstep[f]
        gseq, pseq = [_full(s) for s in gsteps], [_full(s) for s in psteps]
        exact = gseq == pseq
        seq_exact += exact
        denom = max(len(gseq), len(pseq)) or 1
        seq_sim += 1 - levenshtein(gseq, pseq) / denom
        case_exact += exact and ps == gs
        per_case[cid] = {"status": ps, "gold_status": gs, "exact": exact and ps == gs,
                         "missing": sorted(set(gseq) - set(pseq)), "extra": sorted(set(pseq) - set(gseq))}

    def prf(tp, fp, fn):
        p = tp / (tp + fp) if tp + fp else 1.0
        r = tp / (tp + fn) if tp + fn else 1.0
        f = 2 * p * r / (p + r) if p + r else 0.0
        return round(p, 4), round(r, 4), round(f, 4)

    return {
        "cases": n,
        "status_accuracy": round(status_ok / n, 4) if n else 0,
        "case_exact_match": round(case_exact / n, 4) if n else 0,
        "step_prf_full": prf(tp_full, fp_full, fn_full),
        "step_prf_without_evidence": prf(tp_core, fp_core, fn_core),
        "field_accuracy": {f: round(field_hits[f] / field_total, 4) if field_total else 0 for f in FIELDS},
        "sequence_exact": round(seq_exact / n, 4) if n else 0,
        "sequence_similarity": round(seq_sim / n, 4) if n else 0,
        "confusion": {f"{g}->{p}": c for (g, p), c in sorted(confusion.items())},
        "per_case": per_case,
    }


def validate_output(rows: list[dict], expected_ids: list[str], files: dict, audit_at: dict) -> list[str]:
    """Contract checks on an output.csv (schema, order, numbering, evidence rules)."""
    errs = []
    ids = [r["case_id"] for r in rows]
    if ids != sorted(expected_ids):
        errs.append("case ids are not exactly the sorted request ids")
    ops = {"FINDING": {"REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"},
           "REMEDIATION": {"DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"},
           "VERIFICATION": {"VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                            "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"}}
    for r in rows:
        cid = r["case_id"]
        try:
            steps = json.loads(r["steps_json"])
        except json.JSONDecodeError:
            errs.append(f"{cid}: steps_json is not JSON"); continue
        if r["deletion_status"] not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
            errs.append(f"{cid}: bad status")
        if r["deletion_status"] == "NOT_APPLICABLE" and steps:
            errs.append(f"{cid}: NOT_APPLICABLE must have []")
        for i, s in enumerate(steps, 1):
            if s.get("step_no") != i:
                errs.append(f"{cid}: step numbering"); break
            if set(s) != {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}:
                errs.append(f"{cid}: step keys"); break
            if s["operation"] not in ops.get(s["phase"], set()):
                errs.append(f"{cid}: operation/phase mismatch {s['operation']}")
            e = s["evidence_id"]
            if r["deletion_status"] == "COMPLETE" and (s["phase"] != "VERIFICATION" or not e):
                errs.append(f"{cid}: COMPLETE needs evidence-backed verification only")
            if r["deletion_status"] == "INCOMPLETE" and s["phase"] != "FINDING" and e:
                errs.append(f"{cid}: future step carries evidence")
            if r["deletion_status"] == "INCOMPLETE" and s["phase"] == "FINDING" and not e:
                errs.append(f"{cid}: finding without evidence")
            if e and e in files and files[e]["captured_at"] > audit_at[cid]:
                errs.append(f"{cid}: cites {e} captured after audit_at")
        phases = [s["phase"] for s in steps]
        order = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
        if [order[p] for p in phases] != sorted(order[p] for p in phases):
            errs.append(f"{cid}: phase order")
    return errs
