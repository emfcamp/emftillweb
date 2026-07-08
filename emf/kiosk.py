"""Kiosk order API: create, retrieve, cancel and expire unpaid orders.

Backs the /api/kiosk/orders endpoints. An order is a quicktill Transaction
tagged with kiosk metadata; its transaction id is the order ref, and an HMAC
barcode lets a kiosk prove ownership when cancelling. Callers authenticate
with a single shared bearer token.
"""

import datetime
import hashlib
import hmac
import json
from decimal import Decimal
from collections import Counter

import sqlalchemy
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from quicktill.models import (
    LogEntry,
    RefusalsLog,
    Session,
    StockLine,
    StockOut,
    StockType,
    Transaction,
    TransactionMeta,
    Transline,
    User,
    max_quantity,
    zero,
)
from sqlalchemy.orm import joinedload, selectinload, contains_eager

from .tilldb import tillsession


order_meta_key = "emf:kiosk-order"
pending_meta_key = "emf:kiosk-pending"
default_timeout = datetime.timedelta(minutes=15)


# Barcode format: 10 decimal digits — PPPPPCCCCC
#   PPPPP = permuted transaction ID (LCG: avoids leading zeros for small IDs)
#   CCCCC = last 5 decimal digits of HMAC-SHA1(secret, trans_id)
#
# Constants must match quicktill-kiosk-plugin's decode side (see recall.py).
_PERM_A = 73141
_PERM_B = 49873
_PERM_M = 100000


def _permute(trans_id):
    return (_PERM_A * trans_id + _PERM_B) % _PERM_M


def _checkdigits(trans_id):
    secret = settings.EMF_KIOSK_BARCODE_SECRET.encode()
    msg = str(trans_id).encode()
    h = hmac.new(secret, msg, hashlib.sha1)
    return str(int(h.hexdigest(), 16) % _PERM_M).zfill(5)


def _order_barcode(trans_id):
    return f"{_permute(trans_id):05d}{_checkdigits(trans_id)}"


class KioskOrderError(Exception):
    status_code = 400
    code = "kiosk-order-error"

    def __init__(self, message, *, stockline_id=None):
        super().__init__(message)
        self.message = message
        self.stockline_id = stockline_id

    def as_dict(self):
        d = {
            "error": self.code,
            "message": self.message,
        }
        if self.stockline_id is not None:
            d["stockline_id"] = self.stockline_id
        return d


class ConfigurationError(KioskOrderError):
    status_code = 503
    code = "kiosk-api-not-configured"


class AuthorizationError(KioskOrderError):
    status_code = 401
    code = "invalid-token"


class RequestMethodError(KioskOrderError):
    status_code = 405
    code = "method-not-allowed"


class RequestBodyError(KioskOrderError):
    status_code = 400
    code = "invalid-json"


class NoActiveSession(KioskOrderError):
    status_code = 409
    code = "no-active-session"


class OrderNotFound(KioskOrderError):
    status_code = 404
    code = "not-found"


class OrderStateError(KioskOrderError):
    status_code = 409
    code = "order-state-not-suitable"


class OrderError(KioskOrderError):
    status_code = 400
    code = "order-error"


class PriceNotSet(KioskOrderError):
    status_code = 409
    code = "price-not-set"


class InsufficientStock(KioskOrderError):
    status_code = 409
    code = "insufficient-stock"

    def __init__(self, message, *, stockline_id=None, requested=None,
                 available=None):
        super().__init__(message, stockline_id=stockline_id)
        self.requested = requested
        self.available = available

    def as_dict(self):
        d = super().as_dict()
        if self.requested is not None:
            d["requested"] = str(self.requested)
        if self.available is not None:
            d["available"] = str(self.available)
        return d


def _money(value):
    return str(value.quantize(Decimal("0.01")))


def _timestamp(dt):
    return dt.replace(microsecond=0).isoformat()


def _parse_timestamp(value):
    try:
        return datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _read_meta(trans):
    """Return a transaction's decoded kiosk-order metadata, or None.

    None means this is not a kiosk order (the transaction has no kiosk meta
    key) or its stored metadata isn't valid JSON. Callers treat None as
    "not a kiosk order" — e.g. a 404 on the order endpoints.
    """
    entry = trans.meta.get(order_meta_key)
    if not entry:
        return None
    try:
        return json.loads(entry.value)
    except json.JSONDecodeError:
        return None


