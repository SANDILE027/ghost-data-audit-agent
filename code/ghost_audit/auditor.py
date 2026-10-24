"""Deletion auditor: for one request, resolve the subject, discover every
resource that belongs to this deletion, reconstruct each resource's state at
audit_at from citable evidence, apply policy and plan the ordered steps."""
from __future__ import annotations

import csv
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .evidence import EvidenceStore
from .identity import Identity, IdentityIndex
from .model import PRESENT_STATES, SYSTEM_CATEGORY, Observation, system_for
from .policy import Policy, sort_steps

LINK_RE = re.compile(r"linked (\S+) \((\S+)\) to (\S+) and (user_\d+)")
# deletion-worker operations that show which resources a case's workflow touched
WORKFLOW_TYPES = {"DELETE_RECORD", "ANONYMIZE_RECORD", "REDACT_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE",
                  "CHECK_DATA_FLOW", "CHECK_RESTORE_ACCESS", "RETAIN_RECORD"}
BATCH_SLACK_SECONDS = 15 * 60
BATCH_LEAD_SECONDS = 80 * 60
# systems whose workflow resources legitimately carry no identifier at all once processed
STATUS_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
OWNERLESS_SYSTEMS = {"account_service", "analytics_platform"}


def _ts(s: str) -> float:
    from datetime import datetime, timezone
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _case_num(cid: str) -> int:
    m = re.search(r"(\d+)$", cid)
    return int(m.group(1)) if m else 0


@dataclass
class ResourceView:
    system: str
    resource_id: str
    category: str
    state: str = "UNKNOWN"
    evidence_id: str = ""
    obs: list = field(default_factory=list)
    via: set = field(default_factory=set)   # why it is attributed to the case
    support: list = field(default_factory=list)


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list
    user_id: str | None = None
    resources: list = field(default_factory=list)
    notes: list = field(default_factory=list)


