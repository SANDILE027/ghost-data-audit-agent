"""Evidence ingestion: turn every file listed in evidence_files.csv into
normalised Observations plus "coverage" snapshots (complete captures of a
store, which prove absence of anything not listed in them).

Nothing in here knows about cases, users or answers. It only extracts facts
that are physically present in the files."""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from .logparse import parse_file
from .model import (EMAIL_RE, HASH_RE, RESOURCE_RE, SYSTEM_CATEGORY, USER_RE,
                    Observation, norm_email, norm_name, norm_phone, system_for)

NOISE_EVENTS = {"cache_lookup", "connection_acquired", "dependency_retry", "dependency_recovered", "export_started",
                "export_completed", "checkpoint_saved", "batch_started", "consumer_lag", "thumbnail_failed",
                "cache_refresh", "lease_extended", "delivery_abandoned"}
NULLS = {None, "", "null", "None", "NULL", "-", "no direct identifier"}

# fields whose value identifies the person a record belongs to
LINK_FIELDS = {
    "email", "customer_email", "owner_email", "requester_email", "uploaded_by", "subject_reference",
    "lookup_value", "lookup_key", "match_key", "subject_key", "scan_key", "correlation_key", "owner_key",
    "subject_selector", "query", "matched_value", "subject_ref", "customer_reference", "user_id",
    "phone", "requester_phone", "owner_phone", "email_hash", "full_name", "name", "display_name",
    "requester_name", "account",
}
# direct identifiers whose presence means the record still exposes the person
DIRECT_FIELDS = {
    "email", "customer_email", "owner_email", "requester_email", "uploaded_by", "phone", "requester_phone",
    "owner_phone", "email_hash", "full_name", "name", "display_name", "requester_name",
}
REFERENCE_FIELDS = {"customer_reference", "user_id"}

COLLECTION_CATEGORY = {
    "customer_profiles": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "support_conversations": "SUPPORT_MESSAGE",
    "support_records": "SUPPORT_MESSAGE",
    "ticket_attachments": "SUPPORT_ATTACHMENT",
    "customer_export_jobs": "USER_EXPORT",
    "customer_exports": "USER_EXPORT",
    "connector_configurations": "DATA_FLOW",
    "connector_subscriptions": "DATA_FLOW",
    "backup_catalog": "BACKUP_ARCHIVE",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "security_audit_events": "SECURITY_AUDIT_LOG",
    "security_audit_records": "SECURITY_AUDIT_LOG",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
    "retained_order_records": "LEGAL_HOLD_RECORD",
}
COLLECTION_SYSTEM = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}


@dataclass
class Coverage:
    """A complete capture of one store at one instant (absence is provable)."""
    file_id: str
    file_captured_at: str
    time: str
    system: str
    present: set = field(default_factory=set)
    table: str = ""
    copy: str = "primary"
    scope_idents: set | None = None    # None = whole store; else only subjects with these identifiers


def _copy_name(table: str) -> str:
    m = re.search(r"_(failover_replica|read_replica|reporting_replica|replica_\w+)$", table)
    return m.group(1) if m else "primary"


def _truthy(v) -> bool | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "allowed", "enabled", "permitted"):
        return True
    if s in ("false", "0", "no", "denied", "disabled", "blocked"):
        return False
    return None


def _clean(v):
    if isinstance(v, str) and v.strip() in NULLS:
        return None
    return v


def extract_idents(fields: dict) -> set:
    """Identifiers usable to link a record to a person (typed strings)."""
    out = set()
    for k, v in fields.items():
        v = _clean(v)
        if v is None or isinstance(v, (dict, list, bool, int, float)):
            continue
        v = str(v)
        if k in LINK_FIELDS or k.endswith("_key") or k.endswith("_email"):
            for e in EMAIL_RE.findall(v):
                out.add("email:" + norm_email(e))
            for u in USER_RE.findall(v):
                out.add("user:" + u)
            for h in HASH_RE.findall(v):
                out.add("hash:" + h)
            if ("phone" in k or re.fullmatch(r"\+[\d ()\-]{7,}", v.strip())) and norm_phone(v):
                out.add("phone:" + norm_phone(v))
            if k in ("full_name", "name", "display_name", "requester_name") and not EMAIL_RE.search(v):
                out.add("name:" + norm_name(v))
        elif k in ("message_text", "message_excerpt"):
            for e in EMAIL_RE.findall(v):
                out.add("email:" + norm_email(e))
    return out


