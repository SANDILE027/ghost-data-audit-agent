"""Core data model shared by the evidence parsers, the auditor and the planner."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Resource namespace
# --------------------------------------------------------------------------
PREFIX_SYSTEM = [
    ("audit_record_", "security_service"),
    ("data_flow_", "integration_service"),
    ("email_delivery_", "email_delivery_service"),
    ("legal_hold_", "legal_service"),
    ("tombstone_", "identity_service"),
    ("marketing_", "marketing_platform"),
    ("analytics_", "analytics_platform"),
    ("account_", "account_service"),
    ("search_", "search_service"),
    ("support_", "support_platform"),
    ("export_", "export_service"),
    ("backup_", "backup_service"),
    ("cache_", "cache_service"),
    ("object_", "object_storage"),
]

SYSTEM_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "analytics_platform": "ANALYTICS_PROFILE",
    "support_platform": "SUPPORT_MESSAGE",
    "export_service": "USER_EXPORT",
    "integration_service": "DATA_FLOW",
    "backup_service": "BACKUP_ARCHIVE",
    "security_service": "SECURITY_AUDIT_LOG",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "legal_service": "LEGAL_HOLD_RECORD",
    "marketing_platform": "MARKETING_PROFILE",
    "cache_service": "CACHE_ENTRY",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "object_storage": "SUPPORT_ATTACHMENT",
}

RESOURCE_RE = re.compile(
    r"\b(audit_record_\d+|data_flow_\d+|email_delivery_\d+|legal_hold_\d+|tombstone_\d+|"
    r"marketing_\d+|analytics_\d+|account_\d+|search_\d+|support_\d+|export_\d+|backup_\d+|cache_\d+)\b"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")


def system_for(resource_id: str) -> str | None:
    for prefix, system in PREFIX_SYSTEM:
        if resource_id.startswith(prefix):
            return system
    return None


def norm_phone(p: str) -> str:
    return re.sub(r"\D", "", p or "")


def norm_email(e: str) -> str:
    return (e or "").strip().lower()


def norm_name(n: str) -> str:
    return re.sub(r"\s+", " ", (n or "").strip().lower())


# --------------------------------------------------------------------------
# States
# --------------------------------------------------------------------------
# PRESENT_PII   record exists and still carries direct identifiers / is attributable
# PRESENT       record exists (no field-level detail available)
# PRESENT_CLEAN record exists without prohibited identifiers (redacted/anonymized)
# ABSENT        record verified not to exist
# ACTIVE / DISABLED  data-flow state;  ACTIVE / BLOCKED  restore state
PRESENT_STATES = {"PRESENT_PII", "PRESENT", "PRESENT_CLEAN"}


@dataclass
class Observation:
    file_id: str
    file_captured_at: str
    obs_time: str
    system: str
    resource_id: str
    category: str
    state: str
    idents: set = field(default_factory=set)   # identifiers that link the record to a person
    source: str = ""                            # human readable provenance (file kind / table / trace)
    exposed: set = field(default_factory=set)   # direct identifiers still visible in the record
    linked_resources: set = field(default_factory=set)  # other resources referenced by this record
    weight: int = 1                             # 1 explicit observation, 0 coverage-derived absence
    copy: str = "primary"                       # physical copy (primary or a named replica)

    def key(self):
        return (self.system, self.resource_id)
