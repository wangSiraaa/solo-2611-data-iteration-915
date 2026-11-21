"""Exact integer split solver for consortium promotion settlement.

Given the grand coalition's realised integer savings ``total``, per-counter
confirmed minimums and every coalition's independently quoted savings
``values[S]``, compute a split ``x`` of ``total`` into non-negative integer
shares such that

* every counter gets at least its confirmed minimum:  ``x_i >= m_i``;
* no coalition profits from leaving the consortium:  ``sum_{i in S} x_i >= v(S)``;
* the shares sum exactly to ``total``.

Among all feasible splits the solver first minimises the spread
``max(x) - min(x)`` and then takes the lexicographically smallest amount
vector in stable counter-id order.

When no split exists the solver returns a re-checkable certificate built
only from real, independently quotable constraints:

* a **Farkas certificate** — a weighted combination of coalition / minimum /
  total constraints whose left-hand sides cancel to zero in every counter
  while the right-hand sides sum to a positive amount — produced by an exact
  rational simplex; or
* an **integer enumeration certificate** — the bounded box of all candidate
  splits plus the full constraint data — when the fractional relaxation is
  feasible yet no integer split exists.

All arithmetic is exact (``fractions.Fraction``); there is no floating point
anywhere in the decision path.
"""

from __future__ import annotations

from fractions import Fraction
from math import gcd
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import INVALID_INPUT, BasketError

# ---------------------------------------------------------------------------
# Exact two-phase simplex with Bland's rule.
# ---------------------------------------------------------------------------


