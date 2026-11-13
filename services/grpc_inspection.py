"""Bounded, schema-aware inspection of Boutique unary shipping and cart RPC requests.

Envoy owns TLS and HTTP/2. This module receives the reassembled gRPC body;
it neither decrypts arbitrary captures nor claims general exploit detection.
The schema follows microservices-demo v0.10.7/protos/demo.proto. Each
protected route (P5.04: shipping and cart) admits only its own methods.
"""

import re


MAX_BODY = 65536
ROUTE_METHODS = {
    "shipping": frozenset({"/hipstershop.ShippingService/GetQuote", "/hipstershop.ShippingService/ShipOrder"}),
    "cart": frozenset({"/hipstershop.CartService/AddItem", "/hipstershop.CartService/GetCart",
                       "/hipstershop.CartService/EmptyCart"}),
}
METHODS = frozenset().union(*ROUTE_METHODS.values())
PRODUCT = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
# The storefront's session id (a UUID) is the cart's user_id.
USER_ID = re.compile(r"[A-Za-z0-9-]{1,64}\Z")
# The storefront offers quantities 1 to 10 per add-to-cart.
CART_QUANTITY = range(1, 11)


class InspectionDenied(ValueError):
    """A stable rule code; never include the submitted field value."""


def _varint(data, position):
    value = 0
    for shift in range(0, 70, 7):
        if position >= len(data):
            raise InspectionDenied("PROTOBUF_TRUNCATED")
        byte = data[position]
        position += 1
        if shift == 63 and byte > 1:
            raise InspectionDenied("PROTOBUF_VARINT")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, position
    raise InspectionDenied("PROTOBUF_VARINT")


def _fields(data, schema, *, repeated=()):
    values, position, count = {}, 0, 0
    while position < len(data):
        count += 1
        if count > 256:
            raise InspectionDenied("PROTOBUF_FIELD_LIMIT")
        tag, position = _varint(data, position)
        number, wire = tag >> 3, tag & 7
        if number not in schema or schema[number] != wire:
            raise InspectionDenied("PROTOBUF_SCHEMA")
        if number in values and number not in repeated:
            # Protobuf normally permits last-one-wins/merged messages. Reject
            # ambiguity so the inspector and upstream cannot see different data.
            raise InspectionDenied("PROTOBUF_DUPLICATE")
        if wire == 0:
            value, position = _varint(data, position)
        else:
            size, position = _varint(data, position)
            if size > len(data) - position:
                raise InspectionDenied("PROTOBUF_TRUNCATED")
            value, position = data[position:position + size], position + size
        values.setdefault(number, []).append(value)
    return values


def _text(value, maximum):
    try:
        decoded = value.decode("utf-8")
    except UnicodeError:
        raise InspectionDenied("PROTOBUF_UTF8") from None
    if len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in decoded):
        raise InspectionDenied("TEXT_CONSTRAINT")
    return decoded


def _user_id(request):
    values = request.get(1)
    if not values:
        raise InspectionDenied("USER_ID")
    if not USER_ID.fullmatch(_text(values[0], 64)):
        raise InspectionDenied("USER_ID")


def _cart(path, request):
    """AddItem {user_id, item}; GetCart and EmptyCart {user_id}. Returns the item count."""
    if not path.endswith("/AddItem"):
        _user_id(_fields(request, {1: 2}))
        return None
    fields = _fields(request, {1: 2, 2: 2})
    _user_id(fields)
    if not fields.get(2):
        raise InspectionDenied("ITEM_REQUIRED")
    item = _fields(fields[2][0], {1: 2, 2: 0})
    if not PRODUCT.fullmatch(_text(item.get(1, [b""])[0], 64)):
        raise InspectionDenied("PRODUCT_ID")
    if item.get(2, [0])[0] not in CART_QUANTITY:
        raise InspectionDenied("QUANTITY")
    return 1


def inspect_request(path, content_type, encoding, body, *, partial=False, route=None):
    """Return only allowlisted metadata. Raw fields never enter the result.

    route names the protected route the request arrived on; when given, the
    method must belong to that route, so one route's listener can never admit
    another route's calls.
    """
    method = path if path in METHODS else "unrecognized"
    result = {"method": method, "bytes": len(body), "verdict": "DENY", "rule": "METHOD_NOT_ALLOWED"}
    try:
        if path not in METHODS:
            raise InspectionDenied("METHOD_NOT_ALLOWED")
        if route is not None and path not in ROUTE_METHODS.get(route, ()):
            raise InspectionDenied("ROUTE_MISMATCH")
        if content_type.split(";", 1)[0].strip().lower() not in {"application/grpc", "application/grpc+proto"}:
            raise InspectionDenied("CONTENT_TYPE")
        if encoding not in {"", "identity"}:
            raise InspectionDenied("COMPRESSION_UNSUPPORTED")
        if partial or len(body) > MAX_BODY:
            raise InspectionDenied("BODY_LIMIT")
        if len(body) < 5 or body[0] != 0:
            raise InspectionDenied("GRPC_FRAME")
        size = int.from_bytes(body[1:5], "big")
        if size != len(body) - 5:
            raise InspectionDenied("UNARY_FRAME_LENGTH")
        if path in ROUTE_METHODS["cart"]:
            items = _cart(path, body[5:])
            result.update(verdict="ALLOW", rule="CART_SCHEMA")
            if items is not None:
                result["items"] = items
            return result
        request = _fields(body[5:], {1: 2, 2: 2}, repeated={2})
        if path.endswith("/ShipOrder") and not request.get(1):
            raise InspectionDenied("ADDRESS_REQUIRED")
        for address in request.get(1, []):
            fields = _fields(address, {1: 2, 2: 2, 3: 2, 4: 2, 5: 0})
            for field in (1, 2, 3, 4):
                for value in fields.get(field, []):
                    _text(value, 256)
            if fields.get(5, [0])[0] > 999999999:
                raise InspectionDenied("ZIP_CONSTRAINT")
        items = request.get(2, [])
        # The real storefront requests a zero-cost quote for an empty cart.
        minimum = 0 if path.endswith("/GetQuote") else 1
        if not minimum <= len(items) <= 20:
            raise InspectionDenied("ITEM_COUNT")
        for item in items:
            fields = _fields(item, {1: 2, 2: 0})
            if not PRODUCT.fullmatch(_text(fields.get(1, [b""])[0], 64)):
                raise InspectionDenied("PRODUCT_ID")
            if not 1 <= fields.get(2, [0])[0] <= 100:
                raise InspectionDenied("QUANTITY")
        result.update(verdict="ALLOW", rule="SHIPPING_SCHEMA", items=len(items))
    except InspectionDenied as error:
        result["rule"] = str(error)
    return result
