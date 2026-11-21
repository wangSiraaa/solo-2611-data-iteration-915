"""Immutable snapshots and the optimal basket quote service.

The service exposes a small callable surface:

* ``publish_snapshot(products, coupons)`` -> snapshot id (idempotent by content,
  reject ``SNAPSHOT_CONFLICT`` when an existing id has different content);
* ``get_snapshot(snapshot_id)``;
* ``quote(basket, coupons, request_id, snapshot_id)`` -> complete quote object.

Quoting never writes sale or return state.  It stores the *request* record so
a same ``request_id`` + canonical basket + snapshot replays byte-for-byte
(the full allocation), while a different body raises ``REQUEST_CONFLICT``.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import optimizer
from .db import Database, canonical_json, deep_copy, json_loads, utc_now
from .errors import (
    INVALID_BASKET,
    INVALID_COUPON,
    INVALID_INPUT,
    QUOTE_CONFLICT,
    REQUEST_CONFLICT,
    SNAPSHOT_CONFLICT,
    UNKNOWN_SNAPSHOT,
    BasketError,
)
from .validation import (
    allocate_report,
    require_id,
    validate_assignments,
    validate_basket,
    validate_coupons,
    validate_quote_payload,
)


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _derive_products(basket: Sequence[Mapping[str, Any]], coupons: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Fallback catalogue when a customer-facing caller did not publish one."""

    prices: Dict[Tuple[str, int], int] = {}
    for unit in basket:
        prices[(unit["sku"], unit["price"])] = prices.get((unit["sku"], unit["price"]), 0) + 1
    return [{"sku": sku, "price": price, "quantity": qty} for (sku, price), qty in sorted(prices.items())]


def _canonical_snapshot(products: Any, coupons: Any) -> Dict[str, Any]:
    if products is None:
        products = []
    if not isinstance(products, Sequence) or isinstance(products, (str, bytes)):
        raise BasketError(INVALID_INPUT, "products must be a list")
    canonical_products: List[Dict[str, Any]] = []
    for index, item in enumerate(products):
        if not isinstance(item, Mapping):
            raise BasketError(INVALID_INPUT, "product entries must be objects", {"index": index})
        sku = require_id(item.get("sku"), INVALID_INPUT, f"products[{index}].sku")
        price = item.get("price")
        quantity = item.get("quantity", 1)
        if not isinstance(price, int) or isinstance(price, bool) or price < 1:
            raise BasketError(INVALID_INPUT, "product price must be a positive integer", {"sku": sku})
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
            raise BasketError(INVALID_INPUT, "product quantity must be a non-negative integer", {"sku": sku})
        canonical_products.append({"sku": sku, "price": price, "quantity": quantity})
    canonical_products.sort(key=lambda p: (p["sku"], p["price"]))
    if len({(p["sku"], p["price"]) for p in canonical_products}) != len(canonical_products):
        raise BasketError(INVALID_INPUT, "duplicate sku/price product rows")

    if coupons is None:
        coupons = []
    # A published catalogue may legitimately mention SKUs that the current
    # product list does not contain (e.g. a promo for another branch); the
    # basket-specific "missing sku" check is applied at quote time instead.
    canonical_coupons = validate_coupons(coupons, {p["sku"] for p in canonical_products}, allow_absent_skus=True)
    return {"products": canonical_products, "coupons": canonical_coupons}