def _set_meta(trans, value):
    trans.set_meta(order_meta_key, json.dumps(value, sort_keys=True))


def _unset_pending(trans):
    if pending_meta_key in trans.meta:
        del trans.meta[pending_meta_key]


def _order_to_dict(trans, meta):
    lines = []
    total = zero
    for tl in trans.lines:
        if tl.amount == zero or tl.items < 0:
            continue
        line_total = tl.amount * tl.items
        total += line_total
        lines.append({
            "description": tl.text,
            "quantity": tl.items,
            "unit_price": _money(tl.amount),
            "line_total": _money(line_total),
        })
    return {
        "barcode": _order_barcode(trans.id),
        "transaction_id": trans.id,
        "created_at": meta["created_at"],
        "expires_at": meta["expires_at"],
        "soft_only": meta.get("soft_only", False),
        "total": _money(total),
        "lines": lines,
        "paid": trans.closed and sum(p.amount for p in trans.payments) > zero,
        "collected": meta.get("collected", False),
        "cancelled": trans.closed and sum(
            (p.amount for p in trans.payments), zero) == zero,
        "id_rejected": meta.get("rejected", False),
    }


def _till_user(session):
    u = session.get(User, settings.EMF_KIOSK_USER)
    if not u:
        raise ConfigurationError("Kiosk till user is not configured")
    return u


def place_order(session, *, items):
    """Create an unpaid kiosk order transaction for the given items.

    Validates each stockline against the location and available stock, then
    builds a Transaction with the priced lines and kiosk metadata (order ref,
    expiry, soft_only) and returns the order-response dict. Raises a
    KioskOrderError subclass on any validation failure.
    """
    current_session = Session.current(session)
    if current_session is None:
        raise NoActiveSession("No till session is active.")

    if not items:
        raise OrderError("Order must contain at least one item.")

    quantities = Counter()
    for item in items:
        try:
            stockline_id = int(item.get("stockline_id", 0))
        except (TypeError, ValueError):
            raise OrderError("stockline_id must be an integer.")
        try:
            qty = int(item.get("qty", 1))
        except (TypeError, ValueError):
            raise OrderError("qty must be an integer.")
        if qty <= 0:
            raise OrderError("qty must be positive.")
        quantities[stockline_id] += qty

    # Load all the stocklines into the session in one database roundtrip.
    session.query(StockLine)\
        .filter(StockLine.id.in_(list(quantities.keys())))\
        .options(joinedload(StockLine.stocktype).joinedload(StockType.unit),
                 joinedload(StockLine.stocktype)
                 .undefer(StockType.remaining))\
        .all()

    now = datetime.datetime.now()
    expires_at = now + default_timeout

    trans = Transaction(session=current_session, notes="Kiosk order")
    session.add(trans)

    soft_only = True

    for stockline_id, qty in sorted(quantities.items()):
        line = session.get(StockLine, stockline_id)
        if not line:
            raise OrderError(
                f"Stock line {stockline_id} does not exist.",
                stockline_id=stockline_id)
        if line.location != settings.EMF_KIOSK_LOCATION:
            raise OrderError(
                f"Stock line {stockline_id} is not in "
                f"location {settings.EMF_KIOSK_LOCATION}.",
                stockline_id=stockline_id)
        if line.linetype != "continuous":
            raise OrderError(
                f"{line.name} is not a continuous stock line.",
                stockline_id=stockline_id)
        st = line.stocktype
        if st.saleprice is None:
            raise PriceNotSet(
                f"{st} does not have a sale price set.",
                stockline_id=stockline_id)

        if st.abv and st.abv > Decimal("0.5"):
            soft_only = False

        total_stock_qty = Decimal(qty) * st.unit.base_units_per_sale_unit
        if line.remaining < total_stock_qty:
            raise InsufficientStock(
                f"There is not enough stock on sale for {line.name}.",
                stockline_id=line.id,
                requested=total_stock_qty,
                available=line.remaining)

        sell, unallocated, _remaining = st.calculate_sale(total_stock_qty)
        if unallocated > zero or not sell:
            raise InsufficientStock(
                f"There is not enough stock on sale for {line.name}.",
                stockline_id=line.id,
                requested=total_stock_qty,
                available=line.remaining)

        tl = Transline(
            transaction=trans,
            items=qty,
            amount=st.saleprice,
            department=st.department,
            transcode='S',
            text=f"{st} {st.unit.sale_unit_name}",
            user=_till_user(session),
            source=settings.EMF_KIOSK_SOURCE,
            protected=True)
        session.add(tl)

        for stockitem, stock_qty in sell:
            if stock_qty > max_quantity:
                raise OrderError(
                    f"Quantity is too large for {line.name}.",
                    stockline_id=line.id)
            session.add(StockOut(
                transline=tl,
                stockitem=stockitem,
                qty=stock_qty,
                removecode_id="sold"))

    meta = {
        "created_at": _timestamp(now),
        "expires_at": _timestamp(expires_at),
        "soft_only": soft_only,
    }
    _set_meta(trans, meta)
    trans.set_meta(pending_meta_key, "yes")
    session.flush()
    # A bit weird having a self-referential transaction note, but hey-ho...
    trans.notes = f"Kiosk order {trans.id}"
    return _order_to_dict(trans, meta)


