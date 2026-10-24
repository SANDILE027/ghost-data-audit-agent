"""Format-agnostic parser for the platform log exports.

Every log line in the corpus is some envelope (logfmt, JSON, syslog, pipe
separated, bracketed, cloud-runtime with an escaped message) around the same
payload: a timestamp, a service, an optional trace id and key=value pairs.
We normalise every line into (timestamp, service, trace_id, event, fields)."""
from __future__ import annotations

import json
import re

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$")
KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*"|\S+)')
BRACKET_RE = re.compile(r"^\[(\w+)\]\s+\[([\w-]+)\]\s+\[([\w-]+)\]\s+([\w-]+):\s*(.*)$")
PIPE_RE = re.compile(r"^\|?\s*(\w+)\s*\|\s*([\w-]+)\s*\|\s*([\w-]+)\s*\|\s*([\w-]+)\s*\|\s*(.*)$")
SYSLOG_RE = re.compile(r"^<\d+>\d+\s+\S+\s+([\w-]+)\s+([\w-]+)\s+-\s+-\s+(.*)$")


def _unq(v: str) -> str:
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].replace('\\"', '"')
    return v


def _kv(text: str) -> dict:
    return {k: _unq(v) for k, v in KV_RE.findall(text)}


def parse_line(line: str):
    line = line.rstrip("\n")
    m = TS_RE.match(line)
    if not m:
        return None
    ts, rest = m.group(1), m.group(2)
    fields: dict = {}
    service = trace = event = None
    if rest.startswith("{"):
        try:
            obj = json.loads(rest)
        except json.JSONDecodeError:
            obj = {}
        fields = {k: (str(v) if v is not None else "null") for k, v in obj.items()}
        service = fields.get("service") or fields.get("logger")
    elif (b := BRACKET_RE.match(rest)):
        _, service, trace, event, tail = b.groups()
        fields = _kv(tail)
    elif (p := PIPE_RE.match(rest)):
        _, service, trace, event, tail = p.groups()
        fields = _kv(tail)
    elif (s := SYSLOG_RE.match(rest)):
        service, trace, tail = s.groups()
        fields = _kv(tail)
    else:
        fields = _kv(rest)
        if "message" in fields and "=" in fields["message"]:
            inner = _kv(fields.pop("message"))
            fields.update(inner)
        service = fields.get("service") or fields.get("function")
        trace = fields.get("trace_id") or fields.get("requestId")
    event = fields.get("event", event)
    trace = fields.get("trace_id", trace)
    return {"ts": ts, "service": service, "trace": trace, "event": event, "fields": fields}


def parse_file(path: str):
    out = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            rec = parse_line(line)
            if rec:
                rec["line"] = i + 1
                out.append(rec)
    return out
