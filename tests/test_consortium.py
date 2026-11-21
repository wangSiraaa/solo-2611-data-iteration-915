"""Tests for the consortium settlement layer (basket_service.consortium)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from basket_service import open_service
from basket_service.errors import BasketError

PRODUCTS = [{"sku": "A", "price": 10}, {"sku": "B", "price": 10}, {"sku": "C", "price": 10}]
BASKET = [
    {"unit_id": "a", "sku": "A", "price": 10},
    {"unit_id": "b", "sku": "B", "price": 10},
    {"unit_id": "c", "sku": "C", "price": 10},
]
PAIR_COUPONS = [
    {"coupon_id": "AB", "D": 4, "requires": {"A": 1, "B": 1}},
    {"coupon_id": "AC", "D": 4, "requires": {"A": 1, "C": 1}},
    {"coupon_id": "BC", "D": 4, "requires": {"B": 1, "C": 1}},
]
GRAND = {"coupon_id": "ABC", "D": 6, "requires": {"A": 1, "B": 1, "C": 1}}
PAIR_CONSENT = {
    "AB": {"must": ["A", "B"]},
    "AC": {"must": ["A", "C"]},
    "BC": {"must": ["B", "C"]},
}
FULL_CONSENT = {**PAIR_CONSENT, "ABC": {"must": ["A", "B", "C"]}}
ZERO_MINIMUMS = {"A": 0, "B": 0, "C": 0}
UNIT_COUNTERS = {"S:a": "A", "S:b": "B", "S:c": "C"}


def make_service():
    return open_service(":memory:")


def base_setup(svc, coupons=PAIR_COUPONS + [GRAND], consent=FULL_CONSENT, minimums=ZERO_MINIMUMS, sale_id="S"):
    snap = svc.publish_snapshot(PRODUCTS, coupons, snapshot_id="snap")
    quote = svc.quote(BASKET, [], "q", snap)
    svc.register_sale(sale_id, quote=quote)
    svc.create_consortium("cons", snap, ["A", "B", "C"], consent, minimums)
    svc.create_deal("deal", "cons", [sale_id], {f"{sale_id}:a": "A", f"{sale_id}:b": "B", f"{sale_id}:c": "C"})
    return snap


class ConsortiumValidationTests(unittest.TestCase):
    def test_create_and_get(self):
        svc = make_service()
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS, snapshot_id="snap")
        created = svc.create_consortium("cons", snap, ["C", "A", "B"], PAIR_CONSENT, ZERO_MINIMUMS)
        self.assertEqual(created["counters"], ["A", "B", "C"])  # stable order
        self.assertEqual(created["minimums_version"], 1)
        again = svc.create_consortium("cons", snap, ["A", "B", "C"], PAIR_CONSENT, ZERO_MINIMUMS)
        self.assertEqual(again, created)

    def test_consent_must_cover_snapshot_coupons(self):
        svc = make_service()
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS, snapshot_id="snap")
        with self.assertRaises(BasketError) as ctx:
            svc.create_consortium("cons", snap, ["A", "B", "C"], {"AB": {"must": ["A", "B"]}}, ZERO_MINIMUMS)
        self.assertEqual(ctx.exception.code, "INVALID_CONSENT")

    def test_consent_unknown_counter_rejected(self):
        svc = make_service()
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS, snapshot_id="snap")
        bad = {**PAIR_CONSENT, "AB": {"must": ["A", "ZZ"]}}
        with self.assertRaises(BasketError) as ctx:
            svc.create_consortium("cons", snap, ["A", "B", "C"], bad, ZERO_MINIMUMS)
        self.assertEqual(ctx.exception.code, "INVALID_CONSENT")

    def test_minimums_must_cover_counters(self):
        svc = make_service()
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS, snapshot_id="snap")
        with self.assertRaises(BasketError) as ctx:
            svc.create_consortium("cons", snap, ["A", "B", "C"], PAIR_CONSENT, {"A": 1})
        self.assertEqual(ctx.exception.code, "INVALID_MINIMUMS")

    def test_consortium_conflict(self):
        svc = make_service()
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS, snapshot_id="snap")
        svc.create_consortium("cons", snap, ["A", "B", "C"], PAIR_CONSENT, ZERO_MINIMUMS)
        with self.assertRaises(BasketError) as ctx:
            svc.create_consortium("cons", snap, ["A", "B", "C"], PAIR_CONSENT, {"A": 1, "B": 0, "C": 0})
        self.assertEqual(ctx.exception.code, "CONSORTIUM_CONFLICT")

    def test_counter_limit(self):
        svc = make_service()
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS, snapshot_id="snap")
        with self.assertRaises(BasketError) as ctx:
            svc.create_consortium("cons", snap, [f"C{i}" for i in range(13)], PAIR_CONSENT, ZERO_MINIMUMS)
        self.assertEqual(ctx.exception.code, "INVALID_CONSORTIUM")

    def test_confirm_minimums_versions(self):
        svc = make_service()
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS, snapshot_id="snap")
        svc.create_consortium("cons", snap, ["A", "B", "C"], PAIR_CONSENT, ZERO_MINIMUMS)
        noop = svc.confirm_minimums("cons", ZERO_MINIMUMS)
        self.assertFalse(noop["updated"])
        updated = svc.confirm_minimums("cons", {"A": 3, "B": 0, "C": 0})
        self.assertTrue(updated["updated"])
        self.assertEqual(updated["version"], 2)
        self.assertEqual(svc.get_consortium("cons")["minimums"], {"A": 3, "B": 0, "C": 0})


class DealTests(unittest.TestCase):
    def test_deal_validation_and_idempotency(self):
        svc = make_service()
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS, snapshot_id="snap")
        quote = svc.quote(BASKET, [], "q", snap)
        svc.register_sale("S", quote=quote)
        svc.create_consortium("cons", snap, ["A", "B", "C"], PAIR_CONSENT, ZERO_MINIMUMS)
        deal = svc.create_deal("deal", "cons", ["S"], UNIT_COUNTERS)
        self.assertEqual(deal["status"], "OPEN")
        self.assertEqual(svc.create_deal("deal", "cons", ["S"], UNIT_COUNTERS)["deal_id"], "deal")
        with self.assertRaises(BasketError) as ctx:
            svc.create_deal("deal", "cons", ["S"], {"S:a": "A", "S:b": "B", "S:c": "A"})
        self.assertEqual(ctx.exception.code, "DEAL_CONFLICT")

    def test_deal_unit_mapping_must_cover_sales(self):
        svc = make_service()
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS, snapshot_id="snap")
        quote = svc.quote(BASKET, [], "q", snap)
        svc.register_sale("S", quote=quote)
        svc.create_consortium("cons", snap, ["A", "B", "C"], PAIR_CONSENT, ZERO_MINIMUMS)
        with self.assertRaises(BasketError) as ctx:
            svc.create_deal("deal", "cons", ["S"], {"S:a": "A", "S:b": "B"})
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")
        with self.assertRaises(BasketError) as ctx:
            svc.create_deal("deal", "cons", ["S"], {"S:a": "A", "S:b": "B", "S:c": "ZZ"})
        self.assertEqual(ctx.exception.code, "UNKNOWN_COUNTER")

    def test_deal_snapshot_mismatch_and_sale_reuse(self):
        svc = make_service()
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS, snapshot_id="snap")
        other = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS + [GRAND], snapshot_id="other")
        quote = svc.quote(BASKET, [], "q", snap)
        svc.register_sale("S", quote=quote)
        svc.create_consortium("cons", other, ["A", "B", "C"], FULL_CONSENT, ZERO_MINIMUMS)
        with self.assertRaises(BasketError) as ctx:
            svc.create_deal("deal", "cons", ["S"], UNIT_COUNTERS)
        self.assertEqual(ctx.exception.code, "SALE_SNAPSHOT_MISMATCH")
        svc.create_consortium("cons2", snap, ["A", "B", "C"], PAIR_CONSENT, ZERO_MINIMUMS)
        svc.create_deal("deal", "cons2", ["S"], UNIT_COUNTERS)
        with self.assertRaises(BasketError) as ctx:
            svc.create_deal("deal2", "cons2", ["S"], UNIT_COUNTERS)
        self.assertEqual(ctx.exception.code, "SALE_ALREADY_DEALT")

    def test_unknown_sale_and_consortium(self):
        svc = make_service()
        with self.assertRaises(BasketError) as ctx:
            svc.create_deal("deal", "nope", ["S"], UNIT_COUNTERS)
        self.assertEqual(ctx.exception.code, "UNKNOWN_CONSORTIUM")
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS, snapshot_id="snap")
        svc.create_consortium("cons", snap, ["A", "B", "C"], PAIR_CONSENT, ZERO_MINIMUMS)
        with self.assertRaises(BasketError) as ctx:
            svc.create_deal("deal", "cons", ["S"], UNIT_COUNTERS)
        self.assertEqual(ctx.exception.code, "UNKNOWN_SALE")


class QuoteSubsetTests(unittest.TestCase):
    def test_subset_quote_and_replay(self):
        svc = make_service()
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS + [GRAND], snapshot_id="snap")
        q1 = svc.quote_subset(BASKET, snap, ["AB"], "sub-1")
        self.assertEqual(q1["total_discount"], 4)
        self.assertEqual(q1["coupon_ids"], ["AB"])
        self.assertEqual(svc.quote_subset(list(reversed(BASKET)), snap, ["AB"], "sub-1"), q1)
        with self.assertRaises(BasketError) as ctx:
            svc.quote_subset(BASKET, snap, ["AB", "BC"], "sub-1")
        self.assertEqual(ctx.exception.code, "REQUEST_CONFLICT")

    def test_subset_must_come_from_catalogue(self):
        svc = make_service()
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS, snapshot_id="snap")
        with self.assertRaises(BasketError) as ctx:
            svc.quote_subset(BASKET, snap, ["NOPE"], "sub-1")
        self.assertEqual(ctx.exception.code, "INVALID_COUPON")

    def test_subset_shares_request_namespace_with_full_quotes(self):
        svc = make_service()
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS, snapshot_id="snap")
        svc.quote(BASKET, [], "shared", snap)
        with self.assertRaises(BasketError) as ctx:
            svc.quote_subset(BASKET, snap, ["AB"], "shared")
        self.assertEqual(ctx.exception.code, "REQUEST_CONFLICT")

    def test_empty_subset_and_empty_basket(self):
        svc = make_service()
        snap = svc.publish_snapshot(PRODUCTS, PAIR_COUPONS, snapshot_id="snap")
        quote = svc.quote_subset([], snap, [], "empty")
        self.assertEqual(quote["total_discount"], 0)
        self.assertEqual(quote["total"], 0)


class CoalitionQuoteTests(unittest.TestCase):
    def test_consent_must_filters_coupons(self):
        svc = make_service()
        base_setup(svc)
        quote = svc.quote_coalition("deal", ["A", "B"])
        self.assertEqual(quote["eligible_coupon_ids"], ["AB"])
        self.assertEqual(quote["savings"], 4)
        solo = svc.quote_coalition("deal", ["A"])
        self.assertEqual(solo["eligible_coupon_ids"], [])
        self.assertEqual(solo["savings"], 0)

    def test_any_of_consent(self):
        svc = make_service()
        coupons = [{"coupon_id": "PROMO", "D": 5, "requires": {"A": 1}}]
        consent = {"PROMO": {"must": ["A"], "any_of": [["B", "C"]]}}
        snap = svc.publish_snapshot(PRODUCTS, coupons, snapshot_id="snap")
        quote = svc.quote(BASKET, [], "q", snap)
        svc.register_sale("S", quote=quote)
        svc.create_consortium("cons", snap, ["A", "B", "C"], consent, ZERO_MINIMUMS)
        svc.create_deal("deal", "cons", ["S"], UNIT_COUNTERS)
        self.assertEqual(svc.quote_coalition("deal", ["A"])["eligible_coupon_ids"], [])
        self.assertEqual(svc.quote_coalition("deal", ["A", "B"])["eligible_coupon_ids"], ["PROMO"])
        self.assertEqual(svc.quote_coalition("deal", ["A", "C"])["eligible_coupon_ids"], ["PROMO"])
        self.assertEqual(svc.quote_coalition("deal", ["B", "C"])["eligible_coupon_ids"], [])

    def test_missing_items_make_coupon_ineligible(self):
        # A refunded unit removes its SKU, so consent alone is not enough.
        svc = make_service()
        base_setup(svc)
        svc.return_units("ret-a", "S", ["a"], {"a": "SCRAP"})
        quote = svc.quote_coalition("deal", ["A", "B"])
        self.assertEqual(quote["eligible_coupon_ids"], [])  # AB needs sku A, now gone
        self.assertEqual(quote["items"], ["S:b"])

    def test_repeated_queries_replay(self):
        svc = make_service()
        base_setup(svc)
        first = svc.quote_coalition("deal", ["A", "B"])
        second = svc.quote_coalition("deal", ["B", "A"])
        self.assertEqual(first, second)
        self.assertEqual(first["quote_request_id"], second["quote_request_id"])

    def test_unknown_counter_and_deal(self):
        svc = make_service()
        base_setup(svc)
        with self.assertRaises(BasketError) as ctx:
            svc.quote_coalition("deal", ["A", "ZZ"])
        self.assertEqual(ctx.exception.code, "UNKNOWN_COUNTER")
        with self.assertRaises(BasketError) as ctx:
            svc.quote_coalition("nope", ["A"])
        self.assertEqual(ctx.exception.code, "UNKNOWN_DEAL")


class SettlementTests(unittest.TestCase):
    def test_sign_and_replay_and_close(self):
        svc = make_service()
        base_setup(svc)
        settlement = svc.attempt_settlement("deal", "settle")
        self.assertEqual(settlement["status"], "SIGNED")
        self.assertEqual(settlement["split"], {"A": 2, "B": 2, "C": 2})
        self.assertEqual(svc.attempt_settlement("deal", "settle"), settlement)
        with self.assertRaises(BasketError) as ctx:
            svc.attempt_settlement("deal", "settle-2")
        self.assertEqual(ctx.exception.code, "DEAL_CLOSED")
        self.assertEqual(svc.get_deal("deal")["status"], "CLOSED")

    def test_rejected_settlement_is_stored_and_verifiable(self):
        svc = make_service()
        base_setup(svc, coupons=PAIR_COUPONS, consent=PAIR_CONSENT)
        settlement = svc.attempt_settlement("deal", "settle")
        self.assertEqual(settlement["status"], "REJECTED")
        self.assertEqual(settlement["solver"]["certificate"]["type"], "farkas")
        self.assertTrue(svc.verify_settlement("settle")["valid"])
        # A rejected deal stays open: a later attempt (same pool) replays per request id.
        again = svc.attempt_settlement("deal", "settle-new")
        self.assertEqual(again["status"], "REJECTED")
        self.assertNotEqual(again["settlement_id"], settlement["settlement_id"])

    def test_settlement_request_id_scoped_per_deal(self):
        svc = make_service()
        base_setup(svc)
        svc.attempt_settlement("deal", "settle")
        with self.assertRaises(BasketError) as ctx:
            svc.get_settlement("nope")
        self.assertEqual(ctx.exception.code, "UNKNOWN_SETTLEMENT")

    def test_check_no_exit(self):
        svc = make_service()
        base_setup(svc)
        svc.attempt_settlement("deal", "settle")
        report = svc.check_no_exit("settle")
        self.assertTrue(report["stable"])
        self.assertEqual(report["coalitions_checked"], 7)
        self.assertTrue(report["sum_matches"])

    def test_check_no_exit_requires_signed(self):
        svc = make_service()
        base_setup(svc, coupons=PAIR_COUPONS, consent=PAIR_CONSENT)
        svc.attempt_settlement("deal", "settle")
        with self.assertRaises(BasketError) as ctx:
            svc.check_no_exit("settle")
        self.assertEqual(ctx.exception.code, "SETTLEMENT_NOT_SIGNED")

    def test_verify_deep_and_shallow(self):
        svc = make_service()
        base_setup(svc)
        svc.attempt_settlement("deal", "settle")
        self.assertTrue(svc.verify_settlement("settle", deep=True)["valid"])
        self.assertTrue(svc.verify_settlement("settle", deep=False)["valid"])

    def test_minimums_update_changes_outcome(self):
        svc = make_service()
        base_setup(svc)
        svc.confirm_minimums("cons", {"A": 3, "B": 0, "C": 0})
        settlement = svc.attempt_settlement("deal", "settle")
        self.assertEqual(settlement["status"], "REJECTED")
        self.assertEqual(settlement["minimums_version"], 2)
        cert = settlement["solver"]["certificate"]
        kinds = sorted(c["kind"] for c in cert["constraints"])
        self.assertEqual(kinds, ["coalition", "minimum", "total"])


class RefundFlowTests(unittest.TestCase):
    def test_refund_blocks_next_settlement_and_history_survives(self):
        svc = make_service()
        coupons = [
            {"coupon_id": "ABC6", "D": 6, "requires": {"A": 1, "B": 1, "C": 1}},
            {"coupon_id": "BC5", "D": 5, "requires": {"B": 1, "C": 1}},
        ]
        consent = {"ABC6": {"must": ["A", "B", "C"]}, "BC5": {"must": ["B", "C"]}}
        snap = svc.publish_snapshot(PRODUCTS, coupons, snapshot_id="snap")
        q1 = svc.quote(BASKET, [], "q1", snap)
        basket2 = [dict(u, unit_id=u["unit_id"] + "2") for u in BASKET]
        q2 = svc.quote(basket2, [], "q2", snap)
        svc.register_sale("S1", quote=q1)
        svc.register_sale("S2", quote=q2)
        svc.create_consortium("cons", snap, ["A", "B", "C"], consent, ZERO_MINIMUMS)
        svc.create_deal("deal1", "cons", ["S1"], {"S1:a": "A", "S1:b": "B", "S1:c": "C"})
        first = svc.attempt_settlement("deal1", "settle-1")
        self.assertEqual(first["split"], {"A": 1, "B": 2, "C": 3})

        svc.return_units("ret", "S2", ["a2"], {"a2": "SCRAP"})
        svc.create_deal("deal2", "cons", ["S2"], {"S2:a2": "A", "S2:b2": "B", "S2:c2": "C"})
        deal2 = svc.get_deal("deal2")
        self.assertEqual([e["uid"] for e in deal2["active_pool"]], ["S2:b2", "S2:c2"])
        self.assertEqual(deal2["current_total"], 4)
        second = svc.attempt_settlement("deal2", "settle-2")
        self.assertEqual(second["status"], "REJECTED")
        self.assertTrue(svc.verify_settlement("settle-2")["valid"])

        # The signed settlement keeps its frozen pool and split.
        frozen = svc.get_settlement("settle-1")
        self.assertEqual(frozen["status"], "SIGNED")
        self.assertEqual(len(frozen["pool"]), 3)
        self.assertTrue(svc.verify_settlement("settle-1")["valid"])

    def test_fully_refunded_deal_settles_zero(self):
        svc = make_service()
        base_setup(svc)
        svc.return_units("ret-all", "S", ["a", "b", "c"], {"a": "SCRAP", "b": "SCRAP", "c": "SCRAP"})
        settlement = svc.attempt_settlement("deal", "settle")
        self.assertEqual(settlement["status"], "SIGNED")
        self.assertEqual(settlement["total"], 0)
        self.assertEqual(settlement["split"], {"A": 0, "B": 0, "C": 0})

    def test_refund_after_signing_keeps_frozen_record_valid(self):
        svc = make_service()
        base_setup(svc)
        svc.attempt_settlement("deal", "settle")
        # Refunding a unit of the signed deal's sale is an after-sales fact;
        # the signed settlement keeps its frozen pool and stays verifiable.
        svc.return_units("ret-late", "S", ["a"], {"a": "RESTOCK"})
        frozen = svc.get_settlement("settle")
        self.assertEqual(frozen["status"], "SIGNED")
        self.assertEqual(len(frozen["pool"]), 3)
        verdict = svc.verify_settlement("settle")
        self.assertTrue(verdict["valid"], verdict["problems"])
        reported = [c for c in verdict["checks"] if c["check"] == "units_refunded_after_signing"][0]
        self.assertEqual(reported["detail"], ["S:a"])
        # But the same unit can never support a new quote on the deal's pool.
        self.assertEqual(svc.get_deal("deal")["current_total"], 4)
        coalition = svc.quote_coalition("deal", ["A", "B"])
        self.assertEqual(coalition["items"], ["S:b"])


class TwelveCounterTests(unittest.TestCase):
    def test_twelve_counters_full_core_check(self):
        svc = make_service()
        counters = [f"C{i:02d}" for i in range(1, 13)]
        products = [{"sku": c, "price": 10} for c in counters]
        coupons = []
        consent = {}
        for k in range(6):
            a, b = counters[2 * k], counters[2 * k + 1]
            coupons.append({"coupon_id": f"P{k + 1}", "D": 5, "requires": {a: 1, b: 1}})
            consent[f"P{k + 1}"] = {"must": [a, b]}
        coupons.append({"coupon_id": "G12", "D": 40, "requires": {c: 1 for c in counters}})
        consent["G12"] = {"must": counters}
        snap = svc.publish_snapshot(products, coupons, snapshot_id="snap12")
        basket = [{"unit_id": f"u{i:02d}", "sku": c, "price": 10} for i, c in enumerate(counters, 1)]
        quote = svc.quote(basket, [], "q12", snap)
        self.assertEqual(quote["total_discount"], 40)
        svc.register_sale("S12", quote=quote)
        svc.create_consortium("cons12", snap, counters, consent, {c: 0 for c in counters})
        svc.create_deal("deal12", "cons12", ["S12"], {f"S12:u{i:02d}": c for i, c in enumerate(counters, 1)})
        pair = svc.quote_coalition("deal12", ["C01", "C02"])
        self.assertEqual(pair["savings"], 5)
        self.assertEqual(svc.quote_coalition("deal12", ["C02", "C01"]), pair)
        settlement = svc.attempt_settlement("deal12", "settle12")
        self.assertEqual(settlement["status"], "SIGNED")
        self.assertEqual(settlement["split"], {c: (3 if i < 8 else 4) for i, c in enumerate(counters)})
        report = svc.check_no_exit("settle12")
        self.assertTrue(report["stable"])
        self.assertEqual(report["coalitions_checked"], 4095)
        self.assertTrue(svc.verify_settlement("settle12")["valid"])


class PersistenceTests(unittest.TestCase):
    def test_reopen_preserves_consortium_deal_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "cons.db")
            svc = open_service(path)
            base_setup(svc)
            signed = svc.attempt_settlement("deal", "settle")
            del svc
            reopened = open_service(path)
            self.assertEqual(reopened.get_settlement("settle")["split"], signed["split"])
            self.assertEqual(reopened.get_consortium("cons")["counters"], ["A", "B", "C"])
            self.assertEqual(reopened.get_deal("deal")["status"], "CLOSED")
            self.assertTrue(reopened.check_no_exit("settle")["stable"])
            self.assertTrue(reopened.verify_settlement("settle")["valid"])
            # Coalition quotes replay from the same database.
            quote = reopened.quote_coalition("deal", ["A", "B"])
            self.assertEqual(quote["savings"], 4)


if __name__ == "__main__":
    unittest.main()
