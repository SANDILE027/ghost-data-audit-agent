"""Token / cost accounting for every model call made during a run."""
from __future__ import annotations

import json
from collections import defaultdict

# USD per million tokens (input, output). Update if provider pricing changes.
PRICING = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-4-1": (15.00, 75.00),
}


class UsageTracker:
    def __init__(self):
        self.calls = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "cache_hits": 0})

    def record(self, model: str, purpose: str, input_tokens: int, output_tokens: int):
        k = (model, purpose)
        self.calls[k]["calls"] += 1
        self.calls[k]["input"] += int(input_tokens or 0)
        self.calls[k]["output"] += int(output_tokens or 0)

    def record_cache_hit(self, model: str, purpose: str):
        self.calls[(model, purpose)]["cache_hits"] += 1

    def cost(self, model: str, inp: int, out: int) -> float:
        pi, po = PRICING.get(model, (0.0, 0.0))
        return inp / 1e6 * pi + out / 1e6 * po

    def summary(self, n_requests: int) -> dict:
        rows = []
        tot_in = tot_out = tot_calls = 0
        tot_cost = 0.0
        for (model, purpose), v in sorted(self.calls.items()):
            c = self.cost(model, v["input"], v["output"])
            rows.append({"model": model, "purpose": purpose, **v, "cost_usd": round(c, 6)})
            tot_in += v["input"]; tot_out += v["output"]; tot_calls += v["calls"]; tot_cost += c
        return {"requests": n_requests, "rows": rows, "total_calls": tot_calls, "total_input": tot_in,
                "total_output": tot_out, "total_tokens": tot_in + tot_out,
                "avg_tokens_per_request": (tot_in + tot_out) / n_requests if n_requests else 0,
                "total_cost_usd": round(tot_cost, 6),
                "cost_per_request_usd": round(tot_cost / n_requests, 8) if n_requests else 0}

    def dump(self, path: str, n_requests: int):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.summary(n_requests), fh, indent=2)