def expire_orders(session):
    """Delete unpaid kiosk orders past expiry; returns those removed.

    Only touches open transactions carrying kiosk metadata; skips
    part-paid orders and those in use at a register.

    Expiry deletes the whole transaction, rather than just voiding the
    transaction lines.
    """
    now = datetime.datetime.now()

    rows = session.query(TransactionMeta)\
        .filter(TransactionMeta.key == order_meta_key)\
        .join(Transaction)\
        .filter(Transaction.closed.is_(False))\
        .options(contains_eager(TransactionMeta.transaction)
                 .selectinload(Transaction.payments),
                 contains_eager(TransactionMeta.transaction)
                 .selectinload(Transaction.lines))\
        .all()

    expired = []
    for row in rows:
        trans = row.transaction
        meta = _read_meta(trans)
        if not meta:
            continue
        expires_at = _parse_timestamp(meta.get("expires_at"))
        if expires_at is None or expires_at > now:
            continue
        if trans.payments:
            continue
        if any(not line.protected for line in trans.lines):
            # Transaction has been altered on till
            continue
        if trans.user is not None:
            # Transaction is active in a register
            continue

        expired.append(trans.id)
        session.add(LogEntry(
            source=settings.EMF_KIOSK_EXPIRY_SOURCE,
            loguser=_till_user(session),
            description=f"Expired kiosk order {trans.logref}"))
        session.delete(trans)

    return expired


def list_orders(session):
    """Return active kiosk orders.

    Returns all unpaid orders (closed=False) plus all paid orders
    (closed=True, any session). Expired unpaid orders are already
    deleted by expire_orders, so nothing stale leaks in from the
    unpaid side.
    """
    rows = session.query(TransactionMeta)\
        .filter(TransactionMeta.key == pending_meta_key)\
        .options(
            selectinload(TransactionMeta.transaction)
            .joinedload(Transaction.meta),
            selectinload(TransactionMeta.transaction)
            .selectinload(Transaction.lines),
            selectinload(TransactionMeta.transaction)
            .selectinload(Transaction.payments))\
        .all()

    orders = []
    commit_needed = False
    for row in rows:
        trans = row.transaction
        meta = _read_meta(trans)
        if not meta:
            continue
        if meta.get("collected"):
            # This shouldn't happen, but just in case...
            _unset_pending(trans)
            commit_needed = True
            continue
        order_dict = _order_to_dict(trans, meta)
        if order_dict["cancelled"]:
            _unset_pending(trans)
            commit_needed = True
        orders.append(order_dict)

    if commit_needed:
        session.commit()

    return orders


def _check_token(request):
    """Validate the shared kiosk bearer token.
    """
    configured_token = settings.EMF_KIOSK_ORDER_TOKEN
    if not configured_token:
        raise ConfigurationError("Kiosk API token has not been configured.")
    header = request.META.get("HTTP_AUTHORIZATION")
    if not header:
        raise AuthorizationError(
            "Authorization header missing")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        raise AuthorizationError(
            "Incorrect scheme in Authorization header")
    if not token:
        raise AuthorizationError(
            "Missing bearer token in Authorization header")
    if not hmac.compare_digest(token, configured_token):
        raise AuthorizationError(
            "Invalid bearer token in Authorization header")


