"""Global search across products, delivery notes, Goods Out, sites, bins and references."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select

from wms.core.enums import EntityType
from wms.core.numbers import fmt_date, fmt_qty
from wms.db.models import Bin, DeliveryNote, GoodsIssue, Product, Site, StockMovement, StockReturn, Supplier
from wms.services.base import BaseService
from wms.services.products import text_filter


@dataclass
class SearchHit:
    kind: str  # Product, Delivery Note, Goods Out, Site, Bin, Supplier, Return, Movement
    entity_type: str
    entity_id: int
    title: str
    detail: str


class SearchService(BaseService):
    def search(self, text: str, per_kind: int = 50) -> list[SearchHit]:
        text = (text or "").strip()
        if len(text) < 2:
            return []
        like = f"%{text}%"
        hits: list[SearchHit] = []
        with self.db.read_session() as s:
            for p in s.scalars(select(Product).where(text_filter(text)).order_by(Product.internal_code)
                               .limit(per_kind)).unique():
                where = p.default_bin.full_code if p.default_bin else ""
                hits.append(SearchHit("Product", EntityType.PRODUCT.value, p.id, f"{p.internal_code} - {p.name}",
                                      " | ".join(x for x in (p.supplier_code, p.supplier.name if p.supplier else None,
                                                             where, "inactive" if not p.is_active else None) if x)))
            for dn in s.scalars(select(DeliveryNote).where(or_(
                    DeliveryNote.dn_number.ilike(like), DeliveryNote.po_reference.ilike(like),
                    DeliveryNote.supplier_id.in_(select(Supplier.id).where(Supplier.name.ilike(like)))))
                    .order_by(DeliveryNote.delivery_date.desc()).limit(per_kind)):
                hits.append(SearchHit("Delivery Note", EntityType.DELIVERY_NOTE.value, dn.id,
                                      f"{dn.dn_number} - {dn.supplier.name}",
                                      f"{fmt_date(dn.delivery_date)} | {dn.status.title()}"
                                      + (f" | PO {dn.po_reference}" if dn.po_reference else "")))
            for gi in s.scalars(select(GoodsIssue).where(or_(
                    GoodsIssue.issue_number.ilike(like), GoodsIssue.reference.ilike(like),
                    GoodsIssue.requested_by.ilike(like)))
                    .order_by(GoodsIssue.issue_date.desc()).limit(per_kind)):
                hits.append(SearchHit("Goods Out", EntityType.GOODS_ISSUE.value, gi.id,
                                      f"{gi.issue_number} - {gi.site.display}",
                                      f"{fmt_date(gi.issue_date)} | Ref {gi.reference or '-'} | {gi.status.title()}"))
            for site in s.scalars(select(Site).where(or_(
                    Site.project_number.ilike(like), Site.name.ilike(like), Site.customer.ilike(like),
                    Site.address.ilike(like), Site.postcode.ilike(like))).limit(per_kind)):
                hits.append(SearchHit("Site / Project", EntityType.SITE.value, site.id, site.display,
                                      " | ".join(x for x in (site.customer, site.status.title(), site.postcode) if x)))
            for b in s.scalars(select(Bin).where(Bin.full_code.ilike(like)).order_by(Bin.full_code).limit(per_kind)):
                hits.append(SearchHit("Bin", EntityType.BIN.value, b.id, b.full_code,
                                      " | ".join(x for x in (b.description, None if b.is_available else "Quarantine")
                                                 if x)))
            for sup in s.scalars(select(Supplier).where(or_(Supplier.name.ilike(like), Supplier.code.ilike(like)))
                                 .limit(per_kind)):
                hits.append(SearchHit("Supplier", EntityType.SUPPLIER.value, sup.id, f"{sup.code} - {sup.name}",
                                      sup.contact_name or ""))
            for r in s.scalars(select(StockReturn).where(or_(StockReturn.return_number.ilike(like),
                                                             StockReturn.reference.ilike(like))).limit(per_kind)):
                hits.append(SearchHit("Return", EntityType.RETURN.value, r.id, r.return_number,
                                      f"{fmt_date(r.return_date)} | {r.reference or ''}"))
            # Reference numbers that only exist on movements (job numbers, adjustment/transfer numbers)
            seen_docs = {(h.entity_type, h.entity_id) for h in hits}
            for m in s.scalars(select(StockMovement).where(or_(StockMovement.reference.ilike(like),
                                                               StockMovement.doc_number.ilike(like)))
                               .order_by(StockMovement.occurred_at.desc()).limit(per_kind)):
                if (m.doc_type, m.doc_id) in seen_docs:
                    continue
                hits.append(SearchHit("Movement", "MOVEMENT", m.id,
                                      f"{m.doc_number or m.reference} - {m.product.internal_code}",
                                      f"{m.movement_type.replace('_', ' ').title()} | {fmt_qty(m.quantity)} "
                                      f"{m.product.unit.code} | {fmt_date(m.occurred_at)}"))
        return hits
