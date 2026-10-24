"""Request-intent classification: does the request text authorize deletion?

Two independent classifiers:
  * `rule_intent`  - a deterministic multilingual scorer (deletion cues vs.
    negations / non-deletion purposes). Always available, zero cost.
  * `LLMIntentClassifier` - an LLM that reads the request (any language) and
    returns a structured verdict. Results are cached on disk keyed by the text
    hash, and token usage is recorded for the usage report.
The pipeline uses the LLM verdict when available and logs every disagreement
with the rule classifier so that the evaluation can surface them."""
from __future__ import annotations

import hashlib
import json
import os
import re

DELETION_CUES = [
    r"\bdelet\w*", r"\beras\w*", r"\bremove (my|the) (account|profile)", r"\bclose (my|this|the) account",
    r"\bclosed? (my )?account", r"permanent(ly)? (account[- ])?delet", r"account[- ]deletion",
    r"exclus[aã]o", r"\bexclu[ai]r\b", r"\belimin\w*", r"\bborr\w*", r"\bapag\w*", r"\bsuppr\w*", r"\beffac\w*",
    r"\bl[öo]sch\w*", r"\bverwijder\w*", r"\bcancel(ar|e|ló)\b.{0,20}\bcuenta\b",
    r"삭제", r"削除", r"удал", r"cancel(ar)? (mi|minha) cuenta",
    r"remove (my|the) profile", r"remove (any )?remaining", r"finish the deletion", r"no longer need this account",
    r"\bdisconnected my account\b", r"\bdeleting my account\b",
]
NEGATIONS = [
    r"\bnot asking for deletion\b", r"\bnot a request to (erase|delete)", r"\bdon'?t delete\b", r"\bdo not (close|delete)\b",
    r"\bwithout removing\b", r"\bkeep the account\b", r"\bleave (the|my) (account|profile)\b", r"\bleave .{0,30} in place\b",
    r"\bcontinue using the service\b", r"\bmarketing lists only\b", r"\bstill (want|need|use)\b",
    r"\bkeep (my|the) (account|profile)\b", r"\baccount active\b",
]
NON_DELETION_PURPOSES = [
    r"\bdownload my\b", r"\bcopy of\b", r"\bwhat personal information\b", r"\bcorrect it\b", r"\bmisspelled\b",
    r"\bchange the email\b", r"\bresetting the login\b", r"\bpromotional emails\b", r"\bout of date\b",
    r"\bportable format\b", r"\binvoices\b", r"\bopt[- ]?out\b", r"\bunsubscribe\b",
]


def rule_intent(text: str) -> dict:
    t = (text or "").lower()
    # keep the words that actually matched (readable audit trail), not the patterns
    hits = lambda pats: [m.group(0) for p in pats for m in [re.search(p, t)] if m]
    pos, neg, other = hits(DELETION_CUES), hits(NEGATIONS), hits(NON_DELETION_PURPOSES)
    score = len(pos) - 2 * len(neg) - len(other)
    # The intake queue only receives privacy requests: a follow-up such as "my old profile still
    # appears, remove that too" is a deletion request even without an explicit verb, so the
    # default is to authorize unless the text states a different purpose or a negation.
    authorizes = not neg and (score > 0 or not other)
    return {"authorizes_deletion": authorizes, "score": score, "cues": pos, "negations": neg, "other": other,
            "method": "rules"}


SYSTEM_PROMPT = """You triage privacy requests sent to an online shop.
Decide whether the customer is asking the shop to DELETE / ERASE their account and personal data
(right to erasure). Requests to correct data, export or download data, get a copy, change an email,
reset a login, or stop marketing are NOT deletion requests, even if they mention deletion in passing
(e.g. "I am not asking for deletion"). Requests may be written in any language.
Reply with JSON only: {"authorizes_deletion": true|false, "request_type": "<deletion|access|export|correction|marketing_opt_out|account_support|other>", "language": "<ISO code>"}"""


class LLMIntentClassifier:
    """Anthropic Messages API classifier with an on-disk cache."""

    def __init__(self, cache_path: str, usage, model: str | None = None):
        self.model = model or os.environ.get("GHOST_INTENT_MODEL", "claude-haiku-4-5")
        self.cache_path = cache_path
        self.usage = usage
        self.cache = {}
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as fh:
                self.cache = json.load(fh)
        self.client = None
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                import anthropic  # type: ignore
                self.client = anthropic.Anthropic()
            except Exception:
                self.client = None

    @property
    def available(self) -> bool:
        return self.client is not None

    def _key(self, text: str) -> str:
        return hashlib.sha256((self.model + "\n" + text).encode()).hexdigest()

    def classify(self, text: str) -> dict | None:
        k = self._key(text)
        if k in self.cache:
            self.usage.record_cache_hit(self.model, "intent")
            return dict(self.cache[k], method="llm-cache")
        if not self.available:
            return None
        resp = self.client.messages.create(
            model=self.model, max_tokens=80, temperature=0, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Request text:\n<<<\n{text}\n>>>"}],
        )
        self.usage.record(self.model, "intent", resp.usage.input_tokens, resp.usage.output_tokens)
        raw = "".join(getattr(b, "text", "") for b in resp.content)
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            out = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            out = {}
        if "authorizes_deletion" not in out:
            return None
        out = {"authorizes_deletion": bool(out["authorizes_deletion"]), "request_type": out.get("request_type"),
               "language": out.get("language")}
        self.cache[k] = out
        return dict(out, method="llm")

    def save(self):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as fh:
            json.dump(self.cache, fh, indent=1, sort_keys=True)


def classify_requests(requests: list[dict], llm: LLMIntentClassifier | None) -> dict:
    """case_id -> verdict dict (with both classifiers' opinions)."""
    out = {}
    for q in requests:
        r = rule_intent(q["request_text"])
        verdict = dict(r)
        l = llm.classify(q["request_text"]) if llm else None
        if l is not None:
            verdict = dict(l)
            verdict["rules_agree"] = (l["authorizes_deletion"] == r["authorizes_deletion"])
            verdict["rules"] = r
        out[q["case_id"]] = verdict
    return out
