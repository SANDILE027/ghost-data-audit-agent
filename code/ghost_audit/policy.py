"""Policy engine: deletion_rules.csv -> required treatment per data category,
and the step vocabulary / ordering contract from the problem statement."""
from __future__ import annotations

import csv
import os

FINDING_ORDER = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"]
REMEDIATION_ORDER = ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"]
VERIFICATION_ORDER = ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                      "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"]
OP_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
OP_RANK = {op: i for i, op in enumerate(FINDING_ORDER + REMEDIATION_ORDER + VERIFICATION_ORDER)}

# Treatment -> target end state
ACTION_TARGET = {"DELETE": "ABSENT", "REDACT": "REDACTED", "ANONYMIZE": "ANONYMIZED", "RETAIN": "PRESENT"}
# Categories that are processes rather than records (not in deletion_rules.csv)
PROCESS_TARGET = {"DATA_FLOW": "DISABLED"}


class Policy:
    def __init__(self, dataset_dir: str):
        self.rules = {}
        with open(os.path.join(dataset_dir, "deletion_rules.csv"), encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                self.rules[r["data_category"]] = r

    def target(self, category: str) -> str | None:
        """Required end state for a record of this category."""
        if category in PROCESS_TARGET:
            return PROCESS_TARGET[category]
        rule = self.rules.get(category)
        if not rule:
            return None
        action = rule["required_action"].upper()
        if category == "BACKUP_ARCHIVE" and action == "RETAIN":
            # retain the archive but restore access for the subject must be blocked
            return "BLOCKED"
        return ACTION_TARGET.get(action)

    def rule_id(self, category: str) -> str | None:
        r = self.rules.get(category)
        return r["rule_id"] if r else None


def sort_steps(steps: list[dict]) -> list[dict]:
    steps = sorted(steps, key=lambda s: (PHASE_ORDER[s["phase"]], OP_RANK[s["operation"]], s["system_id"], s["resource_id"]))
    out = []
    seen = set()
    for s in steps:
        k = (s["phase"], s["operation"], s["system_id"], s["resource_id"])
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    for i, s in enumerate(out, 1):
        s["step_no"] = i
    return [{"step_no": s["step_no"], "phase": s["phase"], "system_id": s["system_id"], "resource_id": s["resource_id"],
             "operation": s["operation"], "state": OP_STATE[s["operation"]], "evidence_id": s.get("evidence_id", "")}
            for s in out]