class QuoteService:
    def __init__(self, db: Database):
        self.db = db
        # Snapshots are immutable per id (conflicting re-publishes are rejected
        # by the database), so caching them in memory is always safe.
        self._snapshot_cache: Dict[str, Dict[str, Any]] = {}

    # -- snapshots ---------------------------------------------------------
    def publish_snapshot(
        self,
        products: Optional[Sequence[Mapping[str, Any]]] = None,
        coupons: Optional[Sequence[Mapping[str, Any]]] = None,
        *,
        snapshot_id: Optional[str] = None,
    ) -> str:
        canonical = _canonical_snapshot(products, coupons)
        content = {
            "products": canonical["products"],
            "coupons": canonical["coupons"],
        }
        content_hash = _hash(content)
        chosen_id = require_id(snapshot_id, INVALID_INPUT, "snapshot_id") if snapshot_id else f"snap-{content_hash[:16]}"
        with self.db.transaction() as conn:
            row = conn.execute("SELECT content_json FROM snapshots WHERE snapshot_id = ?", (chosen_id,)).fetchone()
            if row is not None:
                if row["content_json"] != canonical_json(content):
                    raise BasketError(
                        SNAPSHOT_CONFLICT,
                        "snapshot id already published with different content",
                        {"snapshot_id": chosen_id},
                    )
                return chosen_id
            seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM snapshots").fetchone()
            conn.execute(
                "INSERT INTO snapshots (snapshot_id, seq, content_json, coupons_json, products_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chosen_id,
                    seq_row["next"],
                    canonical_json(content),
                    canonical_json(canonical["coupons"]),
                    canonical_json(canonical["products"]),
                    utc_now(),
                ),
            )
        return chosen_id

    def get_snapshot(self, snapshot_id: str) -> Dict[str, Any]:
        cached = self._snapshot_cache.get(snapshot_id)
        if cached is not None:
            return deep_copy(cached)
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_SNAPSHOT, "unknown snapshot", {"snapshot_id": snapshot_id})
        snapshot = {
            "snapshot_id": row["snapshot_id"],
            "seq": row["seq"],
            "products": json_loads(row["products_json"]),
            "coupons": json_loads(row["coupons_json"]),
            "created_at": row["created_at"],
        }
        self._snapshot_cache[snapshot_id] = deep_copy(snapshot)
        return snapshot

    # -- quoting -----------------------------------------------------------
    def quote(
        self,
        basket: Any,
        coupons: Any,
        request_id: Optional[str] = None,
        snapshot_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        units = validate_basket(basket)
        skus = {u["sku"] for u in units}
        if request_id is None:
            # Fire-and-forget callers may omit a request id; derive a stable one
            # from the canonical request so identical calls still replay.
            request_id = "auto-" + _hash({"basket": units, "snapshot": snapshot_id, "coupons": coupons})[:24]
        request_id = require_id(request_id, INVALID_INPUT, "request_id")

        if snapshot_id is None:
            canonical_coupons = validate_coupons(coupons, skus)
            snapshot_id = self.publish_snapshot(_derive_products(units, canonical_coupons), canonical_coupons)
        else:
            require_id(snapshot_id, INVALID_INPUT, "snapshot_id")
            snapshot = self.get_snapshot(snapshot_id)
            declared = {p["sku"] for p in snapshot["products"]}
            if declared:
                unknown = sorted(skus - declared)
                if unknown:
                    raise BasketError(
                        INVALID_BASKET,
                        "basket unit sku is not present in the bound snapshot's product catalogue",
                        {"snapshot_id": snapshot_id, "skus": unknown},
                    )
            # The published catalogue is immutable for this quote: any coupon
            # list supplied alongside a snapshot id must match it exactly, so a
            # promo version switch can never masquerade as the same snapshot.
            snapshot_coupons = validate_coupons(snapshot["coupons"], skus)
            if coupons is None or coupons == []:
                canonical_coupons = snapshot_coupons
            else:
                supplied = validate_coupons(coupons, skus)
                if canonical_json(supplied) != canonical_json(snapshot_coupons):
                    raise BasketError(
                        SNAPSHOT_CONFLICT,
                        "supplied coupons differ from the bound snapshot",
                        {"snapshot_id": snapshot_id},
                    )
                canonical_coupons = snapshot_coupons

        basket_sig = _hash(units)
        with self.db.connection() as conn:
            existing = conn.execute("SELECT * FROM quotes WHERE request_id = ?", (request_id,)).fetchone()
        if existing is not None:
            if existing["snapshot_id"] != snapshot_id or existing["basket_sig"] != basket_sig:
                raise BasketError(
                    REQUEST_CONFLICT,
                    "request_id reused with a different basket or snapshot",
                    {"request_id": request_id},
                )
            return json_loads(existing["quote_json"])

        # Compute a fresh optimal quote.  The optimizer's inputs are canonical
        # copies; nothing here mutates service state besides the quote record.
        used_masks, total = optimizer.solve(units, canonical_coupons)
        coupon_by_id = {c["coupon_id"]: c for c in canonical_coupons}
        mapping = {
            coupon_id: [units[i]["unit_id"] for i in range(len(units)) if mask >> i & 1]
            for coupon_id, mask in used_masks.items()
        }
        mapping = {cid: sorted(ids) for cid, ids in sorted(mapping.items())}
        validate_assignments(mapping, units, canonical_coupons)  # defensive
        allocation = allocate_report(units, used_masks, {c["coupon_id"]: c["D"] for c in canonical_coupons})
        if sum(entry["paid"] for entry in allocation) != total:  # pragma: no cover
            raise BasketError(QUOTE_CONFLICT, "internal total mismatch")

        quote = {
            "request_id": request_id,
            "snapshot_id": snapshot_id,
            "basket": units,
            "coupons": canonical_coupons,
            "assignments": mapping,
            "coupon_ids": sorted(mapping),
            "allocation": allocation,
            "total": total,
            "original_total": sum(u["price"] for u in units),
            "total_discount": sum(coupon_by_id[cid]["D"] for cid in mapping),
        }
        # Re-validate the full document as an external quote would be.
        quote = validate_quote_payload(quote)
        quote.update(
            {
                "coupon_ids": sorted(mapping),
                "original_total": sum(u["price"] for u in units),
                "total_discount": sum(coupon_by_id[cid]["D"] for cid in mapping),
            }
        )

        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM quotes WHERE request_id = ?", (request_id,)).fetchone()
            if existing is not None:
                if existing["snapshot_id"] != snapshot_id or existing["basket_sig"] != basket_sig:
                    raise BasketError(
                        REQUEST_CONFLICT,
                        "request_id reused with a different basket or snapshot",
                        {"request_id": request_id},
                    )
                return json_loads(existing["quote_json"])
            conn.execute(
                "INSERT INTO quotes (request_id, snapshot_id, basket_sig, basket_json, quote_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (request_id, snapshot_id, basket_sig, canonical_json(units), canonical_json(quote), utc_now()),
            )
        return deep_copy(quote)

    # -- subset quoting (consortium coalitions) ------------------------------
    def _prepare_subset(
        self, basket: Any, snapshot_id: str, coupon_ids: Sequence[str], request_id: str
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str, str, str]:
        """Validate a subset-quote request; return canonical parts + signature."""

        units = validate_basket(basket)
        skus = {u["sku"] for u in units}
        request_id = require_id(request_id, INVALID_INPUT, "request_id")
        require_id(snapshot_id, INVALID_INPUT, "snapshot_id")
        snapshot = self.get_snapshot(snapshot_id)
        declared = {p["sku"] for p in snapshot["products"]}
        if declared:
            unknown = sorted(skus - declared)
            if unknown:
                raise BasketError(
                    INVALID_BASKET,
                    "basket unit sku is not present in the bound snapshot's product catalogue",
                    {"snapshot_id": snapshot_id, "skus": unknown},
                )
        if coupon_ids is None or isinstance(coupon_ids, (str, bytes, Mapping)):
            raise BasketError(INVALID_INPUT, "coupon_ids must be a list")
        if not isinstance(coupon_ids, Sequence):
            raise BasketError(INVALID_INPUT, "coupon_ids must be a list")
        chosen: List[str] = []
        for index, coupon_id in enumerate(coupon_ids):
            chosen.append(require_id(coupon_id, INVALID_INPUT, f"coupon_ids[{index}]"))
        chosen = sorted(set(chosen))
        catalogue = {c["coupon_id"]: c for c in snapshot["coupons"]}
        unknown_coupons = [cid for cid in chosen if cid not in catalogue]
        if unknown_coupons:
            raise BasketError(
                INVALID_COUPON,
                "coupon_ids must be a subset of the bound snapshot's catalogue",
                {"snapshot_id": snapshot_id, "coupon_ids": unknown_coupons},
            )
        canonical_coupons = validate_coupons([catalogue[cid] for cid in chosen], skus)
        basket_sig = _hash({"basket": units, "coupon_ids": chosen})
        return units, canonical_coupons, snapshot_id, request_id, basket_sig

    def _replay_subset(self, conn, request_id: str, snapshot_id: str, basket_sig: str) -> Optional[Dict[str, Any]]:
        existing = conn.execute("SELECT * FROM quotes WHERE request_id = ?", (request_id,)).fetchone()
        if existing is None:
            return None
        if existing["snapshot_id"] != snapshot_id or existing["basket_sig"] != basket_sig:
            raise BasketError(
                REQUEST_CONFLICT,
                "request_id reused with a different basket, snapshot or coupon subset",
                {"request_id": request_id},
            )
        return json_loads(existing["quote_json"])

    def _compute_subset_quote(
        self, units: List[Dict[str, Any]], canonical_coupons: List[Dict[str, Any]], snapshot_id: str, request_id: str
    ) -> Dict[str, Any]:
        used_masks, total = optimizer.solve(units, canonical_coupons)
        coupon_by_id = {c["coupon_id"]: c for c in canonical_coupons}
        mapping = {
            coupon_id: [units[i]["unit_id"] for i in range(len(units)) if mask >> i & 1]
            for coupon_id, mask in used_masks.items()
        }
        mapping = {cid: sorted(ids) for cid, ids in sorted(mapping.items())}
        validate_assignments(mapping, units, canonical_coupons)  # defensive
        allocation = allocate_report(units, used_masks, {c["coupon_id"]: c["D"] for c in canonical_coupons})
        if sum(entry["paid"] for entry in allocation) != total:  # pragma: no cover
            raise BasketError(QUOTE_CONFLICT, "internal total mismatch")

        quote = {
            "request_id": request_id,
            "snapshot_id": snapshot_id,
            "basket": units,
            "coupons": canonical_coupons,
            "assignments": mapping,
            "coupon_ids": sorted(mapping),
            "allocation": allocation,
            "total": total,
            "original_total": sum(u["price"] for u in units),
            "total_discount": sum(coupon_by_id[cid]["D"] for cid in mapping),
        }
        quote = validate_quote_payload(quote)
        quote.update(
            {
                "coupon_ids": sorted(mapping),
                "original_total": sum(u["price"] for u in units),
                "total_discount": sum(coupon_by_id[cid]["D"] for cid in mapping),
            }
        )
        return quote

    def quote_subset(
        self,
        basket: Any,
        snapshot_id: str,
        coupon_ids: Sequence[str],
        request_id: str,
    ) -> Dict[str, Any]:
        """Quote a basket against a *subset* of a published snapshot's catalogue.

        The snapshot still pins the product catalogue and the master coupon
        list; ``coupon_ids`` must be a subset of the snapshot's coupons.  The
        subset is part of the request signature, so reusing a request id with a
        different subset is ``REQUEST_CONFLICT`` and an identical call replays
        the stored quote byte-for-byte.  Optimisation, per-unit allocation and
        tie-breaking are exactly those of a full quote.
        """

        units, canonical_coupons, snapshot_id, request_id, basket_sig = self._prepare_subset(
            basket, snapshot_id, coupon_ids, request_id
        )
        with self.db.connection() as conn:
            stored = self._replay_subset(conn, request_id, snapshot_id, basket_sig)
        if stored is not None:
            return stored
        quote = self._compute_subset_quote(units, canonical_coupons, snapshot_id, request_id)
        with self.db.transaction() as conn:
            stored = self._replay_subset(conn, request_id, snapshot_id, basket_sig)
            if stored is not None:
                return stored
            conn.execute(
                "INSERT INTO quotes (request_id, snapshot_id, basket_sig, basket_json, quote_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (request_id, snapshot_id, basket_sig, canonical_json(units), canonical_json(quote), utc_now()),
            )
        return deep_copy(quote)

    def quote_subset_batch(self, items: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Many subset quotes with a single write transaction.

        Each item is ``{"basket", "snapshot_id", "coupon_ids", "request_id"}``;
        the semantics per item are exactly those of :meth:`quote_subset`
        (replay on identical request, ``REQUEST_CONFLICT`` on a different
        body).  Used by the settlement layer, which must quote thousands of
        coalitions against the same frozen snapshot in one attempt.
        """

        prepared = [
            self._prepare_subset(item["basket"], item["snapshot_id"], item["coupon_ids"], item["request_id"])
            for item in items
        ]
        results: List[Optional[Dict[str, Any]]] = [None] * len(prepared)
        with self.db.connection() as conn:
            for index, (_units, _coupons, snapshot_id, request_id, basket_sig) in enumerate(prepared):
                results[index] = self._replay_subset(conn, request_id, snapshot_id, basket_sig)
        for index, (units, canonical_coupons, snapshot_id, request_id, _sig) in enumerate(prepared):
            if results[index] is None:
                results[index] = self._compute_subset_quote(units, canonical_coupons, snapshot_id, request_id)
        with self.db.transaction() as conn:
            for index, (units, _coupons, snapshot_id, request_id, basket_sig) in enumerate(prepared):
                stored = self._replay_subset(conn, request_id, snapshot_id, basket_sig)
                if stored is not None:
                    results[index] = stored
                    continue
                quote = results[index]
                conn.execute(
                    "INSERT INTO quotes (request_id, snapshot_id, basket_sig, basket_json, quote_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (request_id, snapshot_id, basket_sig, canonical_json(units), canonical_json(quote), utc_now()),
                )
        return [deep_copy(q) for q in results]

    # -- lookup ------------------------------------------------------------
    def get_quote(self, request_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT quote_json FROM quotes WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            raise BasketError("UNKNOWN_QUOTE", "unknown quote request", {"request_id": request_id})
        return json_loads(row["quote_json"])


__all__ = ["QuoteService", "publish_quote", "quote"]


def publish_quote(db: Database, products=None, coupons=None, *, snapshot_id=None) -> str:
    return QuoteService(db).publish_snapshot(products, coupons, snapshot_id=snapshot_id)