def _simplex_min(costs: Sequence[Any], rows: Sequence[Tuple[Sequence[Any], str, Any, Any]]):
    """Solve ``min c·x`` subject to the given rows and ``x >= 0``.

    Every row is ``(coeffs, sense, rhs, tag)`` with ``sense`` in
    ``{"<=", ">=", "=="}``.  Returns one of

    * ``("optimal", x, objective)`` with ``x`` a list of ``Fraction``;
    * ``("infeasible", y)`` where ``y`` is a per-row Farkas weight list mapped
      back to the caller's row senses (``y_i >= 0`` for ``">="`` rows,
      ``y_i <= 0`` for ``"<="`` rows, free for ``"=="`` rows, satisfying
      ``y·A <= 0`` and ``y·b > 0``);
    * ``("unbounded", None)``.
    """

    n = len(costs)
    norm: List[List[Any]] = []
    for coeffs, sense, rhs, tag in rows:
        coeffs = [Fraction(v) for v in coeffs]
        if len(coeffs) != n:
            raise AssertionError("row width does not match the objective")
        if sense not in ("<=", ">=", "=="):
            raise AssertionError(f"unknown sense {sense!r}")
        rhs = Fraction(rhs)
        flipped = False
        if rhs < 0:
            coeffs = [-v for v in coeffs]
            rhs = -rhs
            sense = {"<=": ">=", ">=": "<=", "==": "=="}[sense]
            flipped = True
        norm.append([coeffs, sense, rhs, tag, flipped])
    m = len(norm)
    if m == 0:
        return ("optimal", [Fraction(0)] * n, Fraction(0))

    # Column layout: structural variables, per-row slack/surplus, artificials.
    ncols = n
    aux_col: Dict[int, int] = {}
    art_col: Dict[int, int] = {}
    for i, (_, sense, _, _, _) in enumerate(norm):
        if sense in ("<=", ">="):
            aux_col[i] = ncols
            ncols += 1
        if sense in (">=", "=="):
            art_col[i] = ncols
            ncols += 1

    tab: List[List[Fraction]] = []
    for i, (coeffs, sense, rhs, _, _) in enumerate(norm):
        row = [Fraction(0)] * (ncols + 1)  # last column is the rhs
        for j in range(n):
            row[j] = coeffs[j]
        if i in aux_col:
            row[aux_col[i]] = Fraction(1 if sense == "<=" else -1)
        if i in art_col:
            row[art_col[i]] = Fraction(1)
        row[ncols] = rhs
        tab.append(row)
    basis: List[int] = [art_col[i] if i in art_col else aux_col[i] for i in range(m)]

    def pivot(prow: int, pcol: int) -> None:
        inv = Fraction(1) / tab[prow][pcol]
        tab[prow] = [v * inv for v in tab[prow]]
        for i in range(len(tab)):
            if i == prow:
                continue
            factor = tab[i][pcol]
            if factor:
                tab[i] = [a - factor * b for a, b in zip(tab[i], tab[prow])]
        basis[prow] = pcol

    def optimise(obj_costs: List[Fraction], banned: set) -> Tuple[str, Optional[Fraction]]:
        """Append the objective row and run Bland's simplex to optimality."""

        obj = obj_costs[:] + [Fraction(0)]
        for i in range(m):
            b = basis[i]
            if obj_costs[b]:
                obj = [a - obj_costs[b] * r for a, r in zip(obj, tab[i])]
        tab.append(obj)
        # Invariant: obj_row[j] is the reduced cost of column j and the last
        # entry of the objective row is the *negated* objective value.
        while True:
            obj_row = tab[m]
            entering = None
            for j in range(ncols):
                if j in banned or j in basis:
                    continue
                if obj_row[j] < 0:
                    entering = j
                    break  # Bland: smallest index
            if entering is None:
                return ("optimal", -tab[m][ncols])
            leaving = None
            best_ratio: Optional[Fraction] = None
            for i in range(m):
                a = tab[i][entering]
                if a > 0:
                    ratio = tab[i][ncols] / a
                    if (
                        best_ratio is None
                        or ratio < best_ratio
                        or (ratio == best_ratio and basis[i] < basis[leaving])
                    ):
                        best_ratio = ratio
                        leaving = i
            if leaving is None:
                tab.pop()
                return ("unbounded", None)
            pivot(leaving, entering)

    # ---- Phase I: minimise the sum of artificials --------------------------
    costs1 = [Fraction(0)] * ncols
    for j in art_col.values():
        costs1[j] = Fraction(1)
    status, phase_one = optimise(costs1, set())
    assert status == "optimal" and phase_one is not None  # artificials bound it
    if phase_one > 0:
        obj_row = tab[m]
        y: List[Fraction] = []
        for i in range(m):
            if i in art_col:
                y_i = Fraction(1) - obj_row[art_col[i]]
            else:
                y_i = -obj_row[aux_col[i]]
            if norm[i][4]:  # row was multiplied by -1 during normalisation
                y_i = -y_i
            y.append(y_i)
        # Defensive re-check of the Farkas conditions against the caller's rows.
        for j in range(n):
            lhs = sum(y[i] * Fraction(rows[i][0][j]) for i in range(m))
            assert lhs <= 0, "Farkas certificate violates a column condition"
        assert sum(y[i] * Fraction(rows[i][2]) for i in range(m)) > 0
        for i in range(m):
            sense = rows[i][1]
            if sense == ">=":
                assert y[i] >= 0
            elif sense == "<=":
                assert y[i] <= 0
        return ("infeasible", y)

    # ---- drive remaining basic artificials out (they sit at value 0) -------
    art_set = set(art_col.values())
    keep: List[int] = []
    for i in range(m):
        if basis[i] in art_set:
            for j in range(ncols):
                if j not in art_set and tab[i][j] != 0:
                    pivot(i, j)
                    break
            else:
                continue  # redundant row: drop it
        keep.append(i)
    if len(keep) < m:
        tab[:] = [tab[i] for i in keep]
        basis[:] = [basis[i] for i in keep]
        m = len(keep)

    # ---- Phase II: optimise the real objective -----------------------------
    costs2 = [Fraction(v) for v in costs] + [Fraction(0)] * (ncols - n)
    status, objective = optimise(costs2, art_set)
    if status == "unbounded":
        return ("unbounded", None)
    x = [Fraction(0)] * n
    for i in range(m):
        if basis[i] < n:
            x[basis[i]] = tab[i][ncols]
    return ("optimal", x, objective)


# ---------------------------------------------------------------------------
# Finite-domain constraint solver (exact, deterministic).
# ---------------------------------------------------------------------------


