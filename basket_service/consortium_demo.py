"""Consortium settlement demo: real sales -> coalition quotes -> signed splits.

Run ``python3 -m basket_service consortium-demo`` to (re)generate::

    data/consortium.db              real SQLite database
    results/consortium_results.json every scenario, certificate and check
    results/consortium_plan.json    the JSON plan of the main signed scenario

Scenarios (all amounts are synthetic points):

* ``pair_lock``        A/B/C, three pair coupons D=4 fighting over items:
                       the grand coalition saves 4, every pair can save 4 on
                       its own, and no split of 4 satisfies all three pairs
                       (Farkas certificate from the real coalition quotes).
* ``grand_coupon``     adding a three-counter coupon D=6 makes (2,2,2) exact.
* ``minimum_blocks``   same deals and coalition quotes, only A's confirmed
                       minimum raised to 3: total 6 > 3 yet no split exists.
* ``membership``       same grand savings 6 with different coupon membership:
                       coalition values move and the split becomes (3,1,2).
* ``refund``           a signed settlement stays queryable after a refund,
                       while the refunded unit cannot support the next deal.
* ``twelve_counters``  12 counters, 4095 coalitions quoted and checked for
                       exit, repeated coalition queries replayed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from . import open_service
from .demo import _write_json

ROOT = Path(__file__).resolve().parent.parent

PRODUCTS3 = [{"sku": "A", "price": 10}, {"sku": "B", "price": 10}, {"sku": "C", "price": 10}]
BASKET3 = [
    {"unit_id": "a", "sku": "A", "price": 10},
    {"unit_id": "b", "sku": "B", "price": 10},
    {"unit_id": "c", "sku": "C", "price": 10},
]
PAIR_COUPONS = [
    {"coupon_id": "AB", "D": 4, "requires": {"A": 1, "B": 1}},
    {"coupon_id": "AC", "D": 4, "requires": {"A": 1, "C": 1}},
    {"coupon_id": "BC", "D": 4, "requires": {"B": 1, "C": 1}},
]
GRAND_COUPON = {"coupon_id": "ABC", "D": 6, "requires": {"A": 1, "B": 1, "C": 1}}
PAIR_CONSENT = {
    "AB": {"must": ["A", "B"]},
    "AC": {"must": ["A", "C"]},
    "BC": {"must": ["B", "C"]},
}
ZERO_MINIMUMS = {"A": 0, "B": 0, "C": 0}


def _certificate_summary(cert: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {k: constraint[k] for k in ("kind", "counters", "counter", "value", "weight") if k in constraint}
        for constraint in cert["constraints"]
    ]


def run_consortium_demo(db_path: str, out_path: str, plan_path: str) -> Dict[str, Any]:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(db_path + suffix)
        if candidate.exists():
            candidate.unlink()

    svc = open_service(db_path)
    results: Dict[str, Any] = {"database": str(db_path), "scenarios": {}, "checks": []}

    def check(name: str, ok: bool, expected: Any = None, actual: Any = None) -> None:
        results["checks"].append({"check": name, "ok": bool(ok), "expected": expected, "actual": actual})

    # ------------------------------------------------------------------
    # 1. pair_lock: three pair coupons, grand saves 4, every pair can leave
    #    with 4 -> no split of 4 can satisfy all three pairs.
    # ------------------------------------------------------------------
    snap_a = svc.publish_snapshot(PRODUCTS3, PAIR_COUPONS, snapshot_id="pairs-only")
    quote_a = svc.quote(BASKET3, [], "q-a", snap_a)
    svc.register_sale("S-a", quote=quote_a)
    svc.create_consortium("cons-a", snap_a, ["A", "B", "C"], PAIR_CONSENT, ZERO_MINIMUMS)
    svc.create_deal("deal-a", "cons-a", ["S-a"], {"S-a:a": "A", "S-a:b": "B", "S-a:c": "C"})
    pair_quotes = {pair: svc.quote_coalition("deal-a", list(pair)) for pair in ("AB", "AC", "BC")}
    settle_a = svc.attempt_settlement("deal-a", "settle-a")
    verify_a = svc.verify_settlement("settle-a")
    cert_a = settle_a["solver"]["certificate"]
    results["scenarios"]["pair_lock"] = {
        "grand_quote_discount": quote_a["total_discount"],
        "pair_quotes": {pair: {"savings": q["savings"], "eligible": q["eligible_coupon_ids"]} for pair, q in pair_quotes.items()},
        "settlement": {"status": settle_a["status"], "total": settle_a["total"]},
        "certificate": cert_a,
        "certificate_summary": _certificate_summary(cert_a),
        "independent_verification": verify_a,
    }
    check("pair_lock.grand_saves_4", quote_a["total_discount"] == 4, 4, quote_a["total_discount"])
    check("pair_lock.every_pair_quotes_4", all(q["savings"] == 4 for q in pair_quotes.values()), 4, {p: q["savings"] for p, q in pair_quotes.items()})
    check("pair_lock.rejected", settle_a["status"] == "REJECTED", "REJECTED", settle_a["status"])
    check(
        "pair_lock.certificate_is_the_three_pairs",
        sorted(c["counters"] for c in cert_a["constraints"] if c["kind"] == "coalition") == [["A", "B"], ["A", "C"], ["B", "C"]]
        and [c for c in cert_a["constraints"] if c["kind"] == "total"][0]["weight"] == -2,
    )
    check("pair_lock.certificate_verified", verify_a["valid"], True, verify_a["problems"])

    # ------------------------------------------------------------------
    # 2. grand_coupon: adding a three-counter coupon D=6 -> (2,2,2) signs.
    # ------------------------------------------------------------------
    snap_b = svc.publish_snapshot(PRODUCTS3, PAIR_COUPONS + [GRAND_COUPON], snapshot_id="pairs-plus-grand")
    quote_b = svc.quote(BASKET3, [], "q-b", snap_b)
    svc.register_sale("S-b", quote=quote_b)
    consent_b = {**PAIR_CONSENT, "ABC": {"must": ["A", "B", "C"]}}
    svc.create_consortium("cons-b", snap_b, ["A", "B", "C"], consent_b, ZERO_MINIMUMS)
    svc.create_deal("deal-b", "cons-b", ["S-b"], {"S-b:a": "A", "S-b:b": "B", "S-b:c": "C"})
    settle_b = svc.attempt_settlement("deal-b", "settle-b")
    noexit_b = svc.check_no_exit("settle-b")
    verify_b = svc.verify_settlement("settle-b")
    results["scenarios"]["grand_coupon"] = {
        "grand_quote": {"discount": quote_b["total_discount"], "coupon_ids": quote_b["coupon_ids"]},
        "settlement": {"status": settle_b["status"], "split": settle_b["split"], "range": settle_b["range"]},
        "no_exit": noexit_b,
        "independent_verification": verify_b,
    }
    check("grand_coupon.signed_2_2_2", settle_b["status"] == "SIGNED" and settle_b["split"] == {"A": 2, "B": 2, "C": 2}, {"A": 2, "B": 2, "C": 2}, settle_b["split"])
    check("grand_coupon.no_exit", noexit_b["stable"] and noexit_b["coalitions_checked"] == 7, True, noexit_b["violations"])
    check("grand_coupon.verified", verify_b["valid"], True, verify_b["problems"])

    # ------------------------------------------------------------------
    # 3. minimum_blocks: identical deals and coalition quotes, only A's
    #    confirmed minimum is 3 -> cannot sign even though 6 > 3.
    # ------------------------------------------------------------------
    quote_c = svc.quote(BASKET3, [], "q-c", snap_b)
    svc.register_sale("S-c", quote=quote_c)
    svc.create_consortium("cons-c", snap_b, ["A", "B", "C"], consent_b, {"A": 3, "B": 0, "C": 0})
    svc.create_deal("deal-c", "cons-c", ["S-c"], {"S-c:a": "A", "S-c:b": "B", "S-c:c": "C"})
    settle_c = svc.attempt_settlement("deal-c", "settle-c")
    verify_c = svc.verify_settlement("settle-c")
    cert_c = settle_c["solver"]["certificate"]
    results["scenarios"]["minimum_blocks"] = {
        "note": "same snapshot/sales/coalition quotes as grand_coupon; only A's confirmed minimum is 3",
        "settlement": {"status": settle_c["status"], "total": settle_c["total"]},
        "certificate_summary": _certificate_summary(cert_c),
        "independent_verification": verify_c,
    }
    check("minimum_blocks.rejected", settle_c["status"] == "REJECTED", "REJECTED", settle_c["status"])
    check(
        "minimum_blocks.total_exceeds_minimum_yet_infeasible",
        settle_c["total"] == 6 and settle_c["total"] > 3,
        "6 > 3",
        settle_c["total"],
    )
    check(
        "minimum_blocks.certificate_is_bc_coalition_plus_minimum",
        sorted((c["kind"], tuple(c.get("counters") or [c.get("counter")]), c["weight"]) for c in cert_c["constraints"])
        == [("coalition", ("B", "C"), 1), ("minimum", ("A",), 1), ("total", (None,), -1)],
    )
    check("minimum_blocks.certificate_verified", verify_c["valid"], True, verify_c["problems"])

    # ------------------------------------------------------------------
    # 4. membership: same grand savings 6, different coupon membership ->
    #    different coalition exit values and a different split (3,1,2).
    # ------------------------------------------------------------------
    coupons_d = [
        {"coupon_id": "A-SOLO", "D": 3, "requires": {"A": 1}},
        {"coupon_id": "BC", "D": 3, "requires": {"B": 1, "C": 1}},
        {"coupon_id": "ABC6", "D": 6, "requires": {"A": 1, "B": 1, "C": 1}},
    ]
    consent_d = {"A-SOLO": {"must": ["A"]}, "BC": {"must": ["B", "C"]}, "ABC6": {"must": ["A", "B", "C"]}}
    snap_d = svc.publish_snapshot(PRODUCTS3, coupons_d, snapshot_id="membership")
    quote_d = svc.quote(BASKET3, [], "q-d", snap_d)
    svc.register_sale("S-d", quote=quote_d)
    svc.create_consortium("cons-d", snap_d, ["A", "B", "C"], consent_d, ZERO_MINIMUMS)
    svc.create_deal("deal-d", "cons-d", ["S-d"], {"S-d:a": "A", "S-d:b": "B", "S-d:c": "C"})
    ac_quote = svc.quote_coalition("deal-d", ["A", "C"])
    settle_d = svc.attempt_settlement("deal-d", "settle-d")
    verify_d = svc.verify_settlement("settle-d")
    results["scenarios"]["membership"] = {
        "grand_quote": {"discount": quote_d["total_discount"], "coupon_ids": quote_d["coupon_ids"]},
        "coalition_AC": {
            "savings": ac_quote["savings"],
            "eligible": ac_quote["eligible_coupon_ids"],
            "proportional_copy_of_grand_would_be": 4,
        },
        "coalition_values": settle_d["coalition_values"],
        "settlement": {"status": settle_d["status"], "split": settle_d["split"], "range": settle_d["range"]},
        "independent_verification": verify_d,
    }
    check("membership.grand_saves_same_6", quote_d["total_discount"] == 6, 6, quote_d["total_discount"])
    check(
        "membership.coalition_quote_is_independent_not_proportional",
        ac_quote["savings"] == 3 and ac_quote["eligible_coupon_ids"] == ["A-SOLO"],
        3,
        ac_quote["savings"],
    )
    check("membership.signed_3_1_2", settle_d["split"] == {"A": 3, "B": 1, "C": 2}, {"A": 3, "B": 1, "C": 2}, settle_d["split"])
    check("membership.verified", verify_d["valid"], True, verify_d["problems"])

    # ------------------------------------------------------------------
    # 5. refund: a signed settlement survives; the refunded unit cannot
    #    support the next deal's settlement.
    # ------------------------------------------------------------------
    coupons_r = [
        {"coupon_id": "ABC6", "D": 6, "requires": {"A": 1, "B": 1, "C": 1}},
        {"coupon_id": "BC5", "D": 5, "requires": {"B": 1, "C": 1}},
    ]
    consent_r = {"ABC6": {"must": ["A", "B", "C"]}, "BC5": {"must": ["B", "C"]}}
    snap_r = svc.publish_snapshot(PRODUCTS3, coupons_r, snapshot_id="refund-flow")
    quote_r1 = svc.quote(BASKET3, [], "q-r1", snap_r)
    basket_r2 = [dict(u, unit_id=u["unit_id"] + "2") for u in BASKET3]
    quote_r2 = svc.quote(basket_r2, [], "q-r2", snap_r)
    svc.register_sale("S-r1", quote=quote_r1)
    svc.register_sale("S-r2", quote=quote_r2)
    svc.create_consortium("cons-r", snap_r, ["A", "B", "C"], consent_r, ZERO_MINIMUMS)
    svc.create_deal("deal-r1", "cons-r", ["S-r1"], {"S-r1:a": "A", "S-r1:b": "B", "S-r1:c": "C"})
    settle_r1 = svc.attempt_settlement("deal-r1", "settle-r1")
    svc.return_units("ret-a2", "S-r2", ["a2"], {"a2": "SCRAP"})
    svc.create_deal("deal-r2", "cons-r", ["S-r2"], {"S-r2:a2": "A", "S-r2:b2": "B", "S-r2:c2": "C"})
    deal_r2 = svc.get_deal("deal-r2")
    settle_r2 = svc.attempt_settlement("deal-r2", "settle-r2")
    verify_r2 = svc.verify_settlement("settle-r2")
    settle_r1_after = svc.get_settlement("settle-r1")
    verify_r1_after = svc.verify_settlement("settle-r1")
    results["scenarios"]["refund"] = {
        "first_settlement": {"status": settle_r1["status"], "split": settle_r1["split"], "range": settle_r1["range"]},
        "refunded_unit": "S-r2:a2",
        "second_deal": {
            "active_pool": [e["uid"] for e in deal_r2["active_pool"]],
            "excluded_units": [e["uid"] for e in deal_r2["excluded_units"]],
            "current_total": deal_r2["current_total"],
        },
        "second_settlement": {
            "status": settle_r2["status"],
            "total": settle_r2["total"],
            "certificate_summary": _certificate_summary(settle_r2["solver"]["certificate"]),
        },
        "first_settlement_still_queryable": {"status": settle_r1_after["status"], "split": settle_r1_after["split"]},
        "independent_verification": {"second": verify_r2, "first_after_refund": verify_r1_after},
    }
    check("refund.first_signed_1_2_3", settle_r1["split"] == {"A": 1, "B": 2, "C": 3}, {"A": 1, "B": 2, "C": 3}, settle_r1["split"])
    check(
        "refund.refunded_unit_excluded",
        deal_r2["excluded_units"] and deal_r2["excluded_units"][0]["uid"] == "S-r2:a2" and deal_r2["current_total"] == 4,
    )
    check("refund.second_rejected", settle_r2["status"] == "REJECTED", "REJECTED", settle_r2["status"])
    check("refund.second_certificate_verified", verify_r2["valid"], True, verify_r2["problems"])
    check("refund.first_record_intact", settle_r1_after["status"] == "SIGNED" and verify_r1_after["valid"], True, verify_r1_after["problems"])

    # ------------------------------------------------------------------
    # 6. twelve_counters: 4095 coalitions quoted, split signed, no exit.
    # ------------------------------------------------------------------
    counters12 = [f"C{i:02d}" for i in range(1, 13)]
    products12 = [{"sku": c, "price": 10} for c in counters12]
    coupons12: List[Dict[str, Any]] = []
    consent12: Dict[str, Any] = {}
    for k in range(6):
        a, b = counters12[2 * k], counters12[2 * k + 1]
        coupons12.append({"coupon_id": f"P{k + 1}", "D": 5, "requires": {a: 1, b: 1}})
        consent12[f"P{k + 1}"] = {"must": [a, b]}
    coupons12.append({"coupon_id": "G12", "D": 40, "requires": {c: 1 for c in counters12}})
    consent12["G12"] = {"must": counters12}
    snap_12 = svc.publish_snapshot(products12, coupons12, snapshot_id="twelve")
    basket12 = [{"unit_id": f"u{i:02d}", "sku": c, "price": 10} for i, c in enumerate(counters12, 1)]
    quote_12 = svc.quote(basket12, [], "q-12", snap_12)
    svc.register_sale("S-12", quote=quote_12)
    svc.create_consortium("cons-12", snap_12, counters12, consent12, {c: 0 for c in counters12})
    svc.create_deal("deal-12", "cons-12", ["S-12"], {f"S-12:u{i:02d}": c for i, c in enumerate(counters12, 1)})
    pair_q1 = svc.quote_coalition("deal-12", ["C01", "C02"])
    pair_q2 = svc.quote_coalition("deal-12", ["C02", "C01"])
    settle_12 = svc.attempt_settlement("deal-12", "settle-12")
    noexit_12 = svc.check_no_exit("settle-12")
    verify_12 = svc.verify_settlement("settle-12")
    expected_12 = {c: (3 if i < 8 else 4) for i, c in enumerate(counters12)}
    results["scenarios"]["twelve_counters"] = {
        "grand_quote": {"discount": quote_12["total_discount"], "coupon_ids": quote_12["coupon_ids"]},
        "pair_quote": {"savings": pair_q1["savings"], "quote_request_id": pair_q1["quote_request_id"], "replay_identical": pair_q1 == pair_q2},
        "settlement": {"status": settle_12["status"], "split": settle_12["split"], "range": settle_12["range"]},
        "no_exit": noexit_12,
        "independent_verification_summary": {
            "valid": verify_12["valid"],
            "problems": verify_12["problems"],
            "checks": len(verify_12["checks"]),
        },
    }
    check("twelve.grand_saves_40", quote_12["total_discount"] == 40, 40, quote_12["total_discount"])
    check("twelve.pair_quote_replays", pair_q1 == pair_q2 and pair_q1["savings"] == 5)
    check("twelve.signed_expected_split", settle_12["split"] == expected_12, expected_12, settle_12["split"])
    check("twelve.range_is_1", settle_12["range"] == 1, 1, settle_12["range"])
    check("twelve.no_exit_all_4095", noexit_12["stable"] and noexit_12["coalitions_checked"] == 4095, 4095, noexit_12["coalitions_checked"])
    check("twelve.verified", verify_12["valid"], True, verify_12["problems"])

    # ------------------------------------------------------------------
    # Reopen: everything survives a restart.
    # ------------------------------------------------------------------
    del svc
    reopened = open_service(db_path)
    check(
        "reopen.preserves_settlements",
        reopened.get_settlement("settle-b")["split"] == {"A": 2, "B": 2, "C": 2}
        and reopened.get_settlement("settle-a")["status"] == "REJECTED"
        and reopened.check_no_exit("settle-12")["stable"],
    )
    check("reopen.verify_still_valid", reopened.verify_settlement("settle-12")["valid"])

    results["summary"] = {
        "checks": len(results["checks"]),
        "passed": sum(1 for c in results["checks"] if c["ok"]),
        "failed": sum(1 for c in results["checks"] if not c["ok"]),
    }
    results["plan"] = {
        "note": "The grand_coupon scenario as a replayable JSON plan (python -m basket_service run-plan).",
        "steps": [
            {"op": "publish_snapshot", "products": PRODUCTS3, "coupons": PAIR_COUPONS + [GRAND_COUPON], "snapshot_id": "pairs-plus-grand"},
            {"name": "q", "op": "quote", "basket": BASKET3, "coupons": [], "request_id": "q-b", "snapshot_id": "pairs-plus-grand"},
            {"op": "register_sale", "sale_id": "S-b", "quote": "$q"},
            {
                "op": "create_consortium",
                "consortium_id": "cons-b",
                "snapshot_id": "pairs-plus-grand",
                "counters": ["A", "B", "C"],
                "consent": consent_b,
                "minimums": ZERO_MINIMUMS,
            },
            {"op": "create_deal", "deal_id": "deal-b", "consortium_id": "cons-b", "sale_ids": ["S-b"], "unit_counters": {"S-b:a": "A", "S-b:b": "B", "S-b:c": "C"}},
            {"op": "quote_coalition", "deal_id": "deal-b", "counters": ["A", "B"]},
            {"op": "attempt_settlement", "deal_id": "deal-b", "request_id": "settle-b"},
            {"op": "check_no_exit", "settlement_id": "settle-b"},
            {"op": "verify_settlement", "settlement_id": "settle-b"},
        ],
    }

    _write_json(Path(out_path), results)
    _write_json(Path(plan_path), results["plan"])
    return results
