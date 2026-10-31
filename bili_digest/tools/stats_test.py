import os as _os
import sys as _sys

from bili_digest import paths

import math
import sys

def wilcoxon_signed_rank(deltas, zero="drop") -> dict:
    d = [x for x in deltas if x is not None]
    zeros = sum(1 for x in d if abs(x) < 1e-12)
    if zero == "drop":
        d = [x for x in d if abs(x) > 1e-12]
    n = len(d)
    if n == 0:
        return {"n": 0, "W": 0.0, "z": 0.0, "p": 1.0, "zeros": zeros}

    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(abs(d[order[j + 1]]) - abs(d[order[i]])) < 1e-12:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    W_plus = sum(ranks[i] for i in range(n) if d[i] > 0)
    W_minus = sum(ranks[i] for i in range(n) if d[i] < 0)
    W = min(W_plus, W_minus)
    mean = n * (n + 1) / 4.0

    var = n * (n + 1) * (2 * n + 1) / 24.0
    z = (W - mean) / math.sqrt(var) if var > 0 else 0.0

    p = 2 * (1 - _norm_cdf(abs(z)))
    return {"n": n, "W": W, "W_plus": W_plus, "W_minus": W_minus,
            "z": z, "p": min(1.0, p), "zeros": zeros}

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bootstrap_ci(deltas, n_boot: int = 10000, alpha: float = 0.05,
                 seed: int = 42) -> dict:
    import numpy as np
    d = np.array([x for x in deltas if x is not None], dtype=float)
    if len(d) == 0:
        return {"mean": 0.0, "lo": 0.0, "hi": 0.0, "width": 0.0}
    rng = np.random.default_rng(seed)
    n = len(d)
    idx = rng.integers(0, n, size=(n_boot, n))
    means = d[idx].mean(axis=1)
    lo = float(np.percentile(means, 100 * alpha / 2))
    hi = float(np.percentile(means, 100 * (1 - alpha / 2)))
    return {"mean": float(d.mean()), "lo": lo, "hi": hi, "width": hi - lo}

def paired_ttest(deltas) -> dict:
    d = [x for x in deltas if x is not None]
    n = len(d)
    if n < 2:
        return {"n": n, "t": 0.0, "p": 1.0}
    m = sum(d) / n
    var = sum((x - m) ** 2 for x in d) / (n - 1)
    if var <= 0:
        return {"n": n, "t": 0.0, "p": 1.0}
    se = math.sqrt(var / n)
    t = m / se

    p = 2 * (1 - _norm_cdf(abs(t)))
    return {"n": n, "t": t, "p": min(1.0, p)}

def min_queries_needed(sigma_q: float, delta: float, power_factor: float = 8.0) -> int:
    if delta <= 0:
        return 10 ** 9
    return int(math.ceil(power_factor * (sigma_q / delta) ** 2))

def detectable_delta(n: int, sigma_q: float, power_factor: float = 8.0) -> float:
    if n <= 0:
        return float("inf")
    return sigma_q * math.sqrt(power_factor / n)

def benjamini_hochberg(pvals: list, alpha: float = 0.05) -> dict:
    idx = sorted(range(len(pvals)), key=lambda i: pvals[i])
    m = len(pvals)
    p_adj = [1.0] * m

    prev = 1.0
    for rank_from_end in range(m - 1, -1, -1):
        i = idx[rank_from_end]
        val = pvals[i] * m / (rank_from_end + 1)
        prev = min(prev, val)
        p_adj[i] = min(1.0, prev)
    reject = [p_adj[i] <= alpha for i in range(m)]
    k = max((r for r in range(1, m + 1) if pvals[idx[r - 1]] <= alpha * r / m),
            default=0)
    thr = alpha * k / m if k else 0.0
    return {"reject": reject, "p_adj": p_adj, "threshold": thr, "k": k}

def report_many(cases: list, alpha: float = 0.05) -> dict:
    rows = []
    for label, deltas, sigma_q in cases:
        r = report(deltas, sigma_q, label, "")
        rows.append({"label": label, **r})
    bh = benjamini_hochberg([r["wilcoxon"]["p"] for r in rows], alpha)
    for r, pa, rej in zip(rows, bh["p_adj"], bh["reject"]):
        r["p_adj"] = pa
        r["reject"] = rej
    return {"rows": rows, "threshold": bh["threshold"], "k": bh["k"],
            "n_sig_raw": sum(1 for r in rows if r["wilcoxon"]["p"] <= alpha),
            "n_sig_adj": sum(bh["reject"])}