def _cp_propagate(domains: List[List[int]], constraints: Sequence[Tuple[Tuple[int, ...], Tuple[int, ...], int]]) -> Optional[List[List[int]]]:
    """Bounds-consistency fixpoint; returns tightened domains or ``None``."""

    changed = True
    while changed:
        changed = False
        for pos, neg, rhs in constraints:
            sum_hi_pos = sum(domains[j][1] for j in pos)
            sum_lo_neg = sum(domains[j][0] for j in neg)
            if sum_hi_pos - sum_lo_neg < rhs:
                return None
            for j in pos:
                need = rhs - (sum_hi_pos - domains[j][1]) + sum_lo_neg
                if need > domains[j][0]:
                    domains[j][0] = need
                    if domains[j][0] > domains[j][1]:
                        return None
                    changed = True
            for k in neg:
                cap = -rhs + sum_hi_pos - (sum_lo_neg - domains[k][0])
                if cap < domains[k][1]:
                    domains[k][1] = cap
                    if domains[k][0] > domains[k][1]:
                        return None
                    changed = True
    return domains


def _cp_solve(domains: List[List[int]], constraints: Sequence[Tuple[Tuple[int, ...], Tuple[int, ...], int]]) -> Optional[List[int]]:
    """Depth-first search with propagation; first feasible integer point."""

    domains = _cp_propagate([d[:] for d in domains], constraints)
    if domains is None:
        return None
    var = None
    for j in range(len(domains)):
        lo, hi = domains[j]
        if lo < hi and (var is None or hi - lo < domains[var][1] - domains[var][0]):
            var = j
    if var is None:
        # Singleton domains: propagation already checked every constraint.
        return [d[0] for d in domains]
    lo, hi = domains[var]
    for value in range(lo, hi + 1):
        narrowed = [d[:] for d in domains]
        narrowed[var] = [value, value]
        result = _cp_solve(narrowed, constraints)
        if result is not None:
            return result
    return None


# ---------------------------------------------------------------------------
# Certificates.
# ---------------------------------------------------------------------------


def check_farkas_certificate(cert: Mapping[str, Any], values: Mapping[frozenset, int], minimums: Mapping[str, int], total: int) -> bool:
    """Arithmetic check of a Farkas certificate against the true inputs."""

    order = cert["counter_order"]
    sums = {c: 0 for c in order}
    rhs = 0
    for con in cert["constraints"]:
        weight = con["weight"]
        if con["kind"] == "coalition":
            if values.get(frozenset(con["counters"])) != con["value"]:
                return False
            for counter in con["counters"]:
                sums[counter] += weight
            rhs += weight * con["value"]
        elif con["kind"] == "minimum":
            if minimums.get(con["counter"]) != con["value"]:
                return False
            sums[con["counter"]] += weight
            rhs += weight * con["value"]
        elif con["kind"] == "total":
            if con["value"] != total:
                return False
            for counter in order:
                sums[counter] += weight
            rhs += weight * con["value"]
        else:  # pragma: no cover - defensive
            return False
    return all(amount == 0 for amount in sums.values()) and rhs > 0


def _build_farkas_certificate(
    counter_order: Sequence[str],
    minimums: Mapping[str, int],
    total: int,
    values: Mapping[frozenset, int],
    weighted_rows: Sequence[Tuple[Any, Fraction]],
) -> Dict[str, Any]:
    """Translate simplex Farkas weights over ``x' = x - m`` rows into a
    certificate over the original constraints (coalitions, minimums, total)."""

    n = len(counter_order)
    coal_weights: List[Tuple[int, Fraction]] = []
    total_weight = Fraction(0)
    for tag, weight in weighted_rows:
        if tag[0] == "coalition":
            coal_weights.append((tag[1], weight))
        elif tag[0] == "total":
            total_weight += weight
    min_weights: List[Fraction] = []
    for i in range(n):
        acc = total_weight
        for mask, weight in coal_weights:
            if mask >> i & 1:
                acc += weight
        min_weights.append(-acc)  # must be >= 0 by the Farkas column condition

    denominators = [w.denominator for _, w in coal_weights]
    denominators.append(total_weight.denominator)
    denominators.extend(w.denominator for w in min_weights)
    scale = 1
    for denominator in denominators:
        scale = scale * denominator // gcd(scale, denominator)

    constraints: List[Dict[str, Any]] = []
    for mask, weight in coal_weights:
        counters = [counter_order[j] for j in range(n) if mask >> j & 1]
        constraints.append(
            {
                "kind": "coalition",
                "counters": counters,
                "value": values[frozenset(counters)],
                "weight": int(weight * scale),
            }
        )
    for i, weight in enumerate(min_weights):
        if weight > 0:
            constraints.append(
                {
                    "kind": "minimum",
                    "counter": counter_order[i],
                    "value": minimums[counter_order[i]],
                    "weight": int(weight * scale),
                }
            )
    constraints.append({"kind": "total", "value": total, "weight": int(total_weight * scale)})
    cert = {
        "type": "farkas",
        "counter_order": list(counter_order),
        "total": total,
        "constraints": constraints,
        "scale": scale,
    }
    assert check_farkas_certificate(cert, values, minimums, total)
    return cert