def exposed_fields(fields: dict, category: str) -> set:
    out = set()
    for k, v in fields.items():
        v = _clean(v)
        if v is None:
            continue
        if k in DIRECT_FIELDS:
            out.add(k)
        elif k in ("message_text", "message_excerpt") and (EMAIL_RE.search(str(v)) or re.search(r"\+\d[\d ()-]{6,}", str(v))):
            out.add(k)
        elif k == "matched_value" or (k == "direct_identifiers" and str(v) not in ("0", "none")):
            out.add(k)
        elif k in REFERENCE_FIELDS and category == "ANALYTICS_PROFILE":
            # an analytics profile keyed by user is still attributable to that user
            out.add(k)
    return out


def derive(system: str, fields: dict, explicit: str | None = None, category: str | None = None):
    """Return (category, state, exposed) for a record observed in `system`."""
    category = category or SYSTEM_CATEGORY.get(system, "UNKNOWN")
    if system == "support_platform" and (
        any(k in fields for k in ("file", "file_name", "storage_class"))
    ):
        category = "SUPPORT_ATTACHMENT"
    if system == "analytics_platform" and ("email_opt_in" in fields or "contact" in str(fields.get("_event", ""))):
        # a contact/opt-in row keyed only by an identifier is a secondary identifier, not a profile
        category = "SECONDARY_IDENTIFIER"
    exp = exposed_fields(fields, category)
    explicit = (explicit or "").upper() or None

    if explicit == "NOT_FOUND":
        return category, "ABSENT", exp
    if explicit in ("ACTIVE", "DISABLED", "BLOCKED"):
        return category, explicit, exp
    if explicit == "IDENTIFIABLE":
        return category, "PRESENT_PII", exp
    if explicit in ("ANONYMIZED", "REDACTED"):
        return category, "PRESENT_CLEAN", exp

    if category == "DATA_FLOW":
        en = _truthy(fields.get("enabled"))
        if en is None and explicit == "FOUND":
            en = True
        if en is not None:
            return category, "ACTIVE" if en else "DISABLED", exp
    if category == "BACKUP_ARCHIVE":
        ra = _truthy(fields.get("restore_allowed"))
        if ra is None and "eligible" in fields:
            ra = bool(_truthy(fields.get("eligible"))) and not bool(_truthy(fields.get("deletion_block")))
        if ra is None and "decision" in fields:
            ra = _truthy(fields.get("decision"))
        if ra is None and "access" in fields:
            ra = _truthy(fields.get("access"))
        if ra is not None:
            return category, "ACTIVE" if ra else "BLOCKED", exp

    # existence signals
    absent = False
    for k in ("http_status", "status_code"):
        if str(fields.get(k, "")) == "404":
            absent = True
    if str(fields.get("rows", "")) == "0" or str(fields.get("result", "")).lower() in ("miss", "not_found"):
        absent = True
    if str(fields.get("total_hits", "")) == "0":
        absent = True
    if absent:
        return category, "ABSENT", exp

    if category in ("ANALYTICS_PROFILE", "SUPPORT_MESSAGE", "ACCOUNT_TOMBSTONE", "EMAIL_DELIVERY_LOG"):
        return category, "PRESENT_PII" if exp else "PRESENT_CLEAN", exp
    return category, ("PRESENT_PII" if exp else "PRESENT"), exp


