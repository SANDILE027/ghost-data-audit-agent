"""Hand-built evaluation cases (our own fixtures, not taken from the dataset).

Each scenario isolates one behaviour the hidden test set is likely to probe. The
fixture is a tiny, fully synthetic dataset written to a temporary directory in
the same format as `dataset/`; the real pipeline runs on it unchanged and the
output is compared with the expected answer written below from the policy.

  C1 historical e-mail + replica drift     -> INCOMPLETE (copy left in failover replica)
  C2 later evidence must be ignored        -> COMPLETE   (cites the pre-audit 404, not the later hit)
  C3 negated / non-deletion request        -> NOT_APPLICABLE []
  C4 retained + redaction + active flow    -> INCOMPLETE (disable flow before redacting)
  C5 account tombstone still exposing PII  -> INCOMPLETE (redact, keep stable reference)
  C6 identifier-less anonymized analytics  -> COMPLETE   (attributed through the workflow batch)
  C7 restore path + catalog alias document -> INCOMPLETE (block restore, delete statement export)
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile

RULES_SRC = None  # copied from the real dataset so the policy under test is the supplied one


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _csv(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def h(e):
    return hashlib.sha256(e.encode()).hexdigest()


def build_fixture(real_dataset_dir: str) -> str:
    d = tempfile.mkdtemp(prefix="ghost_custom_")
    shutil.copy(os.path.join(real_dataset_dir, "deletion_rules.csv"), os.path.join(d, "deletion_rules.csv"))
    users = [
        ("user_901", "Zanele Mthembu", "zanele.m@example.org", "+27 82 000 0901"),
        ("user_902", "Tomas Lindqvist", "tomas.l@example.org", "+46 70 000 0902"),
        ("user_903", "Ama Owusu", "ama.owusu@example.org", "+233 24 000 0903"),
        ("user_904", "Ravi Menon", "ravi.menon@example.org", "+91 90000 00904"),
        ("user_905", "Lea Novak", "lea.novak@example.org", "+386 40 000 905"),
        ("user_906", "Kenji Mori", "kenji.mori@example.org", "+81 90 0000 0906"),
        ("user_907", "Sara Haddad", "sara.haddad@example.org", "+961 3 000 907"),
    ]
    _csv(f"{d}/user_identities.csv", ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
         [(u, n, e, p, h(e), "2026-01-01T00:00:00Z") for u, n, e, p in users])
    _csv(f"{d}/user_audit_log.csv", ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"],
         [("audit_901", "user_901", "2026-02-01T00:00:00Z", "email", "zanele.old@example.org", "zanele.m@example.org")])
    reqs = [
        ("case_c1", "zanele.old@example.org", "2026-05-01T09:00:00Z", "COMPLETE", "2026-05-02T09:00:00Z", "2026-05-10T09:00:00Z",
         "Please delete my account. I used this older address before changing it."),
        ("case_c2", "tomas.l@example.org", "2026-05-01T09:00:00Z", "COMPLETE", "2026-05-02T09:00:00Z", "2026-05-10T09:00:00Z",
         "I want my account and personal data erased."),
        ("case_c3", "ama.owusu@example.org", "2026-05-01T09:00:00Z", "NOT_STARTED", "2026-05-02T09:00:00Z", "2026-05-10T09:00:00Z",
         "Could you send me an export of my data? I am not asking for deletion and want to keep using the account."),
        ("case_c4", "ravi.menon@example.org", "2026-05-01T09:00:00Z", "PARTIALLY_COMPLETE", "2026-05-02T09:00:00Z", "2026-05-10T09:00:00Z",
         "Solicito la eliminación permanente de la cuenta asociada a este correo."),
        ("case_c5", "lea.novak@example.org", "2026-05-01T09:00:00Z", "COMPLETE", "2026-05-02T09:00:00Z", "2026-05-10T09:00:00Z",
         "Please close my account. Keeping an internal deleted-account reference is fine."),
        ("case_c6", "kenji.mori@example.org", "2026-05-01T09:00:00Z", "COMPLETE", "2026-05-02T09:00:00Z", "2026-05-10T09:00:00Z",
         "このメールアドレスに関連するアカウントの完全な削除を依頼します。"),
        ("case_c7", "sara.haddad@example.org", "2026-05-01T09:00:00Z", "COMPLETE", "2026-05-02T09:00:00Z", "2026-05-10T09:00:00Z",
         "Please permanently delete my account, including any statement or backup copy."),
    ]
    _csv(f"{d}/deletion_requests.csv", ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"], reqs)
    acts = [
        ("act_01", "2026-05-02T08:05:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_9901", "SUCCESS", "Deletion worker submitted primary profile account_9901 to account_service."),
        ("act_02", "2026-05-02T08:06:00Z", "SYNC_RECORD", "account_service", "account_failover_replica", "account_9901", "SUCCESS", "Customer profile synchronization processed account_9901."),
        ("act_03", "2026-05-02T08:10:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_9906", "SUCCESS", "Deletion worker submitted primary profile account_9906 to account_service."),
        ("act_04", "2026-05-02T08:31:00Z", "ANONYMIZE_RECORD", "deletion_worker", "analytics_platform", "analytics_9906", "SUCCESS", "Policy action completed for analytics_9906."),
        ("act_05", "2026-05-01T12:00:00Z", "LINK_RESOURCE_IDENTITY", "data_catalog", "export_service", "export_9907", "SUCCESS", "Data catalog linked export_9907 (NST-2026-0501-9907) to sara.haddad@example.org and user_907."),
    ]
    _csv(f"{d}/system_activity.csv", ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id", "reported_result", "details"], acts)
    files = [
        ("ev_01", "evidence/databases/failover_20260505.sql", "customer_account_failover_replica_capture", "2026-05-05T10:00:00Z"),
        ("ev_02", "evidence/logs/readback_20260508.log", "service_readback_logs", "2026-05-08T10:00:00Z"),
        ("ev_03", "evidence/records/bundle_20260520.json", "records_assurance_snapshot_bundle", "2026-05-20T10:00:00Z"),
        ("ev_04", "evidence/records/statement_20260506.txt", "account_history_service", "2026-05-06T10:00:00Z"),
        ("ev_05", "evidence/records/handover_20260507.txt", "customer_account_service", "2026-05-07T10:00:00Z"),
    ]
    _csv(f"{d}/evidence_files.csv", ["file_id", "file_path", "origin", "captured_at"], files)
    _w(f"{d}/evidence/databases/failover_20260505.sql", """-- Captured at: 2026-05-05T10:00:00Z
