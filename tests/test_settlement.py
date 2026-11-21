"""Tests for the exact settlement split solver (basket_service.settlement)."""

from __future__ import annotations

import random
import unittest
from itertools import combinations

from basket_service.errors import BasketError
from basket_service.reference import (
    brute_force_split,
    verify_enumeration_certificate,
    verify_farkas_certificate,
)
from basket_service.settlement import solve_split


def values_from(spec, counters):
    values = {}
    for r in range(1, len(counters) + 1):
        for combo in combinations(counters, r):
            values[frozenset(combo)] = spec.get(frozenset(combo), 0)
    return values


ABC = ["A", "B", "C"]
ZERO3 = {"A": 0, "B": 0, "C": 0}


class NamedScenarioTests(unittest.TestCase):
    def test_three_pairs_cannot_split_4(self):
        spec = {frozenset("AB"): 4, frozenset("AC"): 4, frozenset("BC"): 4, frozenset("ABC"): 4}
        values = values_from(spec, ABC)
        result = solve_split(ABC, ZERO3, 4, values)
        self.assertEqual(result["status"], "infeasible")
        cert = result["certificate"]
        self.assertEqual(cert["type"], "farkas")
        # 2*(a+b+c) >= 12 from the three pair constraints vs 2*4 = 8.
        self.assertEqual(
            sorted(tuple(c["counters"]) for c in cert["constraints"] if c["kind"] == "coalition"),
            [("A", "B"), ("A", "C"), ("B", "C")],
        )
        self.assertEqual([c for c in cert["constraints"] if c["kind"] == "total"][0]["weight"], -2)
        self.assertTrue(verify_farkas_certificate(cert, values, ZERO3, 4)["valid"])

    def test_grand_coupon_makes_even_split(self):
        spec = {frozenset("AB"): 4, frozenset("AC"): 4, frozenset("BC"): 4, frozenset("ABC"): 6}
        result = solve_split(ABC, ZERO3, 6, values_from(spec, ABC))
        self.assertEqual(result["status"], "optimal")
        self.assertEqual(result["split"], {"A": 2, "B": 2, "C": 2})
        self.assertEqual(result["range"], 0)

    def test_confirmed_minimum_blocks_despite_larger_total(self):
        spec = {frozenset("AB"): 4, frozenset("AC"): 4, frozenset("BC"): 4, frozenset("ABC"): 6}
        values = values_from(spec, ABC)
        result = solve_split(ABC, {"A": 3, "B": 0, "C": 0}, 6, values)
        self.assertEqual(result["status"], "infeasible")
        cert = result["certificate"]
        kinds = sorted((c["kind"], tuple(c.get("counters") or [c.get("counter")])) for c in cert["constraints"])
        self.assertEqual(kinds, [("coalition", ("B", "C")), ("minimum", ("A",)), ("total", (None,))])
        self.assertTrue(verify_farkas_certificate(cert, values, {"A": 3, "B": 0, "C": 0}, 6)["valid"])

    def test_membership_changes_exit_values_and_split(self):
        spec = {
            frozenset("A"): 3,
            frozenset("AB"): 3,
            frozenset("AC"): 3,
            frozenset("BC"): 3,
            frozenset("ABC"): 6,
        }
        result = solve_split(ABC, ZERO3, 6, values_from(spec, ABC))
        self.assertEqual(result["status"], "optimal")
        self.assertEqual(result["split"], {"A": 3, "B": 1, "C": 2})
        self.assertEqual(result["range"], 2)

    def test_twelve_counters(self):
        counters = [f"C{i:02d}" for i in range(1, 13)]
        spec = {}
        for k in range(6):
            spec[frozenset([counters[2 * k], counters[2 * k + 1]])] = 5
        spec[frozenset(counters)] = 40
        minimums = {c: 0 for c in counters}
        result = solve_split(counters, minimums, 40, values_from(spec, counters))
        self.assertEqual(result["status"], "optimal")
        self.assertEqual(result["amounts"], [3] * 8 + [4] * 4)
        self.assertEqual(result["range"], 1)


