"""Independent basket pricing and after-sales ledger service.

Quick start::

    from basket_service import open_service

    svc = open_service("data/shop.db")
    quote = svc.quote(basket, coupons, request_id="q1", snapshot_id=snap)
    sale = svc.register_sale("S1", quote=quote)
    result = svc.return_units("r1", "S1", ["b"], {"b": "RESTOCK"})
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .consortium import ConsortiumService
from .db import Database
from .errors import BasketError
from .ledger import AfterSalesLedger
from .quote_service import QuoteService


class Service:
    """Thin façade wiring the quote service and ledger onto one database."""

    def __init__(self, path: str = ":memory:", *, busy_timeout_ms: int = 30000):
        self.db = Database(path, busy_timeout_ms=busy_timeout_ms)
        self.quotes = QuoteService(self.db)
        self.ledger = AfterSalesLedger(self.db)
        self.consortiums = ConsortiumService(self.db, self.quotes, self.ledger)

    # quote surface -------------------------------------------------------
    def publish_snapshot(self, products=None, coupons=None, *, snapshot_id=None) -> str:
        return self.quotes.publish_snapshot(products, coupons, snapshot_id=snapshot_id)

    def get_snapshot(self, snapshot_id: str):
        return self.quotes.get_snapshot(snapshot_id)

    def quote(self, basket, coupons, request_id: Optional[str] = None, snapshot_id: Optional[str] = None):
        return self.quotes.quote(basket, coupons, request_id, snapshot_id)

    def quote_subset(self, basket, snapshot_id: str, coupon_ids, request_id: str):
        return self.quotes.quote_subset(basket, snapshot_id, coupon_ids, request_id)

    def get_quote(self, request_id: str):
        return self.quotes.get_quote(request_id)

    # ledger surface ------------------------------------------------------
    def register_sale(self, sale_id: str, **kwargs):
        return self.ledger.register_sale(sale_id, **kwargs)

    def get_sale(self, sale_id: str):
        return self.ledger.get_sale(sale_id)

    def return_units(self, request_id: str, sale_id: str, unit_ids, grade):
        return self.ledger.return_units(request_id, sale_id, unit_ids, grade)

    def get_return(self, request_id: str):
        return self.ledger.get_return(request_id)

    def adjust_inventory(self, event_id: str, sku: str, quantity: int, bucket: str):
        return self.ledger.adjust_inventory(event_id, sku, quantity, bucket)

    def inventory(self, skus=None):
        return self.ledger.inventory(skus)

    def audit_inventory(self):
        return self.ledger.audit_inventory()

    def audit_sales(self):
        return self.ledger.audit_sales()

    # consortium surface --------------------------------------------------
    def create_consortium(self, consortium_id, snapshot_id, counters, consent, minimums):
        return self.consortiums.create_consortium(consortium_id, snapshot_id, counters, consent, minimums)

    def get_consortium(self, consortium_id: str):
        return self.consortiums.get_consortium(consortium_id)

    def confirm_minimums(self, consortium_id: str, minimums):
        return self.consortiums.confirm_minimums(consortium_id, minimums)

    def create_deal(self, deal_id: str, consortium_id: str, sale_ids, unit_counters):
        return self.consortiums.create_deal(deal_id, consortium_id, sale_ids, unit_counters)

    def get_deal(self, deal_id: str):
        return self.consortiums.get_deal(deal_id)

    def quote_coalition(self, deal_id: str, counters, request_id: Optional[str] = None):
        return self.consortiums.quote_coalition(deal_id, counters, request_id)

    def attempt_settlement(self, deal_id: str, request_id: str):
        return self.consortiums.attempt_settlement(deal_id, request_id)

    def get_settlement(self, settlement_id: str):
        return self.consortiums.get_settlement(settlement_id)

    def check_no_exit(self, settlement_id: str):
        return self.consortiums.check_no_exit(settlement_id)

    def verify_settlement(self, settlement_id: str, *, deep: bool = True):
        return self.consortiums.verify_settlement(settlement_id, deep=deep)

    # cross-cutting -------------------------------------------------------
    def run(self, plan):
        """Execute a JSON plan; see ``basket_service.planner``."""

        from .planner import run_plan

        return run_plan(self, plan)


def open_service(path: str = ":memory:", *, busy_timeout_ms: int = 30000) -> Service:
    return Service(path, busy_timeout_ms=busy_timeout_ms)


__all__ = ["Service", "open_service", "BasketError", "Database"]
