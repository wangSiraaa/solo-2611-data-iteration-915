"""Consortium (joint-counter) promotion settlement on the basket service.

A *consortium* binds one frozen quote snapshot to a fixed set of counters
(联营柜台).  Every physical unit of a sale belongs to exactly one counter.
Each coupon of the snapshot carries issuer *consent terms*: counters that
must participate themselves (``must``) and groups of which any one member
suffices (``any_of``).  A coupon is eligible for a coalition quote only when
its consent terms hold for the coalition *and* the coalition's own active
units cover the coupon's required SKUs.

A *deal* bundles real ledger sales (their units and per-unit discounts are
stored facts).  At any time the deal's *active pool* is its units that have
not been refunded; a refunded unit never supports a later quote or
settlement, while every signed settlement keeps its frozen pool, coupon
sources and split queryable forever.

A *settlement attempt* quotes every non-empty coalition independently on the
same frozen snapshot (never a proportional copy of the grand total) and then
either signs an exact integer split of the deal's realised savings — every
coalition's share sum is at least its independently quoted savings and every
counter reaches its confirmed minimum — or rejects with a re-checkable
certificate built from those same independent quotes.  Ties are broken by
minimising the spread between the largest and smallest share, then by the
lexicographically smallest amount vector in stable counter-id order.

Amounts are synthetic points; nothing here touches real financial custody.
"""

from __future__ import annotations

import hashlib
from itertools import combinations
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import reference
from .db import Database, canonical_json, json_loads, utc_now
from .errors import (
    CONSORTIUM_CONFLICT,
    DEAL_CLOSED,
    DEAL_CONFLICT,
    INVALID_CONSENT,
    INVALID_CONSORTIUM,
    INVALID_INPUT,
    INVALID_MINIMUMS,
    REQUEST_CONFLICT,
    SALE_ALREADY_DEALT,
    SALE_SNAPSHOT_MISMATCH,
    SETTLEMENT_NOT_SIGNED,
    SETTLEMENT_POOL_CHANGED,
    UNKNOWN_CONSORTIUM,
    UNKNOWN_COUNTER,
    UNKNOWN_DEAL,
    UNKNOWN_SETTLEMENT,
    BasketError,
)
from .settlement import solve_split
from .validation import is_int, require_id

MAX_COUNTERS = 12


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_counters(counters: Any) -> List[str]:
    if counters is None or isinstance(counters, (str, bytes, Mapping)) or not isinstance(counters, Sequence):
        raise BasketError(INVALID_CONSORTIUM, "counters must be a list of counter ids")
    result: List[str] = []
    for index, counter in enumerate(counters):
        result.append(require_id(counter, INVALID_CONSORTIUM, f"counters[{index}]"))
    if len(set(result)) != len(result):
        raise BasketError(INVALID_CONSORTIUM, "duplicate counter id")
    if not 1 <= len(result) <= MAX_COUNTERS:
        raise BasketError(INVALID_CONSORTIUM, f"a consortium needs 1..{MAX_COUNTERS} counters", {"got": len(result)})
    return sorted(result)


def _canonical_minimums(minimums: Any, counters: Sequence[str]) -> Dict[str, int]:
    if not isinstance(minimums, Mapping):
        raise BasketError(INVALID_MINIMUMS, "minimums must be a counter -> non-negative integer mapping")
    if set(minimums) != set(counters):
        raise BasketError(
            INVALID_MINIMUMS,
            "minimums must cover exactly the consortium counters",
            {"expected": sorted(counters), "got": sorted(minimums)},
        )
    canonical: Dict[str, int] = {}
    for counter in counters:
        value = minimums[counter]
        if not is_int(value) or value < 0:
            raise BasketError(INVALID_MINIMUMS, "minimum must be a non-negative integer", {"counter": counter, "value": value})
        canonical[counter] = value
    return canonical


