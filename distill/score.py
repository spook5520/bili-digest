#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import sys
from dataclasses import dataclass, field
from typing import Callable, Any

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

@dataclass
class Factor:
    name: str
    weight: float
    fn: Callable[[dict], float]
    note: str = ""

    def value(self, ctx: dict) -> float:
        try:
            v = float(self.fn(ctx))
        except Exception:
            v = 0.0

        return 0.0 if v < 0 else (1.0 if v > 1 else v)

@dataclass
class ScoreResult:
    granularity: str
    score: float
    parts: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def explain(self) -> str:
        lines = [f"  {self.granularity} 总分 {self.score:.4f}"]
        for name, w, v, contrib in sorted(self.parts, key=lambda x: -x[3]):
            bar = "█" * max(1, int(contrib * 40)) if contrib > 0 else ""
            lines.append(f"    {name:<14} 权重{w:>5.2f} × 值{v:>4.2f} "
                         f"= 贡献{contrib:>6.3f}  {bar}")
        return "\n".join(lines)

    def top_factors(self, n: int = 3) -> list:
        return sorted(self.parts, key=lambda x: -x[3])[:n]

class Scorer:

    def __init__(self, granularity: str, factors: list, penalties: list = None,
                 bonus: list = None, keep: float = None, discard: float = None):
        if not factors:
            raise ValueError("至少需要一个正向因子")
        self.granularity = granularity
        self.factors = factors
        self.penalties = penalties or []
        self.bonus = bonus or []
        total = sum(f.weight for f in factors)
        if total <= 0:
            raise ValueError("正向权重之和必须为正")
        self._norm = [(f, f.weight / total) for f in factors]
        self.keep = keep
        self.discard = discard

    def evaluate(self, ctx: dict) -> ScoreResult:
        parts = []
        s = 0.0
        for f, w in self._norm:
            v = f.value(ctx)
            c = w * v
            s += c
            parts.append((f.name, round(w, 4), round(v, 4), round(c, 4)))
        for f in self.penalties:
            v = f.value(ctx)
            c = -abs(f.weight) * v
            s += c
            parts.append((f"{f.name}(罚)", round(-abs(f.weight), 4),
                          round(v, 4), round(c, 4)))
        for f in self.bonus:
            v = f.value(ctx)
            c = abs(f.weight) * v
            s += c
            parts.append((f"{f.name}(加)", round(abs(f.weight), 4),
                          round(v, 4), round(c, 4)))
        s = max(0.0, min(s, 1.0))
        return ScoreResult(self.granularity, round(s, 4), parts)

    def classify(self, score: float) -> str:
        if self.discard is not None and score <= self.discard:
            return "discard"
        if self.keep is not None and score >= self.keep:
            return "keep"
        return "review"

    def evaluate_and_classify(self, ctx: dict):
        r = self.evaluate(ctx)
        return r, self.classify(r.score)

def factor_linear(key: str, lo: float, hi: float, note: str = "") -> Callable:
    def f(ctx):
        v = ctx.get(key)
        if v is None:
            return 0.0
        if hi == lo:
            return 1.0 if v >= hi else 0.0
        return (float(v) - lo) / (hi - lo)
    return f

def factor_bool(key: str) -> Callable:
    return lambda ctx: 1.0 if ctx.get(key) else 0.0

def factor_ratio(num_key: str, den_key: str, cap: float = 1.0) -> Callable:
    def f(ctx):
        d = ctx.get(den_key)
        if not d:
            return 0.0
        return min(float(ctx.get(num_key, 0)) / float(d), cap)
    return f

def factor_log(key: str, scale: float, note: str = "") -> Callable:
    import math
    def f(ctx):
        n = ctx.get(key)
        if n is None or n <= 0:
            return 0.0
        return min(math.log1p(float(n)) / math.log1p(scale), 1.0)
    return f

if __name__ == "__main__":

    s = Scorer("demo", [
        Factor("a", 2.0, factor_bool("x"), "布尔因子"),
        Factor("b", 1.0, factor_linear("y", 0, 10), "线性因子"),
        Factor("c", 1.0, factor_log("z", 1000), "对数因子"),
    ], keep=0.5, discard=0.2)

    r, cls = s.evaluate_and_classify({"x": True, "y": 5, "z": 100})
    print(r.explain())
    print(f"  归类: {cls}")
    print()
    r2, cls2 = s.evaluate_and_classify({"x": False, "y": 0, "z": 0})
    print(r2.explain())
    print(f"  归类: {cls2}")