class CertificateTests(unittest.TestCase):
    def test_minimums_exceed_total(self):
        spec = {frozenset("A"): 1}
        values = values_from(spec, ["A"])
        result = solve_split(["A"], {"A": 5}, 4, values)
        self.assertEqual(result["status"], "infeasible")
        self.assertEqual(result["reason"], "minimums_exceed_total")
        self.assertTrue(verify_farkas_certificate(result["certificate"], values, {"A": 5}, 4)["valid"])

    def test_integer_only_infeasibility_gets_enumeration_certificate(self):
        # All pairs and triples value 3, grand 6: the fractional core contains
        # (1.5,)*4 but no integer point can satisfy every pair.
        counters = ["A", "B", "C", "D"]
        spec = {}
        for r in (2, 3):
            for combo in combinations(counters, r):
                spec[frozenset(combo)] = 3
        spec[frozenset(counters)] = 6
        values = values_from(spec, counters)
        zero = {c: 0 for c in counters}
        result = solve_split(counters, zero, 6, values)
        self.assertEqual(result["status"], "infeasible")
        self.assertEqual(result["reason"], "integer_core_empty")
        cert = result["certificate"]
        self.assertEqual(cert["type"], "integer_enumeration")
        self.assertTrue(verify_enumeration_certificate(cert)["valid"])
        self.assertIsNone(brute_force_split(counters, zero, 6, values))

    def test_certificate_verifier_rejects_tampering(self):
        spec = {frozenset("AB"): 4, frozenset("AC"): 4, frozenset("BC"): 4, frozenset("ABC"): 4}
        values = values_from(spec, ABC)
        cert = solve_split(ABC, ZERO3, 4, values)["certificate"]
        verdict = verify_farkas_certificate(cert, values, ZERO3, 4)
        self.assertTrue(verdict["valid"])
        # Tamper: drop one coalition constraint.
        broken = dict(cert)
        broken["constraints"] = [c for c in cert["constraints"] if c["kind"] != "coalition" or c["counters"] != ["A", "B"]]
        self.assertFalse(verify_farkas_certificate(broken, values, ZERO3, 4)["valid"])
        # Tamper: claim a different coalition value.
        self.assertFalse(verify_farkas_certificate(cert, values, ZERO3, 5)["valid"])

    def test_single_counter(self):
        values = values_from({frozenset("A"): 2}, ["A"])
        self.assertEqual(solve_split(["A"], {"A": 0}, 5, values)["split"], {"A": 5})
        result = solve_split(["A"], {"A": 0}, 1, values)
        self.assertEqual(result["status"], "infeasible")

    def test_zero_total_zero_split(self):
        values = values_from({}, ABC)
        result = solve_split(ABC, ZERO3, 0, values)
        self.assertEqual(result["status"], "optimal")
        self.assertEqual(result["split"], {"A": 0, "B": 0, "C": 0})

    def test_input_validation(self):
        values = values_from({}, ABC)
        with self.assertRaises(BasketError):
            solve_split(ABC, {"A": 0}, 0, values)  # minimums do not cover counters
        with self.assertRaises(BasketError):
            solve_split(ABC, ZERO3, 0, {frozenset("A"): 0})  # incomplete values
        with self.assertRaises(BasketError):
            solve_split(ABC, ZERO3, -1, values)


class DifferentialTests(unittest.TestCase):
    """solve_split vs exhaustive enumeration on random coupon-like games."""

    def random_game(self, rng, n):
        counters = [f"C{i}" for i in range(n)]
        coupons = []
        for _ in range(rng.randint(0, 4)):
            subset = frozenset(rng.sample(counters, rng.randint(1, n)))
            coupons.append((subset, rng.randint(1, 8)))
        values = {}
        for r in range(1, n + 1):
            for combo in combinations(counters, r):
                subset = frozenset(combo)
                best = 0
                for mask in range(1, 1 << len(coupons)):
                    total, used, ok = 0, frozenset(), True
                    for k, (members, discount) in enumerate(coupons):
                        if mask >> k & 1:
                            if members & used or not members <= subset:
                                ok = False
                                break
                            used |= members
                            total += discount
                    if ok:
                        best = max(best, total)
                values[subset] = best
        total = values[frozenset(counters)]
        if rng.random() < 0.5:
            total = max(0, total + rng.randint(-2, 2))
        minimums = {c: (rng.randint(0, 3) if rng.random() < 0.4 else 0) for c in counters}
        return counters, minimums, total, values

    def check_game(self, counters, minimums, total, values):
        result = solve_split(counters, minimums, total, values)
        expected = brute_force_split(counters, minimums, total, values)
        if expected is None:
            self.assertEqual(result["status"], "infeasible", (counters, minimums, total))
            cert = result["certificate"]
            if cert["type"] == "farkas":
                verdict = verify_farkas_certificate(cert, values, minimums, total)
            else:
                verdict = verify_enumeration_certificate(cert)
            self.assertTrue(verdict["valid"], verdict.get("problems"))
        else:
            self.assertEqual(result["status"], "optimal", (counters, minimums, total))
            self.assertEqual(result["split"], expected)

    def test_differential_small(self):
        rng = random.Random(20261121)
        for _ in range(150):
            counters, minimums, total, values = self.random_game(rng, rng.randint(1, 4))
            self.check_game(counters, minimums, total, values)

    def test_differential_medium(self):
        rng = random.Random(777)
        for _ in range(40):
            counters, minimums, total, values = self.random_game(rng, rng.randint(5, 6))
            self.check_game(counters, minimums, total, values)


if __name__ == "__main__":
    unittest.main()