# --------------------------------------------------------------------------
class EvidenceStore:
    def __init__(self, dataset_dir: str, vision=None):
        self.dataset_dir = dataset_dir
        self.files = {}
        self.obs: list[Observation] = []
        self.coverage: list[Coverage] = []
        self.parse_errors = []
        self.vision = vision
        with open(os.path.join(dataset_dir, "evidence_files.csv"), encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                self.files[row["file_id"]] = row
        # structured files first, documents last: OCR'd ids are validated against the ids that
        # structured evidence and the activity stream already know about
        order = sorted(self.files.items(), key=lambda kv: os.path.splitext(kv[1]["file_path"])[1].lower()
                       in (".pdf", ".png", ".jpg", ".jpeg"))
        known_acts = set()
        aliases = {}
        act_path = os.path.join(dataset_dir, "system_activity.csv")
        if os.path.exists(act_path):
            with open(act_path, encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    known_acts.add(r["resource_id"])
                    m = re.search(r"linked (\S+) \((\S+)\) to", r["details"])
                    if r["event_type"] == "LINK_RESOURCE_IDENTITY" and m and m.group(1) != m.group(2):
                        # the data catalog records the business reference a resource is known by
                        aliases[m.group(2)] = m.group(1)
        if self.vision is not None:
            self.vision.aliases = aliases
        loaded_docs = False
        for fid, row in order:
            if self.vision is not None and not loaded_docs and os.path.splitext(row["file_path"])[1].lower() \
                    in (".pdf", ".png", ".jpg", ".jpeg"):
                self.vision.known_resources = known_acts | {o.resource_id for o in self.obs}
                loaded_docs = True
            path = os.path.join(dataset_dir, row["file_path"])
            try:
                self._load(fid, row, path)
            except Exception as exc:  # pragma: no cover - keep going on a bad file
                self.parse_errors.append((fid, repr(exc)))
        self.by_resource = defaultdict(list)
        for o in self.obs:
            self.by_resource[o.key()].append(o)

    # ------------------------------------------------------------------
    def add(self, fid, captured, t, system, rid, fields, explicit=None, category=None, source="",
            extra_idents=None, linked=None, copy="primary"):
        system = system if system and system not in ("unknown",) else system_for(rid)
        if not system:
            return
        if system_for(rid) and system_for(rid) != system and system not in SYSTEM_CATEGORY:
            system = system_for(rid)
        cat, state, exp = derive(system, fields, explicit, category)
        idents = extract_idents(fields) | set(extra_idents or ())
        linked = set(linked or ()) | {r for r in RESOURCE_RE.findall(json.dumps(fields, default=str)) if r != rid}
        self.obs.append(Observation(fid, captured, t or captured, system, rid, cat, state, idents, source, exp, linked,
                                    copy=copy))

    def _load(self, fid, row, path):
        captured = row["captured_at"]
        ext = os.path.splitext(path)[1].lower()
        if ext == ".json":
            self._load_json(fid, captured, path)
        elif ext == ".sql":
            self._load_sql(fid, captured, path)
        elif ext == ".log":
            self._load_log(fid, captured, path)
        elif ext == ".txt":
            text = open(path, encoding="utf-8").read()
            self._load_text_records(fid, captured, text, "text")
            if self.vision is not None:
                # free-text business documents may reference a resource only by its catalog alias
                self.obs.extend(self.vision.alias_observations(fid, row, text))
        elif ext in (".pdf", ".png", ".jpg", ".jpeg"):
            if self.vision is not None:
                for o in self.vision.observations(fid, row, path):
                    self.obs.append(o)

    # ---------------- JSON ----------------------------------------------
    def _load_json(self, fid, captured, path):
        d = json.load(open(path, encoding="utf-8"))
        if "service_exports" in d:
            for system, sec in d["service_exports"].items():
                for coll, recs in sec.items():
                    for r in recs:
                        self._service_export_record(fid, captured, system, coll, r)
            return
        if "snapshots" in d:
            scope_idents = set()
            first_cov = len(self.coverage)
            first_obs = len(self.obs)
            for snap in d["snapshots"]:
                store, coll, t = snap["data_store"], snap["collection"], snap["captured_at"]
                if coll == "operational_record_history":
                    for r in snap["records"]:
                        self._typed_record(fid, captured, r, source=f"{store}/{coll}")
                    continue
                cov = Coverage(fid, captured, t, store, set(), coll)
                for r in snap["records"]:
                    rid = r.get("resource_id")
                    if not rid:
                        continue
                    flat = dict(r)
                    if isinstance(r.get("document"), dict):
                        flat.update(r["document"])
                        flat.pop("document", None)
                    cat = COLLECTION_CATEGORY.get(coll)
                    self.add(fid, captured, t, store, rid, flat, category=cat, source=f"snapshot:{store}/{coll}")
                    cov.present.add(rid)
                self.coverage.append(cov)
            # a records-assurance bundle is captured for specific subjects: absence is only
            # provable for the subjects that the bundle was produced for.
            for o in self.obs[first_obs:]:
                scope_idents |= o.idents
            for c in self.coverage[first_cov:]:
                c.scope_idents = scope_idents
            return
        recs = d.get("records") or []
        if recs and isinstance(recs[0], dict) and "record_type" in recs[0]:
            for r in recs:
                self._typed_record(fid, captured, r, source=d.get("origin", "records"))
            return
        coll = d.get("collection", "")
        t = d.get("captured_at") or d.get("exported_at") or captured
        cov = Coverage(fid, captured, t, None, set(), coll)
        for r in recs:
            rid = r.get("resource_id")
            if not rid:
                continue
            flat = {k: v for k, v in r.items() if not isinstance(v, (dict, list))}
            cat = COLLECTION_CATEGORY.get(coll)
            self.add(fid, captured, t, system_for(rid), rid, flat, category=cat, source=f"collection:{coll}")
            cov.system = system_for(rid)
            cov.present.add(rid)
        if cov.system:
            cov.scope_idents = set().union(*(o.idents for o in self.obs if o.file_id == fid))
            self.coverage.append(cov)

    def _typed_record(self, fid, captured, r, source=""):
        rid = r.get("resource_id")
        if not rid:
            return
        subj = r.get("subject_reference") or r.get("lookup_value")
        fields = {"subject_reference": subj}
        self.add(fid, captured, r.get("recorded_at") or captured, r.get("system"), rid, fields,
                 explicit=r.get("result"), category=r.get("record_type"), source=source)
        # an explicit IDENTIFIABLE / FOUND typed record exposes the subject identifier
        o = self.obs[-1]
        if o.state in ("PRESENT_PII", "PRESENT") and subj and _clean(subj):
            if EMAIL_RE.search(str(subj)):
                o.exposed.add("email")

    def _service_export_record(self, fid, captured, system, coll, r):
        rid = r.get("resource_id")
        t = r.get("observed_at") or captured
        flat = {k: v for k, v in r.items() if not isinstance(v, (dict, list))}
        if coll == "profile_lookups":
            prof = r.get("profile")
            if prof:
                flat.update(prof)
        if coll == "index_queries":
            hits = r.get("hits") or {}
            docs = hits.get("documents") or []
            if not docs or hits.get("total") == 0:
                flat["total_hits"] = 0
            for doc in docs:
                if doc.get("resource_id") in (None, rid):
                    flat.update({k: v for k, v in doc.items() if k != "resource_id"})
        self.add(fid, captured, t, system, rid, flat, source=f"service_export:{system}/{coll}")

    # ---------------- SQL -----------------------------------------------
    def _load_sql(self, fid, captured, path):
        text = open(path, encoding="utf-8").read()
        m = re.search(r"Captured at:\s*(\S+)", text)
        snap_time = m.group(1) if m else captured
        con = sqlite3.connect(":memory:")
        con.executescript(text)
        con.row_factory = sqlite3.Row
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        scope = {}
        if "snapshot_scope" in tables:
            scope = {r[0]: r[1] for r in con.execute("select origin_system, captured_at from snapshot_scope")}
        for tbl in tables:
            if tbl == "snapshot_scope":
                continue
            rows = [dict(r) for r in con.execute(f'select * from "{tbl}"')]
            cols = [c[1] for c in con.execute(f'pragma table_info("{tbl}")')]
            if "origin_system" in cols:          # replica family: JSON payload per row
                per_sys = defaultdict(set)
                for r in rows:
                    payload = {}
                    for c in cols:
                        if c not in ("resource_id", "origin_system", "collection_name") and isinstance(r[c], str) and r[c].startswith("{"):
                            try:
                                payload = json.loads(r[c])
                            except json.JSONDecodeError:
                                pass
                    if isinstance(payload.get("document"), dict):
                        payload.update(payload.pop("document"))
                    flat = {k: v for k, v in payload.items() if not isinstance(v, (dict, list))}
                    cat = COLLECTION_CATEGORY.get(r.get("collection_name"))
                    self.add(fid, captured, snap_time, r["origin_system"], r["resource_id"], flat, category=cat,
                             source=f"sql:{tbl}", copy=_copy_name(tbl))
                    per_sys[r["origin_system"]].add(r["resource_id"])
                # a customer-data table holds every collection of the customer platform, so a
                # resource missing from it was not in that copy at capture time
                for sysname in set(COLLECTION_SYSTEM.values()) | set(per_sys):
                    self.coverage.append(Coverage(fid, captured, snap_time, sysname, per_sys.get(sysname, set()), tbl,
                                                  copy=_copy_name(tbl)))
                continue
            base = re.sub(r"_(primary|failover_replica|read_replica|reporting_replica|replica_\w+)$", "", tbl)
            system = COLLECTION_SYSTEM.get(base)
            t = snap_time
            if scope:
                if system not in scope:
                    continue
                t = scope[system]
            if system is None and rows:
                system = system_for(rows[0]["resource_id"])
            cov = Coverage(fid, captured, t, system, set(), tbl, copy=_copy_name(tbl))
            for r in rows:
                flat = {k: v for k, v in r.items() if k not in ("updated_at",)}
                if "enabled" in flat:
                    flat["enabled"] = str(flat["enabled"])
                if "restore_allowed" in flat:
                    flat["restore_allowed"] = str(flat["restore_allowed"])
                if "user_id" in flat and "customer_reference" not in flat and system == "account_service":
                    flat["customer_reference"] = flat.pop("user_id")
                self.add(fid, captured, t, system, r["resource_id"], flat,
                         category=COLLECTION_CATEGORY.get(base), source=f"sql:{tbl}", copy=_copy_name(tbl))
                cov.present.add(r["resource_id"])
            self.coverage.append(cov)

    # ---------------- logs ----------------------------------------------
    def _load_log(self, fid, captured, path):
        lines = parse_file(path)
        groups = defaultdict(list)
        for i, rec in enumerate(lines):
            f = rec["fields"]
            if rec["event"] == "record_lookup" or ("record_type" in f and "result" in f):
                self._typed_record(fid, captured, {
                    "recorded_at": rec["ts"], "system": f.get("system"), "resource_id": f.get("resource_id"),
                    "record_type": f.get("record_type"), "result": f.get("result"),
                    "subject_reference": f.get("subject_reference")}, source="log:typed")
                continue
            key = rec["trace"] if rec["trace"] and not str(rec["trace"]).startswith(("collector", "resolver", "vault", "notify")) else f"line{i}"
            groups[key].append(rec)
        for key, recs in groups.items():
            merged = {}
            rid = None
            t = None
            svc = None
            events = []
            for rec in recs:
                if rec["event"] in NOISE_EVENTS:
                    continue   # infrastructure chatter (cache misses, pool waits, retries) says nothing about the record
                if rec["event"]:
                    events.append(rec["event"])
                for k, v in rec["fields"].items():
                    if k in ("instance", "pod", "region", "attempt", "severity", "logger", "level", "requestId",
                             "function", "trace_id", "service", "event"):
                        continue
                    if k == "resource_id":
                        rid = v
                        t = rec["ts"]
                        svc = rec["service"]
                        continue
                    if k == "lookup_key" and RESOURCE_RE.fullmatch(str(v)):
                        continue
                    merged[k] = v
                if rid is None:
                    svc = svc or rec["service"]
            if not rid or not RESOURCE_RE.fullmatch(rid):
                continue
            system = system_for(rid)
            merged["_event"] = " ".join(events)
            category = None
            if svc == "support_replica" or "contact_query" in merged["_event"]:
                # legacy contact index rows are lookup copies of the subject's identifier
                category = "SECONDARY_IDENTIFIER"
            self.add(fid, captured, t, system, rid, merged, category=category, source=f"log:{svc}")

    # ---------------- plain text (handover notes, OCR text) --------------
    LINE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(.+)")

    def _load_text_records(self, fid, captured, text, source):
        for line in text.splitlines():
            m = self.LINE_RE.search(line)
            if m:
                t, system, rid, rtype, result, subj = m.groups()
                self._typed_record(fid, captured, {"recorded_at": t, "system": system, "resource_id": rid,
                                                    "record_type": rtype, "result": result,
                                                    "subject_reference": subj.strip()}, source=source)

    # ------------------------------------------------------------------
    def stats(self):
        from collections import Counter
        return Counter((o.system, o.state) for o in self.obs)