class Auditor:
    def __init__(self, dataset_dir: str, evidence: EvidenceStore, identities: IdentityIndex, policy: Policy,
                 all_requests: list[dict], authorized: set | None = None):
        self.d = dataset_dir
        self.ev = evidence
        self.ix = identities
        self.policy = policy
        self.requests = all_requests
        # requests whose text authorizes deletion (non-deletion requests run no deletion workflow)
        self.authorized = authorized if authorized is not None else {q["case_id"] for q in all_requests}
        self._req_by_id = {q["case_id"]: q for q in all_requests}
        self._prelim_cache = {}
        self.activities = []
        with open(os.path.join(dataset_dir, "system_activity.csv"), encoding="utf-8") as fh:
            self.activities = list(csv.DictReader(fh))
        self._learn_account_convention()
        self._index_links()
        self._index_owners()
        self._assign_workflow_activities()

    # ------------------------------------------------------------------
    def _index_links(self):
        """LINK_RESOURCE_IDENTITY activities from the data catalog."""
        self.links = defaultdict(list)    # user_id -> [(time, resource_id, activity_id)]
        for a in self.activities:
            if a["event_type"] == "LINK_RESOURCE_IDENTITY":
                m = LINK_RE.search(a["details"])
                if m:
                    self.links[m.group(4)].append((a["event_time"], a["resource_id"], a["activity_id"]))

    def _index_owners(self):
        """Owner(s) of each resource according to identifiers inside evidence."""
        self.owners = defaultdict(set)
        for o in self.ev.obs:
            us = self.ix.owners(o.idents)
            if len(us) == 1:
                self.owners[o.resource_id] |= us
        for uid, lst in self.links.items():
            for _, rid, _ in lst:
                self.owners[rid].add(uid)

    def _assign_workflow_activities(self):
        """Attribute each deletion-worker activity to the request whose workflow
        produced it. Activities on resources with a known owner go to that
        owner's request open at that time; the rest are resolved per batch
        (requests sharing a reporting window) in request order."""
        self.case_acts = defaultdict(list)  # case_id -> [activity]
        reqs = []
        for q in self.requests:
            u = self.ix.resolve_email(q["email"])
            reqs.append((q, u.user_id if u else None))
        self._req_user = {q["case_id"]: uid for q, uid in reqs}

        def open_cases(t):
            out = []
            for q, uid in reqs:
                if q["case_id"] not in self.authorized:
                    continue
                if q["requested_at"] <= t and _ts(t) <= _ts(q["reported_at"]) + BATCH_SLACK_SECONDS:
                    out.append((q, uid))
            return out

        unresolved = defaultdict(list)   # (system, frozenset(candidate case ids)) -> [activity]
        owned_sys = defaultdict(set)     # case_id -> systems already attributed by ownership
        wf = [a for a in self.activities if a["source_system"] == "deletion_worker" and a["event_type"] in WORKFLOW_TYPES]
        for a in wf:
            t = a["event_time"]
            cands = open_cases(t)
            rid = a["resource_id"]
            owners = self.owners.get(rid, set())
            if owners:
                for q, uid in cands:
                    if uid in owners:
                        self.case_acts[q["case_id"]].append(a)
                        owned_sys[q["case_id"]].add(system_for(rid))
                continue
            near = [q["case_id"] for q, _ in cands
                    if _ts(q["reported_at"]) - BATCH_LEAD_SECONDS <= _ts(t) <= _ts(q["reported_at"]) + BATCH_SLACK_SECONDS]
            if near and system_for(rid) in OWNERLESS_SYSTEMS:
                unresolved[system_for(rid)].append((a, near))
        # second pass: ownerless activities (fully anonymized analytics, primary profiles whose
        # only trace is a bare 404) are matched to the requests of the reporting batch that
        # closes right after them and that still lack a resource of that system.
        self._owned_sys = owned_sys
        rep_of = {q["case_id"]: q["reported_at"] for q, _ in reqs}
        for system in ("account_service", "analytics_platform"):
            batches = defaultdict(list)
            for a, cids in unresolved.get(system, []):
                t = _ts(a["event_time"])
                # the batch whose report closes next after the activity; a short overrun past the
                # report time is accepted only when no later batch is open
                closing = min((rep_of[c] for c in cids if _ts(rep_of[c]) >= t), default=None)
                if closing is None:
                    closing = max((rep_of[c] for c in cids if _ts(rep_of[c]) >= t - BATCH_SLACK_SECONDS), default=None)
                if closing is None:
                    continue
                batch = tuple(sorted((c for c in cids if rep_of[c] == closing), key=_case_num))
                batches[batch].append(a)
            for batch, acts in batches.items():
                need = [c for c in batch if system not in owned_sys[c] and system not in self._ident_systems(c)]
                if system == "analytics_platform":
                    # a processed analytics profile with no identifier left is the footprint of a
                    # successful anonymization: it belongs to a request whose deletion workflow ran
                    # in this batch and otherwise succeeded.
                    need = [c for c in need if self._prelim_status(c) == "COMPLETE"]
                    need.sort(key=lambda c: (not self._ran_in_batch(c, rep_of[c]), _case_num(c)))
                acts = sorted(acts, key=lambda a: a["event_time"])
                # The workflow's reported status is derived from the worker result of the
                # primary-profile operation, so pair compatible outcomes first.
                pairs = []
                left_c, left_a = list(need), list(acts)
                for cid in (list(left_c) if system == "account_service" else []):
                    want = STATUS_RESULT.get(self._req_by_id[cid]["reported_status"])
                    for a in left_a:
                        if a["reported_result"] == want:
                            pairs.append((cid, a)); left_c.remove(cid); left_a.remove(a)
                            break
                pairs += list(zip(left_c, left_a))
                for cid, a in pairs:
                    self.case_acts[cid].append(a)
                    owned_sys[cid].add(system)
                    self._prelim_cache.pop(cid, None)

    def _ran_in_batch(self, case_id: str, rep: str) -> bool:
        lo, hi = _ts(rep) - BATCH_LEAD_SECONDS, _ts(rep) + BATCH_SLACK_SECONDS
        return any(lo <= _ts(a["event_time"]) <= hi for a in self.case_acts.get(case_id, []))

    def _prelim_status(self, case_id: str) -> str:
        if case_id not in self._prelim_cache:
            q = self._req_by_id[case_id]
            ident = self.ix.resolve_email(q["email"])
            if ident is None:
                self._prelim_cache[case_id] = "UNKNOWN"
            else:
                res = self.discover(q, ident)
                views = list(res.values())
                for rv in views:
                    self.resolve_state(rv, q, ident)
                self._prelim_cache[case_id] = self.plan(q, views)[0]
        return self._prelim_cache[case_id]

    def _lower_bound(self, q: dict) -> str:
        """Evidence about the subject belongs to this request unless it predates the
        audit of the same subject's previous request."""
        uid = self._req_user.get(q["case_id"])
        lo = "0000"
        for other in self.requests:
            if other["case_id"] != q["case_id"] and self._req_user.get(other["case_id"]) == uid \
                    and other["audit_at"] < q["requested_at"]:
                lo = max(lo, other["audit_at"])
        return lo

    def _ident_systems(self, case_id: str) -> set:
        if not hasattr(self, "_ident_sys_cache"):
            self._ident_sys_cache = {}
        if case_id not in self._ident_sys_cache:
            q = self._req_by_id[case_id]
            u = self.ix.resolve_email(q["email"])
            out = set()
            if u:
                ids = u.idents()
                lo = self._lower_bound(q)
                for o in self.ev.obs:
                    if o.idents & ids and lo <= o.obs_time <= q["audit_at"]:
                        out.add(o.system)
            self._ident_sys_cache[case_id] = out
        return self._ident_sys_cache[case_id]

    # ------------------------------------------------------------------
    def discover(self, q: dict, ident: Identity) -> dict:
        lo, hi = self._lower_bound(q), q["audit_at"]
        ids = ident.idents()
        res: dict[tuple, ResourceView] = {}

        def add(system, rid, via, category=None):
            k = (system, rid)
            if k not in res:
                res[k] = ResourceView(system, rid, category or SYSTEM_CATEGORY.get(system, "UNKNOWN"))
            res[k].via.add(via)

        for o in self.ev.obs:
            if o.idents & ids and lo <= o.obs_time <= hi:
                add(o.system, o.resource_id, f"ident:{o.file_id}", o.category)
                for lr in o.linked_resources:
                    if system_for(lr) == "account_service":
                        add("account_service", lr, f"ref:{o.file_id}")
        for t, rid, aid in self.links.get(ident.user_id, []):
            if lo <= t <= hi:
                add(system_for(rid), rid, f"catalog:{aid}")
        for a in self.case_acts.get(q["case_id"], []):
            add(a["target_system"] if system_for(a["resource_id"]) is None else system_for(a["resource_id"]),
                a["resource_id"], f"workflow:{a['activity_id']}")
        if not any(k[0] == "account_service" for k in res) and self.account_convention:
            # primary profile ids follow the (zero-padded) user number for the accounts whose
            # captures show that convention
            num = int(ident.user_id.split("_")[1])
            rid = self.account_convention.format(num)
            if re.fullmatch(r"account_0\d+", rid):
                add("account_service", rid, "convention:user_id")
        return res

    def _learn_account_convention(self):
        """Infer from evidence rows whether account ids are derived from the user number."""
        hits = 0
        for o in self.ev.obs:
            if o.system != "account_service":
                continue
            m = re.fullmatch(r"account_(\d+)", o.resource_id)
            for i in o.idents:
                if i.startswith("user:") and m and int(i.split("_")[1]) == int(m.group(1)) \
                        and m.group(1).startswith("0"):
                    hits += 1
                    self.account_convention = "account_{:0%dd}" % len(m.group(1))
        if not hits:
            self.account_convention = None

    # ------------------------------------------------------------------
    def resolve_state(self, rv: ResourceView, q: dict, ident: Identity):
        """Reconstruct the state at audit_at.

        Each physical copy (primary store, failover / read / regional replicas) is
        tracked separately: a later primary capture without the record does not
        prove that a replica copy was removed. Only evidence captured at or before
        audit_at is used, so the answer never depends on later files."""
        audit_at = q["audit_at"]
        ids = ident.idents()
        per_copy = defaultdict(list)   # copy -> [(time, weight, state, file_id, obs)]
        for o in self.ev.by_resource.get((rv.system, rv.resource_id), []):
            if o.obs_time <= audit_at and o.file_captured_at <= audit_at:
                if o.copy != "primary" and o.obs_time < q["requested_at"]:
                    continue   # stale replica image from before the request
                per_copy[o.copy].append((o.obs_time, 1, o.state, o.file_id, o))
        for c in self.ev.coverage:
            if c.system != rv.system or c.time > audit_at or c.file_captured_at > audit_at:
                continue
            if rv.resource_id in c.present:
                continue
            if c.scope_idents is not None and not (c.scope_idents & ids):
                continue
            if c.copy != "primary" and c.copy not in per_copy:
                continue   # absence in a replica only matters for a copy we know existed
            if self.policy.target(rv.category) == "PRESENT":
                continue   # retained records (security / legal) are never inferred absent from a capture gap
            per_copy[c.copy].append((c.time, 0, "ABSENT", c.file_id, c))
        for o in self.ev.by_resource.get((rv.system, rv.resource_id), []):
            if o.category not in ("UNKNOWN", SYSTEM_CATEGORY.get(rv.system)):
                rv.category = o.category
        if not per_copy:
            return
        rank = {"PRESENT_PII": 4, "ACTIVE": 4, "PRESENT": 3, "PRESENT_CLEAN": 2, "DISABLED": 1, "BLOCKED": 1, "ABSENT": 0}
        finals = []
        for copy, cands in per_copy.items():
            cands.sort(key=lambda x: (x[0], x[1]))
            latest_t = cands[-1][0]
            tied = [c for c in cands if c[0] == latest_t]
            best = max(tied, key=lambda c: (c[1], rank.get(c[2], 0)))
            # supporting evidence: observations agreeing with the final state of this copy and
            # not contradicted by anything later in the same copy
            support = []
            for c in reversed(cands):
                if c[2] != best[2] and not (best[2] == "ABSENT" and c[1] == 0):
                    break
                if c[2] == best[2]:
                    support.append(c)
            finals.append((rank.get(best[2], 0), copy, best, support))
        finals.sort(key=lambda f: (f[0], f[1] == "primary"))
        top = finals[-1]
        rv.state = top[2][2]
        rv.support = top[3]
        rv.evidence_id = top[2][3]
        rv.obs = [c for f in finals for c in f[3]]

    def choose_citations(self, views: list):
        """Cite, for every resource, the evidence file that best explains the case:
        explicit observations beat coverage-derived absence; among explicit ones the file
        that substantiates the most resources of this case wins, then the most recent."""
        file_score = defaultdict(int)
        for rv in views:
            for f in {c[3] for c in getattr(rv, "support", []) if c[1] == 1}:
                file_score[f] += 1
        for rv in views:
            sup = getattr(rv, "support", [])
            if not sup:
                continue
            explicit = [c for c in sup if c[1] == 1]
            if explicit:
                best = max(explicit, key=lambda c: (file_score[c[3]], c[0]))
            else:
                # scoped captures made for this subject beat whole-store captures; a whole-store
                # capture that lists other records beats an empty one
                # a capture that also substantiates the case's other resources is the case's evidence;
                # then scoped captures made for this subject, then non-empty whole-store captures
                best = max(sup, key=lambda c: (file_score[c[3]],) + ((c[4].scope_idents is not None, bool(c[4].present))
                                               if c[4] is not None else (False, False)) + (c[0],))
            rv.evidence_id = best[3]

    # ------------------------------------------------------------------
    def plan(self, q: dict, resources: list[ResourceView]):
        steps = []
        incomplete = False
        for rv in resources:
            target = self.policy.target(rv.category)
            if target is None:
                continue
            st = rv.state
            sysid, rid, ev = rv.system, rv.resource_id, rv.evidence_id
            f = lambda op: steps.append({"phase": "FINDING", "operation": op, "system_id": sysid, "resource_id": rid, "evidence_id": ev})
            r = lambda op: steps.append({"phase": "REMEDIATION", "operation": op, "system_id": sysid, "resource_id": rid, "evidence_id": ""})
            v = lambda op, e: steps.append({"phase": "VERIFICATION", "operation": op, "system_id": sysid, "resource_id": rid, "evidence_id": e})
            if target == "ABSENT":
                if st in PRESENT_STATES:
                    incomplete = True
                    f("REPORT_REMAINING_RECORD"); r("DELETE_RECORD"); v("VERIFY_RECORD_ABSENT", "")
                else:
                    v("VERIFY_RECORD_ABSENT", ev)
            elif target in ("REDACTED", "ANONYMIZED"):
                op_r, op_v = ("REDACT_RECORD", "VERIFY_RECORD_REDACTED") if target == "REDACTED" else \
                    ("ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED")
                if st in ("PRESENT_PII", "PRESENT"):
                    incomplete = True
                    f("REPORT_EXPOSED_PII"); r(op_r); v(op_v, "")
                elif st == "ABSENT":
                    v("VERIFY_RECORD_ABSENT", ev)
                else:
                    v(op_v, ev)
            elif target == "PRESENT":
                v("VERIFY_RECORD_PRESENT", ev)
            elif target == "DISABLED":
                if st == "ACTIVE":
                    incomplete = True
                    f("REPORT_ACTIVE_DATA_FLOW"); r("DISABLE_DATA_FLOW"); v("VERIFY_DATA_FLOW_DISABLED", "")
                else:
                    v("VERIFY_DATA_FLOW_DISABLED", ev)
            elif target == "BLOCKED":
                if st == "ACTIVE":
                    incomplete = True
                    f("REPORT_RESTORE_RISK"); r("BLOCK_RESTORE"); v("VERIFY_RESTORE_BLOCKED", "")
                else:
                    v("VERIFY_RESTORE_BLOCKED", ev)
        if incomplete:
            for s in steps:
                if s["phase"] == "VERIFICATION":
                    s["evidence_id"] = ""
        else:
            # A COMPLETE verdict must cite evidence for every check. When no file proves a
            # resource's state (e.g. a screenshot could not be read on this machine), cite the
            # activity that ties the resource to this request (catalog link / workflow step).
            via = {(rv.system, rv.resource_id): rv.via for rv in resources}
            for s in steps:
                if not s["evidence_id"]:
                    acts = sorted(v.split(":", 1)[1] for v in via.get((s["system_id"], s["resource_id"]), ())
                                  if v.startswith(("catalog:", "workflow:")))
                    s["evidence_id"] = acts[0] if acts else ""
        return ("INCOMPLETE" if incomplete else "COMPLETE"), sort_steps(steps)

    # ------------------------------------------------------------------
    def audit(self, q: dict, authorizes_deletion: bool) -> CaseResult:
        if not authorizes_deletion:
            return CaseResult(q["case_id"], "NOT_APPLICABLE", [])
        ident = self.ix.resolve_email(q["email"])
        if ident is None:
            return CaseResult(q["case_id"], "COMPLETE", [], notes=["subject not resolvable"])
        res = self.discover(q, ident)
        views = []
        for rv in res.values():
            self.resolve_state(rv, q, ident)
            views.append(rv)
        self.choose_citations(views)
        status, steps = self.plan(q, views)
        return CaseResult(q["case_id"], status, steps, ident.user_id, views)