def _build_minimums_certificate(counter_order: Sequence[str], minimums: Mapping[str, int], total: int, values: Mapping[frozenset, int]) -> Dict[str, Any]:
    """Trivial certificate: the confirmed minimums alone exceed the total."""

    constraints = [
        {"kind": "minimum", "counter": counter, "value": minimums[counter], "weight": 1}
        for counter in counter_order
    ]
    constraints.append({"kind": "total", "value": total, "weight": -1})
    cert = {"type": "farkas", "counter_order": list(counter_order), "total": total, "constraints": constraints, "scale": 1}
    assert check_farkas_certificate(cert, values, minimums, total)
    return cert


# ---------------------------------------------------------------------------
# Coalition constraint pruning (sound for any values map).
# ---------------------------------------------------------------------------


def _kept_coalitions(n: int, w: Mapping[int, int]) -> List[int]:
    """Keep only coalition constraints not implied by disjoint sub-coalitions.

    ``w[mask]`` is the (clamped) right-hand side.  A constraint ``x(S) >= w_S``
    is implied whenever ``S`` splits into disjoint ``S1, S2`` with
    ``w_S <= w_{S1} + w_{S2}``; induction over subset size makes the rule
    sound transitively, so the kept constraints imply every dropped one.
    """

    kept: List[int] = []
    for mask in range(1, 1 << n):
        if w[mask] <= 0:
            continue
        decomposable = False
        sub = (mask - 1) & mask
        while sub:
            other = mask ^ sub
            if sub < other and w[mask] <= w.get(sub, 0) + w.get(other, 0):
                decomposable = True
                break
            sub = (sub - 1) & mask
        if not decomposable:
            kept.append(mask)
    return kept


# ---------------------------------------------------------------------------
# Main entry point.
# ---------------------------------------------------------------------------