def _canonical_consent(consent: Any, coupon_ids: Sequence[str], counters: Sequence[str]) -> Dict[str, Any]:
    if not isinstance(consent, Mapping):
        raise BasketError(INVALID_CONSENT, "consent must be a coupon_id -> terms mapping")
    if set(consent) != set(coupon_ids):
        raise BasketError(
            INVALID_CONSENT,
            "consent terms must cover exactly the snapshot's coupons",
            {"expected": sorted(coupon_ids), "got": sorted(consent)},
        )
    counter_set = set(counters)
    canonical: Dict[str, Any] = {}
    for coupon_id in sorted(coupon_ids):
        terms = consent[coupon_id]
        if not isinstance(terms, Mapping):
            raise BasketError(INVALID_CONSENT, "consent terms must be objects", {"coupon_id": coupon_id})
        must_raw = terms.get("must", [])
        any_of_raw = terms.get("any_of", [])
        if not isinstance(must_raw, Sequence) or isinstance(must_raw, (str, bytes)):
            raise BasketError(INVALID_CONSENT, "must must be a list of counter ids", {"coupon_id": coupon_id})
        must = sorted({require_id(c, INVALID_CONSENT, f"consent[{coupon_id}].must") for c in must_raw})
        unknown = [c for c in must if c not in counter_set]
        if unknown:
            raise BasketError(INVALID_CONSENT, "must references unknown counters", {"coupon_id": coupon_id, "counters": unknown})
        if not isinstance(any_of_raw, Sequence) or isinstance(any_of_raw, (str, bytes)):
            raise BasketError(INVALID_CONSENT, "any_of must be a list of counter-id groups", {"coupon_id": coupon_id})
        groups: List[List[str]] = []
        for index, group in enumerate(any_of_raw):
            if not isinstance(group, Sequence) or isinstance(group, (str, bytes)) or not group:
                raise BasketError(INVALID_CONSENT, "any_of groups must be non-empty lists", {"coupon_id": coupon_id, "index": index})
            members = sorted({require_id(c, INVALID_CONSENT, f"consent[{coupon_id}].any_of[{index}]") for c in group})
            unknown = [c for c in members if c not in counter_set]
            if unknown:
                raise BasketError(
                    INVALID_CONSENT, "any_of references unknown counters", {"coupon_id": coupon_id, "counters": unknown}
                )
            groups.append(members)
        groups = sorted({tuple(g) for g in groups})
        canonical[coupon_id] = {"must": must, "any_of": [list(g) for g in groups]}
    return canonical


def _consent_holds(terms: Mapping[str, Any], coalition: set) -> bool:
    if not set(terms["must"]) <= coalition:
        return False
    return all(any(member in coalition for member in group) for group in terms["any_of"])


class _PoolChanged(Exception):
    """Internal retry signal: the active pool moved during an attempt."""