def _request_json(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise RequestBodyError("Request body is not JSON.")


def kiosk_api_view(view):
    @csrf_exempt
    def new_view(request, *args, **kwargs):
        try:
            _check_token(request)

            with tillsession() as session:
                return view(request, session, *args, **kwargs)

        except KioskOrderError as e:
            return JsonResponse(e.as_dict(), status=e.status_code)
        except sqlalchemy.exc.OperationalError as e:
            return JsonResponse(
                {
                    "error": "database-error",
                    "message": str(e),
                },
                status=503)

    return new_view


@kiosk_api_view
def orders(request, session):
    """Order collection endpoint: GET lists live orders, POST creates one."""
    if request.method == "GET":
        return JsonResponse({"orders": list_orders(session)})
    if request.method != "POST":
        raise RequestMethodError("Use GET or POST.")

    payload = _request_json(request)

    result = place_order(
        session,
        items=payload.get("items", []))

    session.commit()

    return JsonResponse(result, status=201)


@kiosk_api_view
def order_detail(request, session, transid):
    """Single-order resource: GET retrieves it, DELETE cancels it."""
    if request.method not in ("GET", "DELETE"):
        raise RequestMethodError("Use GET or DELETE.")

    trans = session.get(Transaction, transid, options=[
        joinedload(Transaction.meta),
        selectinload(Transaction.lines),
        selectinload(Transaction.payments)
    ])

    if not trans:
        raise OrderNotFound("Order not found.")

    meta = _read_meta(trans)
    if not meta:
        raise OrderNotFound("Order not found.")

    if request.method == "DELETE":
        if trans.closed or trans.payments:
            raise OrderStateError(
                "Order has already been paid or cancelled and cannot "
                "now be cancelled.")

        if trans.user is not None:
            raise OrderStateError(
                "Order is currently being processed at a till.")

        session.add(LogEntry(
            source=settings.EMF_KIOSK_SOURCE,
            loguser=_till_user(session),
            description=f"Cancelled kiosk order {trans.logref}"))

        _set_meta(trans, meta)
        _unset_pending(trans)
        voidlines = [
            tl.void(trans, _till_user(session), settings.EMF_KIOSK_SOURCE)
            for tl in list(trans.lines)]
        session.add_all([v for v in voidlines if v is not None])
        trans.closed = True
        session.commit()

    return JsonResponse(_order_to_dict(trans, meta))


@kiosk_api_view
def update_order(request, session, transid, action=None):
    """Mark an order collected (POST); drops it from future poll results."""
    if request.method != "POST":
        raise RequestMethodError("Use POST.")

    trans = session.get(Transaction, transid, options=[
        joinedload(Transaction.meta)])

    if trans is None:
        raise OrderNotFound("Order not found.")

    meta = _read_meta(trans)
    if meta is None:
        raise OrderNotFound("Order not found.")

    # An order cannot be marked as collected or rejected if it has already
    # been marked as one or the other.
    if meta.get("collected", False):
        raise OrderStateError("Order already collected.")
    if meta.get("rejected", False):
        raise OrderStateError("Order already marked as rejected.")

    if action == "collect":
        meta["collected"] = True
        _set_meta(trans, meta)
        _unset_pending(trans)
        session.add(LogEntry(
            source=settings.EMF_KIOSK_SOURCE,
            loguser=_till_user(session),
            description=f"Kiosk order {trans.logref} marked collected"))
        session.commit()

    elif action == "id-reject":
        meta["rejected"] = True
        _set_meta(trans, meta)
        session.add(LogEntry(
            source=settings.EMF_KIOSK_SOURCE,
            loguser=_till_user(session),
            description=f"Kiosk order {trans.logref} marked rejected"))
        session.add(RefusalsLog(
            user=_till_user(session),
            terminal=settings.EMF_KIOSK_SOURCE,
            details=f"ID rejected via kiosk (order {transid})"))
        session.commit()

    return JsonResponse({"ok": True, "transaction_id": transid})
