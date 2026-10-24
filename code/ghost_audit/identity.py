"""Identity resolution: current snapshot + historical audit log -> every
identifier a subject has ever had, with the time window it was valid."""
from __future__ import annotations

import csv
import hashlib
import os
from collections import defaultdict
from dataclasses import dataclass, field

from .model import norm_email, norm_name, norm_phone


@dataclass
class Identity:
    user_id: str
    name: str
    emails: set = field(default_factory=set)
    phones: set = field(default_factory=set)
    hashes: set = field(default_factory=set)
    names: set = field(default_factory=set)
    current_email: str = ""
    audit_ids: list = field(default_factory=list)     # audit_log rows that establish history

    def idents(self) -> set:
        s = {"user:" + self.user_id}
        s |= {"email:" + e for e in self.emails}
        s |= {"phone:" + p for p in self.phones if p}
        s |= {"hash:" + h for h in self.hashes}
        return s

    def strong_idents(self) -> set:
        return self.idents()


class IdentityIndex:
    def __init__(self, dataset_dir: str):
        self.users: dict[str, Identity] = {}
        with open(os.path.join(dataset_dir, "user_identities.csv"), encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                ident = Identity(r["user_id"], r["name"])
                ident.current_email = norm_email(r["email"])
                ident.emails.add(ident.current_email)
                ident.phones.add(norm_phone(r["phone"]))
                ident.hashes.add(r["email_hash"].lower())
                ident.names.add(norm_name(r["name"]))
                self.users[r["user_id"]] = ident
        path = os.path.join(dataset_dir, "user_audit_log.csv")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    ident = self.users.get(r["user_id"])
                    if not ident:
                        continue
                    f, old, new = r["field_name"], r["old_value"], r["new_value"]
                    ident.audit_ids.append(r["audit_id"])
                    if f == "email":
                        ident.emails |= {norm_email(old), norm_email(new)}
                    elif f == "phone":
                        ident.phones |= {norm_phone(old), norm_phone(new)}
                    elif f == "email_hash":
                        ident.hashes |= {old.lower(), new.lower()}
                    elif f in ("name", "full_name"):
                        ident.names |= {norm_name(old), norm_name(new)}
        for ident in self.users.values():
            ident.hashes |= {hashlib.sha256(e.encode()).hexdigest() for e in ident.emails}
            ident.phones.discard("")
        self.by_ident = defaultdict(set)
        for uid, ident in self.users.items():
            for i in ident.idents():
                self.by_ident[i].add(uid)

    def resolve_email(self, email: str) -> Identity | None:
        uids = self.by_ident.get("email:" + norm_email(email), set())
        if len(uids) == 1:
            return self.users[next(iter(uids))]
        if uids:  # prefer the user whose *current* email matches
            for u in sorted(uids):
                if self.users[u].current_email == norm_email(email):
                    return self.users[u]
            return self.users[sorted(uids)[0]]
        return None

    def owners(self, idents: set) -> set:
        out = set()
        for i in idents:
            out |= self.by_ident.get(i, set())
        return out
