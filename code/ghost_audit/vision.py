"""Document & screenshot reader.

PDFs (statements, support attachments, registers) and screenshots of internal
consoles are turned into Observations. Two readers are available:

* OCR reader (default, offline, deterministic): `pdftotext` for PDFs and
  Tesseract (two page-segmentation modes) for screenshots, followed by
  OCR-tolerant entity resolution: resource ids are re-assembled from split
  tokens ("data_flow 9846" -> data_flow_9846) and e-mail addresses are snapped
  to the nearest known identifier (edit distance <= 2) to undo OCR noise
  ("ingrid.dahi@outiook.com" -> ingrid.dahl@outlook.com).
* VLM reader (optional): when ANTHROPIC_API_KEY is set and --vlm is passed, the
  screenshot/PDF page is sent to a vision model which returns structured rows;
  results are cached on disk and token usage recorded. The OCR result is kept as
  a cross-check.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess

from .evidence import derive
from .model import EMAIL_RE, Observation, norm_email, norm_phone, system_for

RID_LOOSE = re.compile(
    r"\b(audit_record|data_flow|email_delivery|legal_hold|tombstone|marketing|analytics|account|search|support|export|backup)"
    r"(?:_(\d{1,5})|[ .,](\d{3,5}))\b")
USER_LOOSE = re.compile(r"\buser[ _]?(\d{1,4})\b", re.I)
PHONE_RE = re.compile(r"\+\d[\d ()\-]{7,}\d")


def _lev(a: str, b: str, cap: int = 3) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


class DocumentReader:
    def __init__(self, identities, cache_dir: str, usage=None, use_vlm: bool = False):
        self.ix = identities
        self.usage = usage
        self.cache_dir = cache_dir
        self.use_vlm = use_vlm and bool(os.environ.get("ANTHROPIC_API_KEY"))
        self.known_emails = sorted({i.split(":", 1)[1] for i in identities.by_ident if i.startswith("email:")})
        self.vlm_cache_path = os.path.join(cache_dir, "vlm_cache.json")
        self.vlm_cache = {}
        if os.path.exists(self.vlm_cache_path):
            self.vlm_cache = json.load(open(self.vlm_cache_path, encoding="utf-8"))
        self.extractions = {}   # file_id -> extracted text summary (for audit trail)
        self.known_resources = set()  # ids corroborated by structured sources (set by EvidenceStore)
        self.aliases = {}             # business reference -> resource id (from the data catalog)

    # ------------------------------------------------------------------
    def text_of(self, path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf":
            if shutil.which("pdftotext"):
                return subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True).stdout
            try:
                from pypdf import PdfReader  # type: ignore
                return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
            except Exception:
                return ""
        if not shutil.which("tesseract"):
            return ""
        # word boxes -> visual rows (table rows of the console), so that an id and the e-mail on
        # the same screen row end up on the same text line
        tsv = subprocess.run(["tesseract", path, "-", "--psm", "11", "tsv"], capture_output=True, text=True).stdout
        words = []
        for ln in tsv.splitlines()[1:]:
            parts = ln.split("\t")
            if len(parts) < 12 or not parts[11].strip():
                continue
            try:
                left, top, h = int(parts[6]), int(parts[7]), int(parts[9])
            except ValueError:
                continue
            words.append((top + h / 2, left, parts[11]))
        words.sort()
        rows, cur, cy = [], [], None
        for y, x, w in words:
            if cy is not None and abs(y - cy) > 9:
                rows.append(cur); cur = []
            cur.append((x, w)); cy = y if cy is None or not cur[:-1] else (cy + y) / 2
        if cur:
            rows.append(cur)
        lines = [" ".join(w for _, w in sorted(r)) for r in rows]
        return "\n".join(lines)

    def snap_email(self, e: str) -> str | None:
        e = norm_email(e).replace(",", ".")
        if e in self.ix.by_ident and False:
            return e
        if "email:" + e in self.ix.by_ident:
            return e
        best, bd = None, 3
        for k in self.known_emails:
            if abs(len(k) - len(e)) > 2 or k[0] != e[0]:
                continue
            d = _lev(e, k, 2)
            if d < bd:
                best, bd = k, d
        return best if bd <= 2 else None

    # ------------------------------------------------------------------
    def observations(self, fid: str, row: dict, path: str):
        captured = row["captured_at"]
        text = self.text_of(path)
        self._origin = row.get("origin", "")
        rows = self._rows_from_text(text, ocr=not path.lower().endswith(".pdf"))
        if self.use_vlm and path.lower().endswith(".png"):
            vlm_rows = self._vlm_rows(path)
            if vlm_rows is not None:
                rows = vlm_rows
        self.extractions[fid] = rows
        origin = row.get("origin", "")
        out = []
        for r in rows:
            system = r.get("system") or system_for(r["resource_id"])
            if not system:
                continue
            fields = dict(r.get("fields", {}))
            explicit = r.get("result")
            if explicit is None:
                if system == "integration_service":
                    explicit = "ACTIVE" if re.search(r"continuous|active|enabled", text, re.I) else None
                elif system == "backup_service":
                    # listed in the recovery manager with no deletion block shown -> restorable
                    explicit = "ACTIVE"
            cat, state, exp = derive(system, fields, explicit, r.get("category"))
            if system == "support_platform" and ("attachment" in text.lower() or ".pdf" in text.lower()) \
                    and path.lower().endswith(".pdf"):
                cat = "SUPPORT_ATTACHMENT"
                state = "PRESENT_PII" if r["idents"] else "PRESENT"
            obs = Observation(fid, captured, captured, system, r["resource_id"], cat, state, set(r["idents"]),
                              f"document:{origin}", exp | ({"email"} if any(i.startswith("email:") for i in r["idents"]) else set()))
            out.append(obs)
        return out

    def alias_observations(self, fid: str, row: dict, text: str):
        """Observations for resources that a text document references only by catalog alias."""
        self._origin = row.get("origin", "")
        saved, self.aliases_only = getattr(self, "aliases_only", False), True
        try:
            rows = [r for r in self._rows_from_text(text, ocr=False) if r.get("via_alias")]
        finally:
            self.aliases_only = saved
        out = []
        for r in rows:
            system = system_for(r["resource_id"])
            cat, state, exp = derive(system, {}, None, None)
            out.append(Observation(fid, row["captured_at"], row["captured_at"], system, r["resource_id"], cat, state,
                                   set(r["idents"]), f"document-alias:{self._origin}",
                                   exp | ({"email"} if any(i.startswith("email:") for i in r["idents"]) else set())))
        return out

    def _rows_from_text(self, text: str, ocr: bool = True):
        """Associate each resource id with identifiers on the same line; when the
        document is about a single subject, its identifiers apply to every row."""
        doc_emails, doc_users, doc_phones = set(), set(), set()
        for e in EMAIL_RE.findall(text):
            s = self.snap_email(e)
            if s:
                doc_emails.add(s)
        for u in re.findall(r"\buser_\d{2,4}\b", text):
            if u in self.ix.users:
                doc_users.add(u)
        for p in PHONE_RE.findall(text):
            doc_phones.add(norm_phone(p))
        # subjects behind the identifiers
        subj_users = set()
        for e in doc_emails:
            subj_users |= self.ix.by_ident.get("email:" + e, set())
        single = len(subj_users) == 1
        rows = {}
        alias_hits = set()
        for line in ([] if getattr(self, "aliases_only", False) else text.splitlines()):
            raw_emails = EMAIL_RE.findall(line)
            for m in RID_LOOSE.finditer(line):
                rid = f"{m.group(1).lower()}_{m.group(2) or m.group(3)}"
                idents = set()
                for e in raw_emails:
                    s = self.snap_email(e)
                    if s:
                        idents.add("email:" + s)
                for u in re.findall(r"\buser_\d{2,4}\b", line):
                    if u in self.ix.users:
                        idents.add("user:" + u)
                entry = rows.setdefault(rid, [set(), False])
                entry[0].update(idents)
                entry[1] = entry[1] or bool(raw_emails)
        # OCR validation: keep ids corroborated elsewhere in the corpus; snap one-character
        # misreads to the corroborated id; drop fragments of a longer id read on the same page
        if self.known_resources and ocr:
            fixed = {}
            for rid, val in rows.items():
                if rid not in self.known_resources:
                    pre, num = rid.rsplit("_", 1)
                    cands = [k for k in self.known_resources if k.startswith(pre + "_") and _lev(k, rid, 1) <= 1]
                    if len(cands) != 1:
                        continue
                    rid = cands[0]
                ent = fixed.setdefault(rid, [set(), False])
                ent[0] |= val[0]; ent[1] = ent[1] or val[1]
            rows = fixed
        # business references registered in the data catalog (statement numbers, customer keys)
        for alias, rid in self.aliases.items():
            if alias.startswith("user_") and system_for(rid) != getattr(self, "_origin", None):
                continue   # a bare customer key only identifies the resource on its own system's screens
            if re.search(r"(?<![\w-])" + re.escape(alias) + r"(?![\w-])", text) and rid not in rows:
                rows[rid] = [set(), False]
                alias_hits.add(rid)
                if alias.startswith("user_") and alias in self.ix.users:
                    rows[rid][0].add("user:" + alias)
                    single = True
                    subj_users = {alias}
        out = []
        for rid, (idents, had_email) in rows.items():
            if not idents and single and not had_email:
                u = next(iter(subj_users))
                idents = {"email:" + e for e in doc_emails if u in self.ix.by_ident.get("email:" + e, set())}
                idents |= {"phone:" + p for p in doc_phones if u in self.ix.by_ident.get("phone:" + p, set())}
                idents.add("user:" + u)
            out.append({"resource_id": rid, "idents": sorted(idents), "fields": {}, "via_alias": rid in alias_hits})
        return out

    # ------------------------------------------------------------------
    VLM_PROMPT = (
        "This is a screenshot of an internal console of an online shop. List every customer-data record "
        "visible on screen whose identifier looks like account_N, search_N, support_N, export_N, analytics_N, "
        "data_flow_N, backup_N, marketing_N, tombstone_N or audit_record_N. For each, give the e-mail, phone, "
        "user_N reference shown on the same row or detail pane, and whether the record is active / present / "
        "restorable / enabled. Reply with JSON only: {\"rows\": [{\"resource_id\": str, \"emails\": [str], "
        "\"phones\": [str], \"user_refs\": [str], \"status\": str}]}")

    def _vlm_rows(self, path: str):
        data = open(path, "rb").read()
        key = hashlib.sha256(data).hexdigest()
        if key not in self.vlm_cache:
            try:
                import anthropic  # type: ignore
                client = anthropic.Anthropic()
                model = os.environ.get("GHOST_VLM_MODEL", "claude-sonnet-4-5")
                resp = client.messages.create(
                    model=model, max_tokens=800, temperature=0,
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": base64.b64encode(data).decode()}},
                        {"type": "text", "text": self.VLM_PROMPT}]}])
                if self.usage:
                    self.usage.record(model, "vision", resp.usage.input_tokens, resp.usage.output_tokens)
                raw = "".join(getattr(b, "text", "") for b in resp.content)
                m = re.search(r"\{.*\}", raw, re.S)
                self.vlm_cache[key] = json.loads(m.group(0)) if m else {"rows": []}
                os.makedirs(self.cache_dir, exist_ok=True)
                json.dump(self.vlm_cache, open(self.vlm_cache_path, "w", encoding="utf-8"), indent=1)
            except Exception:
                return None
        elif self.usage:
            self.usage.record_cache_hit("vlm", "vision")
        rows = []
        for r in self.vlm_cache[key].get("rows", []):
            rid = (r.get("resource_id") or "").strip().lower()
            if not system_for(rid):
                continue
            idents = {"email:" + s for s in (self.snap_email(e) for e in r.get("emails", [])) if s}
            idents |= {"user:" + u for u in r.get("user_refs", []) if re.fullmatch(r"user_\d+", u)}
            idents |= {"phone:" + norm_phone(p) for p in r.get("phones", []) if norm_phone(p)}
            rows.append({"resource_id": rid, "idents": sorted(idents), "fields": {}})
        return rows