class ConsortiumService:
    def __init__(self, db: Database, quotes, ledger):
        self.db = db
        self.quotes = quotes
        self.ledger = ledger

    # ------------------------------------------------------------------ reads
    def _load_consortium(self, consortium_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM consortiums WHERE consortium_id = ?", (consortium_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_CONSORTIUM, "unknown consortium", {"consortium_id": consortium_id})
        snapshot = self.quotes.get_snapshot(row["snapshot_id"])
        return {
            "consortium_id": row["consortium_id"],
            "snapshot_id": row["snapshot_id"],
            "counters": json_loads(row["counters_json"]),
            "consent": json_loads(row["consent_json"]),
            "minimums": json_loads(row["minimums_json"]),
            "minimums_version": row["minimums_version"],
            "created_at": row["created_at"],
            "_catalogue": {c["coupon_id"]: c for c in snapshot["coupons"]},
        }

    def _load_deal(self, deal_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM deals WHERE deal_id = ?", (deal_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_DEAL, "unknown deal", {"deal_id": deal_id})
        return {
            "deal_id": row["deal_id"],
            "consortium_id": row["consortium_id"],
            "sales": json_loads(row["sales_json"]),
            "unit_counters": json_loads(row["unit_counters_json"]),
            "status": row["status"],
            "created_at": row["created_at"],
        }

    def _current_pool(self, deal: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int, str]:
        """Active (not refunded) units of the deal's sales, with their discounts."""

        pool: List[Dict[str, Any]] = []
        excluded: List[Dict[str, Any]] = []
        for sale_id in deal["sales"]:
            sale = self.ledger.get_sale(sale_id)
            for unit in sale["units"]:
                uid = f"{sale_id}:{unit['unit_id']}"
                entry = {
                    "uid": uid,
                    "sale_id": sale_id,
                    "unit_id": unit["unit_id"],
                    "sku": unit["sku"],
                    "price": unit["price"],
                    "discount": unit["discount"],
                    "counter": deal["unit_counters"][uid],
                }
                (excluded if unit["returned"] else pool).append(entry)
        pool.sort(key=lambda e: e["uid"])
        excluded.sort(key=lambda e: e["uid"])
        total = sum(entry["discount"] for entry in pool)
        pool_sig = _hash({"pool": pool, "excluded": excluded})
        return pool, excluded, total, pool_sig

    # ------------------------------------------------------------- consortium
    def create_consortium(
        self,
        consortium_id: str,
        snapshot_id: str,
        counters: Any,
        consent: Any,
        minimums: Any,
    ) -> Dict[str, Any]:
        consortium_id = require_id(consortium_id, INVALID_CONSORTIUM, "consortium_id")
        snapshot = self.quotes.get_snapshot(snapshot_id)  # UNKNOWN_SNAPSHOT propagates
        counter_order = _canonical_counters(counters)
        coupon_ids = sorted(c["coupon_id"] for c in snapshot["coupons"])
        canonical_consent = _canonical_consent(consent, coupon_ids, counter_order)
        canonical_minimums = _canonical_minimums(minimums, counter_order)
        content = {
            "snapshot_id": snapshot_id,
            "counters": counter_order,
            "consent": canonical_consent,
            "minimums": canonical_minimums,
        }
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM consortiums WHERE consortium_id = ?", (consortium_id,)).fetchone()
            if row is not None:
                stored = {
                    "snapshot_id": row["snapshot_id"],
                    "counters": json_loads(row["counters_json"]),
                    "consent": json_loads(row["consent_json"]),
                    "minimums": json_loads(row["minimums_json"]),
                }
                if stored["snapshot_id"] != content["snapshot_id"] or stored["counters"] != content["counters"] or stored["consent"] != content["consent"]:
                    raise BasketError(
                        CONSORTIUM_CONFLICT,
                        "consortium id already exists with different content",
                        {"consortium_id": consortium_id},
                    )
                if stored["minimums"] != content["minimums"]:
                    raise BasketError(
                        CONSORTIUM_CONFLICT,
                        "consortium id already exists with different minimums; use confirm_minimums",
                        {"consortium_id": consortium_id},
                    )
                return self.get_consortium(consortium_id)
            conn.execute(
                "INSERT INTO consortiums (consortium_id, snapshot_id, counters_json, consent_json, minimums_json, minimums_version, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
                (
                    consortium_id,
                    snapshot_id,
                    canonical_json(counter_order),
                    canonical_json(canonical_consent),
                    canonical_json(canonical_minimums),
                    utc_now(),
                ),
            )
            conn.execute(
                "INSERT INTO consortium_minimums (consortium_id, version, minimums_json, created_at) VALUES (?, 1, ?, ?)",
                (consortium_id, canonical_json(canonical_minimums), utc_now()),
            )
        return self.get_consortium(consortium_id)

    def get_consortium(self, consortium_id: str) -> Dict[str, Any]:
        loaded = self._load_consortium(consortium_id)
        loaded.pop("_catalogue")
        return loaded

    def confirm_minimums(self, consortium_id: str, minimums: Any) -> Dict[str, Any]:
        loaded = self._load_consortium(consortium_id)
        canonical = _canonical_minimums(minimums, loaded["counters"])
        if canonical == loaded["minimums"]:
            return {"consortium_id": consortium_id, "version": loaded["minimums_version"], "minimums": canonical, "updated": False}
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT minimums_version FROM consortiums WHERE consortium_id = ?", (consortium_id,)
            ).fetchone()
            version = row["minimums_version"] + 1
            conn.execute(
                "INSERT INTO consortium_minimums (consortium_id, version, minimums_json, created_at) VALUES (?, ?, ?, ?)",
                (consortium_id, version, canonical_json(canonical), utc_now()),
            )
            conn.execute(
                "UPDATE consortiums SET minimums_json = ?, minimums_version = ? WHERE consortium_id = ?",
                (canonical_json(canonical), version, consortium_id),
            )
        return {"consortium_id": consortium_id, "version": version, "minimums": canonical, "updated": True}

    # ------------------------------------------------------------------- deal
    def create_deal(self, deal_id: str, consortium_id: str, sale_ids: Any, unit_counters: Any) -> Dict[str, Any]:
        deal_id = require_id(deal_id, INVALID_INPUT, "deal_id")
        consortium = self._load_consortium(consortium_id)
        if sale_ids is None or isinstance(sale_ids, (str, bytes, Mapping)) or not isinstance(sale_ids, Sequence):
            raise BasketError(INVALID_INPUT, "sale_ids must be a list")
        sales = sorted({require_id(s, INVALID_INPUT, "sale_ids[]") for s in sale_ids})
        if not sales:
            raise BasketError(INVALID_INPUT, "a deal needs at least one sale")
        if not isinstance(unit_counters, Mapping):
            raise BasketError(INVALID_INPUT, "unit_counters must be a 'sale_id:unit_id' -> counter mapping")

        sale_records = [self.ledger.get_sale(sale_id) for sale_id in sales]
        for sale in sale_records:
            if sale["snapshot_id"] != consortium["snapshot_id"]:
                raise BasketError(
                    SALE_SNAPSHOT_MISMATCH,
                    "sale is bound to a different snapshot than the consortium",
                    {"sale_id": sale["sale_id"], "sale_snapshot": sale["snapshot_id"], "consortium_snapshot": consortium["snapshot_id"]},
                )
        expected_uids = {f"{sale['sale_id']}:{unit['unit_id']}" for sale in sale_records for unit in sale["units"]}
        if set(unit_counters) != expected_uids:
            raise BasketError(
                INVALID_INPUT,
                "unit_counters must cover exactly the units of the deal's sales as 'sale_id:unit_id'",
                {"missing": sorted(expected_uids - set(unit_counters)), "extra": sorted(set(unit_counters) - expected_uids)},
            )
        canonical_unit_counters: Dict[str, str] = {}
        for uid in sorted(expected_uids):
            counter = require_id(unit_counters[uid], INVALID_INPUT, f"unit_counters[{uid}]")
            if counter not in consortium["counters"]:
                raise BasketError(UNKNOWN_COUNTER, "unit assigned to a counter outside the consortium", {"uid": uid, "counter": counter})
            canonical_unit_counters[uid] = counter

        content_sig = _hash({"consortium_id": consortium_id, "sales": sales, "unit_counters": canonical_unit_counters})
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM deals WHERE deal_id = ?", (deal_id,)).fetchone()
            if row is not None:
                if row["content_sig"] != content_sig:
                    raise BasketError(DEAL_CONFLICT, "deal id already exists with different content", {"deal_id": deal_id})
                return self.get_deal(deal_id)
            taken = conn.execute(
                f"SELECT sale_id, deal_id FROM deal_sales WHERE sale_id IN ({','.join('?' * len(sales))})", sales
            ).fetchall()
            if taken:
                raise BasketError(
                    SALE_ALREADY_DEALT,
                    "a sale already belongs to another deal",
                    {"sales": [dict(row) for row in taken]},
                )
            conn.execute(
                "INSERT INTO deals (deal_id, consortium_id, sales_json, unit_counters_json, content_sig, status, created_at) VALUES (?, ?, ?, ?, ?, 'OPEN', ?)",
                (deal_id, consortium_id, canonical_json(sales), canonical_json(canonical_unit_counters), content_sig, utc_now()),
            )
            for sale_id in sales:
                conn.execute("INSERT INTO deal_sales (sale_id, deal_id) VALUES (?, ?)", (sale_id, deal_id))
        return self.get_deal(deal_id)

    def get_deal(self, deal_id: str) -> Dict[str, Any]:
        deal = self._load_deal(deal_id)
        pool, excluded, total, pool_sig = self._current_pool(deal)
        return {
            **deal,
            "active_pool": pool,
            "excluded_units": excluded,
            "current_total": total,
            "pool_sig": pool_sig,
        }

    # -------------------------------------------------------- coalition quote
    def _canonical_coalition(self, counters: Any, counter_order: Sequence[str]) -> List[str]:
        if counters is None or isinstance(counters, (str, bytes, Mapping)) or not isinstance(counters, Sequence):
            raise BasketError(INVALID_INPUT, "counters must be a non-empty list")
        coalition = sorted({require_id(c, INVALID_INPUT, "counters[]") for c in counters})
        if not coalition:
            raise BasketError(INVALID_INPUT, "a coalition needs at least one counter")
        unknown = [c for c in coalition if c not in counter_order]
        if unknown:
            raise BasketError(UNKNOWN_COUNTER, "coalition references counters outside the consortium", {"counters": unknown})
        return coalition

    def _coalition_quote_spec(
        self,
        deal: Mapping[str, Any],
        consortium: Mapping[str, Any],
        coalition: Sequence[str],
        pool: Sequence[Mapping[str, Any]],
        pool_sig: str,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Pure part of a coalition quote: basket, eligible coupons, request id."""

        coalition_set = set(coalition)
        basket_units = [entry for entry in pool if entry["counter"] in coalition_set]
        basket = [{"unit_id": e["uid"], "sku": e["sku"], "price": e["price"]} for e in basket_units]
        basket_skus = {e["sku"] for e in basket_units}
        catalogue = consortium["_catalogue"]
        eligible: List[str] = []
        for coupon_id in sorted(catalogue):
            terms = consortium["consent"][coupon_id]
            if not _consent_holds(terms, coalition_set):
                continue
            # The actually selected counters must provide the needed physical
            # items: every required SKU has to be present in the coalition's
            # own active units.
            if not set(catalogue[coupon_id]["requires"]) <= basket_skus:
                continue
            eligible.append(coupon_id)
        key = "+".join(coalition)
        if request_id is None:
            request_id = f"cq:{deal['deal_id']}:{pool_sig[:12]}:{key}"
        return {
            "deal_id": deal["deal_id"],
            "coalition": list(coalition),
            "coalition_key": key,
            "pool_sig": pool_sig,
            "items": [e["uid"] for e in basket_units],
            "eligible_coupon_ids": eligible,
            "quote_request_id": request_id,
            "_spec": {
                "basket": basket,
                "snapshot_id": consortium["snapshot_id"],
                "coupon_ids": eligible,
                "request_id": request_id,
            },
        }

    def _coalition_quote(
        self,
        deal: Mapping[str, Any],
        consortium: Mapping[str, Any],
        coalition: Sequence[str],
        pool: Sequence[Mapping[str, Any]],
        pool_sig: str,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        spec = self._coalition_quote_spec(deal, consortium, coalition, pool, pool_sig, request_id)
        quote = self.quotes.quote_subset(**spec.pop("_spec"))
        spec["savings"] = quote["total_discount"]
        spec["quote"] = quote
        return spec

    def quote_coalition(self, deal_id: str, counters: Any, request_id: Optional[str] = None) -> Dict[str, Any]:
        """Quote one coalition on the deal's current active pool (repeatable)."""

        deal = self._load_deal(deal_id)
        consortium = self._load_consortium(deal["consortium_id"])
        coalition = self._canonical_coalition(counters, consortium["counters"])
        pool, _excluded, _total, pool_sig = self._current_pool(deal)
        return self._coalition_quote(deal, consortium, coalition, pool, pool_sig, request_id)

    # -------------------------------------------------------------- settlement
    def attempt_settlement(self, deal_id: str, request_id: str) -> Dict[str, Any]:
        """Try to sign the split of one deal; reject with a certificate instead.

        Idempotent by ``request_id``: an identical call replays the stored
        record.  A deal closes once a settlement is SIGNED.
        """

        request_id = require_id(request_id, INVALID_INPUT, "request_id")
        deal = self._load_deal(deal_id)
        with self.db.connection() as conn:
            existing = conn.execute("SELECT deal_id FROM settlements WHERE settlement_id = ?", (request_id,)).fetchone()
        if existing is not None:
            if existing["deal_id"] != deal_id:
                raise BasketError(
                    REQUEST_CONFLICT,
                    "settlement request_id already used for another deal",
                    {"request_id": request_id, "deal_id": existing["deal_id"]},
                )
            return self.get_settlement(request_id)
        if deal["status"] == "CLOSED":
            raise BasketError(DEAL_CLOSED, "deal already has a signed settlement", {"deal_id": deal_id})

        consortium = self._load_consortium(deal["consortium_id"])
        counter_order = consortium["counters"]
        for _attempt in range(3):
            pool, excluded, total, pool_sig = self._current_pool(deal)
            specs = []
            coalitions: List[Tuple[str, ...]] = []
            for r in range(1, len(counter_order) + 1):
                for combo in combinations(counter_order, r):
                    spec = self._coalition_quote_spec(deal, consortium, list(combo), pool, pool_sig)
                    specs.append(spec["_spec"])
                    coalitions.append(combo)
            quotes = self.quotes.quote_subset_batch(specs)
            values: Dict[frozenset, int] = {}
            coalition_meta: Dict[str, Dict[str, Any]] = {}
            for combo, spec, quote in zip(coalitions, specs, quotes):
                savings = quote["total_discount"]
                values[frozenset(combo)] = savings
                coalition_meta["+".join(combo)] = {
                    "savings": savings,
                    "quote_request_id": spec["request_id"],
                }
            result = solve_split(counter_order, consortium["minimums"], total, values)
            record = {
                "settlement_id": request_id,
                "deal_id": deal_id,
                "consortium_id": consortium["consortium_id"],
                "status": "SIGNED" if result["status"] == "optimal" else "REJECTED",
                "snapshot_id": consortium["snapshot_id"],
                "counters": counter_order,
                "minimums": consortium["minimums"],
                "minimums_version": consortium["minimums_version"],
                "pool": pool,
                "excluded_units": excluded,
                "pool_sig": pool_sig,
                "total": total,
                "split": result.get("split"),
                "range": result.get("range"),
                "solver": {
                    key: result[key]
                    for key in ("status", "reason", "certificate", "fractional_range_bound", "kept_coalitions")
                    if key in result
                },
                "created_at": utc_now(),
            }
            try:
                with self.db.transaction() as conn:
                    existing = conn.execute(
                        "SELECT deal_id FROM settlements WHERE settlement_id = ?", (request_id,)
                    ).fetchone()
                    if existing is not None:
                        if existing["deal_id"] != deal_id:
                            raise BasketError(
                                REQUEST_CONFLICT,
                                "settlement request_id already used for another deal",
                                {"request_id": request_id, "deal_id": existing["deal_id"]},
                            )
                        return self.get_settlement(request_id)
                    deal_row = conn.execute("SELECT status FROM deals WHERE deal_id = ?", (deal_id,)).fetchone()
                    if deal_row["status"] == "CLOSED":
                        raise BasketError(DEAL_CLOSED, "deal already has a signed settlement", {"deal_id": deal_id})
                    _pool2, _excluded2, _total2, sig2 = self._current_pool(deal)
                    if sig2 != pool_sig:
                        raise _PoolChanged()
                    conn.execute(
                        "INSERT INTO settlements (settlement_id, deal_id, consortium_id, status, minimums_version, pool_sig, record_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            request_id,
                            deal_id,
                            consortium["consortium_id"],
                            record["status"],
                            record["minimums_version"],
                            pool_sig,
                            canonical_json(record),
                            record["created_at"],
                        ),
                    )
                    for key, meta in coalition_meta.items():
                        conn.execute(
                            "INSERT INTO settlement_coalitions (settlement_id, coalition_key, savings, quote_request_id) VALUES (?, ?, ?, ?)",
                            (request_id, key, meta["savings"], meta["quote_request_id"]),
                        )
                    if record["status"] == "SIGNED":
                        conn.execute("UPDATE deals SET status = 'CLOSED' WHERE deal_id = ?", (deal_id,))
            except _PoolChanged:
                continue
            return self.get_settlement(request_id)
        raise BasketError(SETTLEMENT_POOL_CHANGED, "the active pool kept changing during the attempt; retry", {"deal_id": deal_id})

    def get_settlement(self, settlement_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT record_json FROM settlements WHERE settlement_id = ?", (settlement_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_SETTLEMENT, "unknown settlement", {"settlement_id": settlement_id})
            coalitions = conn.execute(
                "SELECT coalition_key, savings, quote_request_id FROM settlement_coalitions WHERE settlement_id = ? ORDER BY coalition_key",
                (settlement_id,),
            ).fetchall()
        record = json_loads(row["record_json"])
        record["coalition_values"] = {c["coalition_key"]: c["savings"] for c in coalitions}
        record["coalition_quotes"] = {c["coalition_key"]: c["quote_request_id"] for c in coalitions}
        return record

    # ------------------------------------------------------------------ audit
    def check_no_exit(self, settlement_id: str) -> Dict[str, Any]:
        """Verify that no coalition would profit from leaving the consortium."""

        record = self.get_settlement(settlement_id)
        if record["status"] != "SIGNED":
            raise BasketError(SETTLEMENT_NOT_SIGNED, "settlement was rejected; there is no split to check", {"settlement_id": settlement_id})
        split = record["split"]
        violations: List[Dict[str, Any]] = []
        for key, savings in record["coalition_values"].items():
            members = key.split("+")
            allocated = sum(split[member] for member in members)
            if allocated < savings:
                violations.append({"coalition": members, "required": savings, "allocated": allocated})
        for counter, minimum in record["minimums"].items():
            if split[counter] < minimum:
                violations.append({"counter": counter, "required_minimum": minimum, "allocated": split[counter]})
        return {
            "settlement_id": settlement_id,
            "stable": not violations,
            "violations": violations,
            "coalitions_checked": len(record["coalition_values"]),
            "split": split,
            "total": record["total"],
            "sum_matches": sum(split.values()) == record["total"],
        }

    def verify_settlement(self, settlement_id: str, *, deep: bool = True) -> Dict[str, Any]:
        """Re-derive a settlement from primary records and check it end to end.

        Coalition savings are recomputed from the settlement's frozen pool and
        the snapshot catalogue with the independent exhaustive reference; the
        certificate (for REJECTED) or the split (for SIGNED) is checked with
        the independent reference verifiers.
        """

        record = self.get_settlement(settlement_id)
        deal = self._load_deal(record["deal_id"])
        consortium = self._load_consortium(record["consortium_id"])
        problems: List[str] = []
        checks: List[Dict[str, Any]] = []

        def check(name: str, ok: bool, detail: Any = None) -> None:
            checks.append({"check": name, "ok": bool(ok), "detail": detail})
            if not ok:
                problems.append(name)

        # 1. The frozen pool must match the immutable per-unit sale facts and
        #    the deal's unit->counter mapping.
        unit_facts: Dict[str, Dict[str, Any]] = {}
        for sale_id in deal["sales"]:
            sale = self.ledger.get_sale(sale_id)
            for unit in sale["units"]:
                unit_facts[f"{sale_id}:{unit['unit_id']}"] = unit
        pool_uids = {entry["uid"] for entry in record["pool"]}
        excluded_uids = {entry["uid"] for entry in record["excluded_units"]}
        check("pool_and_excluded_cover_deal_units", pool_uids.isdisjoint(excluded_uids) and pool_uids | excluded_uids == set(unit_facts))
        check("pool_covers_exactly_the_deal_mapping", pool_uids | excluded_uids == set(deal["unit_counters"]))
        facts_ok = True
        for entry in record["pool"] + record["excluded_units"]:
            fact = unit_facts.get(entry["uid"])
            if fact is None:
                facts_ok = False
                continue
            if (
                entry["sku"] != fact["sku"]
                or entry["price"] != fact["price"]
                or entry["discount"] != fact["discount"]
                or entry["counter"] != deal["unit_counters"][entry["uid"]]
            ):
                facts_ok = False
        check("pool_matches_immutable_sale_facts", facts_ok)
        check("total_is_sum_of_active_discounts", record["total"] == sum(e["discount"] for e in record["pool"]))

        # 2. The recorded minimums must match the versioned audit trail.
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT minimums_json FROM consortium_minimums WHERE consortium_id = ? AND version = ?",
                (record["consortium_id"], record["minimums_version"]),
            ).fetchone()
        check("minimums_match_confirmed_version", row is not None and json_loads(row["minimums_json"]) == record["minimums"])

        # 3. Recompute every coalition's savings independently from the frozen
        #    pool, the consent terms and the snapshot catalogue.
        recomputed: Dict[frozenset, int] = {}
        if deep:
            catalogue = consortium["_catalogue"]
            request_ids = [record["coalition_quotes"][key] for key in record["coalition_values"]]
            with self.db.connection() as conn:
                if request_ids:
                    marks = ",".join("?" * len(request_ids))
                    rows = conn.execute(
                        f"SELECT request_id, quote_json FROM quotes WHERE request_id IN ({marks})", request_ids
                    ).fetchall()
                else:
                    rows = []
            stored_quotes = {row["request_id"]: json_loads(row["quote_json"]) for row in rows}
            for key, savings in record["coalition_values"].items():
                members = key.split("+")
                member_set = set(members)
                basket_units = [e for e in record["pool"] if e["counter"] in member_set]
                basket = [{"unit_id": e["uid"], "sku": e["sku"], "price": e["price"]} for e in basket_units]
                basket_skus = {e["sku"] for e in basket_units}
                eligible = [
                    cid
                    for cid in sorted(catalogue)
                    if _consent_holds(consortium["consent"][cid], member_set)
                    and set(catalogue[cid]["requires"]) <= basket_skus
                ]
                coupons = [catalogue[cid] for cid in eligible]
                recomputed[frozenset(members)] = reference.coalition_savings(basket, coupons)
                quote_request_id = record["coalition_quotes"][key]
                stored_quote = stored_quotes.get(quote_request_id)
                check(
                    f"coalition[{key}].quote_record_consistent",
                    stored_quote is not None
                    and stored_quote["total_discount"] == savings
                    and stored_quote["snapshot_id"] == record["snapshot_id"],
                    {"quote_request_id": quote_request_id},
                )
            mismatched = {
                "+".join(sorted(key)): (recomputed[key], record["coalition_values"]["+".join(sorted(key))])
                for key in recomputed
                if recomputed[key] != record["coalition_values"]["+".join(sorted(key))]
            }
            check("coalition_values_match_independent_reference", not mismatched, mismatched)

        # 4. Check the outcome with the independent verifiers.
        values_map = {frozenset(key.split("+")): value for key, value in record["coalition_values"].items()}
        if record["status"] == "SIGNED":
            split = record["split"]
            check("split_sums_to_total", sum(split.values()) == record["total"])
            check("split_covers_counters", sorted(split) == record["counters"])
            check(
                "minimums_respected",
                all(split[c] >= record["minimums"][c] for c in record["counters"]),
            )
            worst = None
            for key, savings in record["coalition_values"].items():
                allocated = sum(split[member] for member in key.split("+"))
                if worst is None or allocated - savings < worst[0]:
                    worst = (allocated - savings, key)
            check("all_coalition_constraints_hold", worst is None or worst[0] >= 0, {"tightest": worst})
            n = len(record["counters"])
            if n <= 6 and record["total"] <= 30:
                expected = reference.brute_force_split(record["counters"], record["minimums"], record["total"], values_map)
                check("split_matches_brute_force_optimum", expected == split, {"expected": expected, "actual": split})
            else:
                checks.append(
                    {
                        "check": "split_optimality_independently_rederived",
                        "ok": True,
                        "detail": "skipped: instance too large for exhaustive re-derivation; constraints and certificates verified",
                    }
                )
        else:
            certificate = record["solver"]["certificate"]
            if certificate["type"] == "farkas":
                verdict = reference.verify_farkas_certificate(certificate, values_map, record["minimums"], record["total"])
            else:
                verdict = reference.verify_enumeration_certificate(certificate)
            check("certificate_valid", verdict["valid"], verdict.get("problems"))

        # 5. Informational: units refunded after signing stay in this frozen
        #    record but are excluded from any future pool.
        returned_since = sorted(
            entry["uid"] for entry in record["pool"] if unit_facts[entry["uid"]]["returned"]
        )
        checks.append({"check": "units_refunded_after_signing", "ok": True, "detail": returned_since})

        return {
            "settlement_id": settlement_id,
            "status": record["status"],
            "valid": not problems,
            "problems": problems,
            "checks": checks,
        }
