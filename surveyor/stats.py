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


def _solve(A: list[list[float]], g: list[float]) -> list[float] | None:
    """Solve A x = g by Gaussian elimination with partial pivoting. None if singular."""
    n = len(A)
    M = [A[i][:] + [g[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return None                      # singular / collinear
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        for r in range(n):
            if r != col and M[r][col] != 0.0:
                f = M[r][col] / pv
                for c in range(col, n + 1):
                    M[r][c] -= f * M[col][c]
    return [M[i][n] / M[i][i] for i in range(n)]


def _ols_residuals(v: list[float], zcols: list[list[float]]) -> list[float] | None:
    """Residuals of v after OLS regression on zcols (+ intercept), via normal equations.
    None if the system is singular (perfectly collinear controls)."""
    n = len(v)
    p = len(zcols) + 1                       # + intercept
    X = [[1.0] + [zc[i] for zc in zcols] for i in range(n)]
    A = [[0.0] * p for _ in range(p)]
    g = [0.0] * p
    for i in range(n):
        xi, vi = X[i], v[i]
        for a in range(p):
            g[a] += xi[a] * vi
            xa = xi[a]
            for b in range(a, p):
                A[a][b] += xa * xi[b]
    for a in range(p):                       # mirror the symmetric matrix
        for b in range(a):
            A[a][b] = A[b][a]
    beta = _solve(A, g)
    if beta is None:
        return None
    return [v[i] - sum(beta[a] * X[i][a] for a in range(p)) for i in range(n)]


def partial_spearman_multi(x: list[float], y: list[float],
                           zs: list[list[float]]) -> float:
    """Spearman partial correlation of x and y controlling for a SET of variables zs,
    via rank-transform + OLS residualisation on all controls at once. The multivariate
    generalisation of partial_spearman. NaN if degenerate or the controls are collinear."""
    if len(x) < len(zs) + 3:
        return float("nan")
    rx, ry = ranks(x), ranks(y)
    rz = [ranks(z) for z in zs]
    ex, ey = _ols_residuals(rx, rz), _ols_residuals(ry, rz)
    if ex is None or ey is None:
        return float("nan")
    return pearson(ex, ey)


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