def solve_split(
    counter_order: Sequence[str],
    minimums: Mapping[str, int],
    total: int,
    values: Mapping[frozenset, int],
) -> Dict[str, Any]:
    """Solve one settlement instance exactly.

    ``counter_order`` is the stable counter-id order used for the
    lexicographic tie-break; ``values`` must contain every non-empty
    coalition.  Returns a JSON-safe dict with either ``status == "optimal"``
    (plus the split and optimality evidence) or ``status == "infeasible"``
    (plus a re-checkable certificate).
    """

    n = len(counter_order)
    if n == 0 or len(set(counter_order)) != n:
        raise BasketError(INVALID_INPUT, "counter_order must be a non-empty list of unique counters")
    if sorted(minimums) != sorted(counter_order):
        raise BasketError(INVALID_INPUT, "minimums must cover exactly the counters")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise BasketError(INVALID_INPUT, "total must be a non-negative integer")
    expected = (1 << n) - 1
    if len(values) != expected:
        raise BasketError(INVALID_INPUT, "values must cover every non-empty coalition", {"expected": expected, "got": len(values)})

    m = [minimums[c] for c in counter_order]
    index = {c: i for i, c in enumerate(counter_order)}
    mask_of = {}
    for key in values:
        mask = 0
        for counter in key:
            mask |= 1 << index[counter]
        mask_of[key] = mask

    b: Dict[int, int] = {}
    for key, mask in mask_of.items():
        b[mask] = values[key] - sum(m[i] for i in range(n) if mask >> i & 1)
    Tp = total - sum(m)

    base = {
        "counter_order": list(counter_order),
        "total": total,
        "minimums": {c: minimums[c] for c in counter_order},
    }
    if Tp < 0:
        return {
            **base,
            "status": "infeasible",
            "reason": "minimums_exceed_total",
            "certificate": _build_minimums_certificate(counter_order, minimums, total, values),
        }

    w = {mask: max(b[mask], 0) for mask in b}
    kept = _kept_coalitions(n, w)
    kept_evidence = [
        {
            "counters": [counter_order[j] for j in range(n) if mask >> j & 1],
            "value": values[frozenset(counter_order[j] for j in range(n) if mask >> j & 1)],
            "rhs_after_minimums": b[mask],
        }
        for mask in kept
    ]

    # ---- fractional feasibility with constraint generation -----------------
    rows: List[Tuple[List[Fraction], str, Fraction, Any]] = [
        ([Fraction(1)] * n, "==", Fraction(Tp), ("total",)),
    ]
    present = set()
    for mask in kept:
        if mask & (mask - 1) == 0:  # seed with singletons
            rows.append(([Fraction(1 if mask >> j & 1 else 0) for j in range(n)], ">=", Fraction(b[mask]), ("coalition", mask)))
            present.add(mask)
    while True:
        status, *payload = _simplex_min([Fraction(0)] * n, rows)
        if status == "infeasible":
            y = payload[0]
            weighted = [(rows[i][3], y[i]) for i in range(len(rows)) if y[i] != 0]
            return {
                **base,
                "status": "infeasible",
                "reason": "fractional_core_empty",
                "certificate": _build_farkas_certificate(counter_order, minimums, total, values, weighted),
                "kept_coalitions": kept_evidence,
            }
        assert status == "optimal"
        x_star = payload[0]
        violated = [
            mask
            for mask in kept
            if mask not in present and sum(x_star[j] for j in range(n) if mask >> j & 1) < b[mask]
        ]
        if not violated:
            break
        for mask in violated:
            rows.append(([Fraction(1 if mask >> j & 1 else 0) for j in range(n)], ">=", Fraction(b[mask]), ("coalition", mask)))
            present.add(mask)

    # ---- range LP: minimise max(x) - min(x) fractionally -------------------
    LO, RR = n, n + 1
    rrows: List[Tuple[List[Fraction], str, Fraction, Any]] = [
        ([Fraction(1)] * n + [Fraction(0), Fraction(0)], "==", Fraction(Tp), ("total",)),
    ]
    for i in range(n):
        row = [Fraction(0)] * (n + 2)
        row[i] = Fraction(-1)
        row[LO] = Fraction(1)
        rrows.append((row, "<=", Fraction(m[i]), ("window-lo", i)))
        row = [Fraction(0)] * (n + 2)
        row[i] = Fraction(1)
        row[LO] = Fraction(-1)
        row[RR] = Fraction(-1)
        rrows.append((row, "<=", Fraction(-m[i]), ("window-hi", i)))
    rpresent = set()
    for mask in kept:
        if mask & (mask - 1) == 0:
            rrows.append(
                ([Fraction(1 if mask >> j & 1 else 0) for j in range(n)] + [Fraction(0), Fraction(0)], ">=", Fraction(b[mask]), ("coalition", mask))
            )
            rpresent.add(mask)
    objective = [Fraction(0)] * (n + 2)
    objective[RR] = Fraction(1)
    while True:
        status, *payload = _simplex_min(objective, rrows)
        if status == "infeasible":  # pragma: no cover - feasibility LP already proved feasible
            y = payload[0]
            weighted = [(rrows[i][3], y[i]) for i in range(len(rrows)) if y[i] != 0 and rrows[i][3][0] in ("coalition", "total")]
            return {
                **base,
                "status": "infeasible",
                "reason": "fractional_core_empty",
                "certificate": _build_farkas_certificate(counter_order, minimums, total, values, weighted),
                "kept_coalitions": kept_evidence,
            }
        x_star = payload[0]
        violated = [
            mask
            for mask in kept
            if mask not in rpresent and sum(x_star[j] for j in range(n) if mask >> j & 1) < b[mask]
        ]
        if not violated:
            frac_range = payload[1]
            break
        for mask in violated:
            rrows.append(
                ([Fraction(1 if mask >> j & 1 else 0) for j in range(n)] + [Fraction(0), Fraction(0)], ">=", Fraction(b[mask]), ("coalition", mask))
            )
            rpresent.add(mask)

    # ---- integer search over the spread R ----------------------------------
    upper = [Tp - max(b.get((1 << n) - 1 ^ (1 << i), 0), 0) for i in range(n)]
    coalition_constraints: List[Tuple[Tuple[int, ...], Tuple[int, ...], int]] = [
        (tuple(j for j in range(n) if mask >> j & 1), (), b[mask]) for mask in kept
    ]
    sum_constraints: List[Tuple[Tuple[int, ...], Tuple[int, ...], int]] = [
        (tuple(range(n)), (), Tp),
        ((), tuple(range(n)), -Tp),
    ]
    mu = n  # auxiliary variable: the minimum share (in original x terms)
    r0 = (frac_range.numerator + frac_range.denominator - 1) // frac_range.denominator
    r_max = max(m[i] + upper[i] for i in range(n)) - min(m[i] for i in range(n))
    assignment = None
    chosen_r = None
    for r in range(r0, r_max + 1):
        window: List[Tuple[Tuple[int, ...], Tuple[int, ...], int]] = []
        for i in range(n):
            window.append(((i,), (mu,), -m[i]))  # mu <= x'_i + m_i
            window.append(((mu,), (i,), m[i] - r))  # x'_i + m_i <= mu + r
        domains = [[0, upper[i]] for i in range(n)] + [[min(m), max(m[i] + upper[i] for i in range(n))]]
        assignment = _cp_solve(domains, coalition_constraints + sum_constraints + window)
        if assignment is not None:
            chosen_r = r
            break

    if assignment is None:
        domains_x = [[m[i], m[i] + upper[i]] for i in range(n)]
        points = 1
        for lo, hi in domains_x:
            points *= hi - lo + 1
        if points > 2_000_000:  # pragma: no cover - defensive, demos never hit this
            raise BasketError(INVALID_INPUT, "integer enumeration certificate would be too large", {"points": points})
        certificate = {
            "type": "integer_enumeration",
            "counter_order": list(counter_order),
            "total": total,
            "minimums": {c: minimums[c] for c in counter_order},
            "domains": domains_x,
            "values": [
                {"counters": sorted(key), "value": values[key]} for key in sorted(values, key=lambda k: (len(k), sorted(k)))
            ],
            "points_checked": points,
        }
        return {
            **base,
            "status": "infeasible",
            "reason": "integer_core_empty",
            "certificate": certificate,
            "kept_coalitions": kept_evidence,
            "fractional_range": str(frac_range),
        }

    # ---- lexicographic refinement in stable counter-id order ---------------
    window = []
    for i in range(n):
        window.append(((i,), (mu,), -m[i]))
        window.append(((mu,), (i,), m[i] - chosen_r))
    fixed: List[Tuple[int, int]] = []  # (variable, value) already chosen
    for i in range(n):
        lo, hi = 0, upper[i]
        # binary search the smallest feasible value of x'_i
        while lo < hi:
            mid = (lo + hi) // 2
            domains = [[0, upper[j]] for j in range(n)] + [[min(m), max(m[j] + upper[j] for j in range(n))]]
            for var, value in fixed:
                domains[var] = [value, value]
            domains[i] = [0, mid]
            if _cp_solve(domains, coalition_constraints + sum_constraints + window) is not None:
                hi = mid
            else:
                lo = mid + 1
        fixed.append((i, lo))
    amounts = [m[i] + fixed[i][1] for i in range(n)]
    split = {counter_order[i]: amounts[i] for i in range(n)}
    return {
        **base,
        "status": "optimal",
        "split": split,
        "amounts": amounts,
        "range": max(amounts) - min(amounts),
        "fractional_range_bound": str(frac_range),
        "kept_coalitions": kept_evidence,
    }
