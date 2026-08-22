"""Tiny pure-Python stats so the core needs no numpy/scipy (offline-friendly)."""
from __future__ import annotations

import math


def ranks(xs: list[float]) -> list[float]:
    """Average (fractional) ranks, 1-based, ties averaged."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1  # average of ranks i+1..j+1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx == 0 or sy == 0:
        return float("nan")
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / math.sqrt(sx * sy)


def spearman(xs: list[float], ys: list[float]) -> float:
    return pearson(ranks(xs), ranks(ys))


def partial_spearman(x: list[float], y: list[float], z: list[float]) -> float:
    """Spearman(x, y) controlling for z, via rank-correlation residualisation."""
    rxy, rxz, ryz = spearman(x, y), spearman(x, z), spearman(y, z)
    denom = math.sqrt((1 - rxz ** 2) * (1 - ryz ** 2))
    if denom == 0 or any(math.isnan(v) for v in (rxy, rxz, ryz)):
        return float("nan")
    return (rxy - rxz * ryz) / denom


def auc(scores: list[float], labels: list[int]) -> float:
    """P(a random positive scores above a random negative). Mann-Whitney form."""
    pos = sum(labels)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return float("nan")
    r = ranks([float(s) for s in scores])
    rank_sum_pos = sum(ri for ri, l in zip(r, labels) if l)
    return (rank_sum_pos - pos * (pos + 1) / 2) / (pos * neg)


def precision_at_k(scores: list[float], labels: list[int], k: int) -> float:
    if k <= 0:
        return float("nan")
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
    return sum(labels[i] for i in order) / len(order)