-- Database topology: failover_replica
BEGIN TRANSACTION;
CREATE TABLE customer_profiles_primary (resource_id TEXT PRIMARY KEY, user_id TEXT, full_name TEXT, email TEXT, phone TEXT, account_status TEXT, updated_at TEXT);
CREATE TABLE customer_profiles_failover_replica (resource_id TEXT PRIMARY KEY, user_id TEXT, full_name TEXT, email TEXT, phone TEXT, account_status TEXT, updated_at TEXT);
INSERT INTO customer_profiles_failover_replica VALUES('account_9901','user_901','Zanele Mthembu','zanele.old@example.org','+27 82 000 0901','ACTIVE','2026-05-05T09:00:00Z');
INSERT INTO customer_profiles_primary VALUES('account_7777','user_7777','Decoy Person','decoy@example.org','+1 555 0100','ACTIVE','2026-05-05T09:00:00Z');
COMMIT;
""")
    lines = [
        "2026-05-08T08:00:00Z level=DEBUG service=search_service trace_id=t1 event=index_query_started resource_id=search_9902 lookup_key=tomas.l@example.org",
        "2026-05-08T08:00:01Z level=DEBUG service=search_service trace_id=t1 event=cache_lookup result=miss",
        "2026-05-08T08:00:02Z level=INFO service=search_service trace_id=t1 event=index_query_completed resource_id=search_9902 result=miss total_hits=0",
        "2026-05-08T08:01:00Z level=DEBUG service=account_service trace_id=t2 event=profile_lookup_started resource_id=account_9902 lookup_key=tomas.l@example.org",
        "2026-05-08T08:01:01Z level=INFO service=account_service trace_id=t2 event=profile_lookup_completed resource_id=account_9902 rows=0 http_status=404",
        "2026-05-08T08:02:00Z level=INFO service=security_service trace_id=t3 event=audit_record_read_completed resource_id=audit_record_9904 rows=1 subject_reference=user_904 event_code=ACCOUNT_DELETION_RECORDED retention_until=2032-01-01T00:00:00Z",
        "2026-05-08T08:03:00Z level=INFO service=support_platform trace_id=t4 event=support_ticket_read_completed resource_id=support_9904 customer_reference=null requester_name=null requester_email=ravi.menon@example.org requester_phone=null",
        "2026-05-08T08:04:00Z level=INFO service=integration_service trace_id=t5 event=connector_config_read_completed resource_id=data_flow_9904 enabled=true last_delivery_status=delivered connector_name=analytics_identity_bridge customer_email=ravi.menon@example.org customer_reference=null",
        "2026-05-08T08:05:00Z level=INFO service=account_service trace_id=t6 event=profile_lookup_completed resource_id=account_9904 rows=0 http_status=404 correlation_key=ravi.menon@example.org",
        "2026-05-08T08:06:00Z level=INFO service=account_service trace_id=t7 event=profile_lookup_completed resource_id=account_9905 rows=0 http_status=404 correlation_key=lea.novak@example.org",
        "2026-05-08T08:07:00Z level=INFO service=analytics_platform trace_id=t8 event=analytics_profile_read_completed resource_id=analytics_9906 customer_reference=null email=null phone=null email_hash=null audience_cohort=general_shopper",
        "2026-05-08T08:08:00Z level=INFO service=account_service trace_id=t9 event=profile_lookup_completed resource_id=account_9906 rows=0 http_status=404",
        "2026-05-08T08:09:00Z level=INFO service=backup_service trace_id=t10 event=restore_policy_read_completed resource_id=backup_9907 archive_present=true restore_allowed=true customer_email=sara.haddad@example.org customer_reference=null",
        "2026-05-08T08:10:00Z level=INFO service=account_service trace_id=t11 event=profile_lookup_completed resource_id=account_9907 rows=0 http_status=404 correlation_key=sara.haddad@example.org",
    ]
    _w(f"{d}/evidence/logs/readback_20260508.log", "\n".join(lines) + "\n")
    _w(f"{d}/evidence/records/bundle_20260520.json", json.dumps({
        "exported_at": "2026-05-20T10:00:00Z", "source": "records_assurance_service",
        "snapshots": [
            {"captured_at": "2026-05-20T10:00:00Z", "data_store": "search_service", "collection": "customer_search_documents",
             "records": [{"resource_id": "search_9902", "index_name": "customer_directory_v3",
                          "document": {"display_name": "Tomas Lindqvist", "email": "tomas.l@example.org", "customer_reference": "user_902"}}]},
            {"captured_at": "2026-05-09T10:00:00Z", "data_store": "identity_service", "collection": "deleted_customer_references",
             "records": [{"resource_id": "tombstone_9905", "subject_reference": "user_905", "name": "Lea Novak",
                          "email": "lea.novak@example.org", "phone": None, "email_hash": None, "account_status": "DELETED"}]},
        ]}, indent=1))
    # NB: the bundle file is captured 2026-05-20 (after every audit_at), so it must be ignored entirely.
    # C5 therefore needs its tombstone evidence in a pre-audit file as well:
    _w(f"{d}/evidence/records/statement_20260506.txt",
       "CUSTOMER STATEMENT NST-2026-0501-9907\nCustomer Sara Haddad, sara.haddad@example.org\nOrder ORD-1, total EUR 10.00\n")
    _w(f"{d}/evidence/records/handover_20260507.txt",
       "SHIFT HANDOVER\n- 2026-05-07T10:00:00Z | identity_service | tombstone_9905 | ACCOUNT_TOMBSTONE | IDENTIFIABLE | lea.novak@example.org\n")
    return d


# Expected answers written from the policy (deletion_rules.csv + problem statement ordering).
def E(phase, sysid, rid, op, state, ev=""):
    return {"phase": phase, "system_id": sysid, "resource_id": rid, "operation": op, "state": state, "evidence_id": ev}


EXPECTED = {
    "case_c1": ("INCOMPLETE", [
        E("FINDING", "account_service", "account_9901", "REPORT_REMAINING_RECORD", "PRESENT", "ev_01"),
        E("REMEDIATION", "account_service", "account_9901", "DELETE_RECORD", "ABSENT"),
        E("VERIFICATION", "account_service", "account_9901", "VERIFY_RECORD_ABSENT", "ABSENT")]),
    "case_c2": ("COMPLETE", [
        E("VERIFICATION", "account_service", "account_9902", "VERIFY_RECORD_ABSENT", "ABSENT", "ev_02"),
        E("VERIFICATION", "search_service", "search_9902", "VERIFY_RECORD_ABSENT", "ABSENT", "ev_02")]),
    "case_c3": ("NOT_APPLICABLE", []),
    "case_c4": ("INCOMPLETE", [
        E("FINDING", "support_platform", "support_9904", "REPORT_EXPOSED_PII", "PRESENT", "ev_02"),
        E("FINDING", "integration_service", "data_flow_9904", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE", "ev_02"),
        E("REMEDIATION", "integration_service", "data_flow_9904", "DISABLE_DATA_FLOW", "DISABLED"),
        E("REMEDIATION", "support_platform", "support_9904", "REDACT_RECORD", "REDACTED"),
        E("VERIFICATION", "account_service", "account_9904", "VERIFY_RECORD_ABSENT", "ABSENT"),
        E("VERIFICATION", "security_service", "audit_record_9904", "VERIFY_RECORD_PRESENT", "PRESENT"),
        E("VERIFICATION", "support_platform", "support_9904", "VERIFY_RECORD_REDACTED", "REDACTED"),
        E("VERIFICATION", "integration_service", "data_flow_9904", "VERIFY_DATA_FLOW_DISABLED", "DISABLED")]),
    "case_c5": ("INCOMPLETE", [
        E("FINDING", "identity_service", "tombstone_9905", "REPORT_EXPOSED_PII", "PRESENT", "ev_05"),
        E("REMEDIATION", "identity_service", "tombstone_9905", "REDACT_RECORD", "REDACTED"),
        E("VERIFICATION", "account_service", "account_9905", "VERIFY_RECORD_ABSENT", "ABSENT"),
        E("VERIFICATION", "identity_service", "tombstone_9905", "VERIFY_RECORD_REDACTED", "REDACTED")]),
    "case_c6": ("COMPLETE", [
        E("VERIFICATION", "account_service", "account_9906", "VERIFY_RECORD_ABSENT", "ABSENT", "ev_02"),
        E("VERIFICATION", "analytics_platform", "analytics_9906", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED", "ev_02")]),
    "case_c7": ("INCOMPLETE", [
        E("FINDING", "export_service", "export_9907", "REPORT_REMAINING_RECORD", "PRESENT", "ev_04"),
        E("FINDING", "backup_service", "backup_9907", "REPORT_RESTORE_RISK", "ACTIVE", "ev_02"),
        E("REMEDIATION", "backup_service", "backup_9907", "BLOCK_RESTORE", "BLOCKED"),
        E("REMEDIATION", "export_service", "export_9907", "DELETE_RECORD", "ABSENT"),
        E("VERIFICATION", "account_service", "account_9907", "VERIFY_RECORD_ABSENT", "ABSENT"),
        E("VERIFICATION", "export_service", "export_9907", "VERIFY_RECORD_ABSENT", "ABSENT"),
        E("VERIFICATION", "backup_service", "backup_9907", "VERIFY_RESTORE_BLOCKED", "BLOCKED")]),
}


def expected_with_numbers():
    out = {}
    for cid, (st, steps) in EXPECTED.items():
        out[cid] = (st, [dict(s, step_no=i) for i, s in enumerate(steps, 1)])
    return out