def report(deltas, sigma_q: float, label_a: str = "A", label_b: str = "B") -> dict:
    w = wilcoxon_signed_rank(deltas)
    t = paired_ttest(deltas)
    c = bootstrap_ci(deltas)
    n = len([x for x in deltas if x is not None])
    print(f"  {label_b} vs {label_a}（n={n}）")
    print(f"    均值差 {c['mean']:+.4f}   95% CI [{c['lo']:+.4f}, {c['hi']:+.4f}]"
          f"（宽 {c['width']:.4f}）")
    print(f"    Wilcoxon p={w['p']:.4f}（W={w['W']:.1f}，零差 {w['zeros']}）"
          f" · 配对 t p={t['p']:.4f}")
    sig = "0 不在 CI 内" if (c["lo"] > 0 or c["hi"] < 0) else "0 在 CI 内"
    print(f"    → {sig}")

    md = detectable_delta(n, sigma_q)
    print(f"    → 该 n 下可检测的最小 delta ≈ {md:.4f}"
          f"（σ_q={sigma_q}）")
    if abs(c["mean"]) < md:
        print(f"    ⚠ 观测差值 {abs(c['mean']):.4f} < 可检测下限 {md:.4f} ——")
        print(f"      这个比较【没有分辨力】，差值落在噪声里。要不扩大查询集到"
              f" ≥{min_queries_needed(sigma_q, abs(c['mean']) or 0.05)} 条，"
              f"要不放弃这个量级的比较。")
    return {"wilcoxon": w, "ttest": t, "ci": c, "min_detectable": md}

if __name__ == "__main__":
    import random
    print("  自检：构造三组已知情形的差值\n")
    random.seed(0)

    d1 = [random.gauss(0.08, 0.15) for _ in range(40)]
    print("  ① 真差异（均值 0.08，σ 0.15，n=40）:")
    report(d1, 0.15, "现状", "改进")
    print()

    d2 = [random.gauss(0.0, 0.15) for _ in range(40)]
    print("  ② 纯噪声（均值 0，σ 0.15，n=40）:")
    report(d2, 0.15, "现状", "改动")
    print()

    d3 = [random.gauss(0.005, 0.15) for _ in range(20)]
    print("  ③ 微小差异（均值 0.005，n=20 —— 本项目实际情形）:")
    report(d3, 0.15, "现状", "改动")
    print()
    print(f"  样本量对照（σ_q=0.20）:")
    for d in (0.02, 0.05, 0.10, 0.126):
        print(f"    检测 delta={d:.3f} → 需 {min_queries_needed(0.20, d):>5} 条查询")
    print()
    print(f"  反推（σ_q=0.20）:")
    for n in (16, 20, 50, 128, 800):
        print(f"    n={n:<4} → 可检测的最小 delta {detectable_delta(n, 0.20):.4f}")

def proportion_ci(k: int, n: int, B: int = 4000, seed: int = 0) -> dict:
    import numpy as _np
    if n <= 0:
        return {"p": None, "lo": None, "hi": None, "width": None, "se": None}
    p = k / n
    se = (p * (1 - p) / n) ** 0.5
    rng = _np.random.default_rng(seed)
    sims = rng.binomial(n, p, size=B) / n
    lo, hi = _np.percentile(sims, [2.5, 97.5])
    return {"p": round(p, 4), "lo": round(float(lo), 4),
            "hi": round(float(hi), 4), "width": round(float(hi - lo), 4),
            "se": round(se, 4), "n": n, "k": k}

def ci_line(k: int, n: int, label: str = "") -> str:
    d = proportion_ci(k, n)
    if d["p"] is None:
        return "  %s n=0" % label
    return ("  %-24s %5.1f%% (%d/%d)  95%% CI [%.1f%%, %.1f%%]  宽 %.1fpt" % (
        label, d["p"] * 100, k, n, d["lo"] * 100, d["hi"] * 100, d["width"] * 100))

def n_for_width(target_width: float = 0.04, p: float = 0.7) -> int:

    n = (2 * 1.96) ** 2 * p * (1 - p) / (target_width ** 2)
    return int(n) + 1
