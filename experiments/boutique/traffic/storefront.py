"""Bounded synthetic shopping requests against a loopback-forwarded Boutique.

The upstream payment and email services are demo implementations. These requests
use synthetic data and never contact a public store or a real payment provider.
"""

from datetime import datetime, timezone
from http.cookiejar import CookieJar
from html.parser import HTMLParser
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener


PRODUCT_ID = "OLJCESPC7Z"
MAX_PAGE = 512 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


class StorefrontProbe:
    def __init__(self, base_url, *, timeout=10, allow_cluster_service=False):
        parsed = urlsplit(base_url)
        cluster_service = (allow_cluster_service and parsed.hostname is not None
                           and re.fullmatch(r"frontend\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.svc\.cluster\.local", parsed.hostname))
        if (parsed.scheme != "http" or (parsed.hostname != "127.0.0.1" and not cluster_service) or parsed.port is None
                or not 1 <= parsed.port <= 65535 or parsed.path or parsed.query or parsed.fragment
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("Storefront probe requires an explicit loopback or approved cluster service HTTP port")
        if type(timeout) not in {int, float} or not 0 < timeout <= 20:
            raise ValueError("Storefront timeout must be bounded")
        self.base_url, self.timeout = base_url, timeout

    def _request(self, opener, path, fields=None):
        data = urlencode(fields).encode() if fields is not None else None
        request = Request(self.base_url + path, data=data,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            try:
                response = opener.open(request, timeout=self.timeout)
            except HTTPError as error:
                response = error
            with response:
                body = response.read(MAX_PAGE + 1)
                if len(body) > MAX_PAGE:
                    return None, "", "ResponseTooLarge"
                return response.status, body.decode("utf-8", errors="replace"), None
        except (OSError, URLError, TimeoutError) as error:
            return None, "", type(error).__name__

    def journey(self):
        # A fresh cookie jar prevents carts/orders from contaminating other runs.
        opener = build_opener(HTTPCookieProcessor(CookieJar()), NoRedirect())
        home, _, home_error = self._request(opener, "/")
        added, _, add_error = self._request(opener, "/cart", {"product_id": PRODUCT_ID, "quantity": "1"})
        cart, cart_body, cart_error = self._request(opener, "/cart")
        order, order_body, order_error = self._request(opener, "/cart/checkout", {
            "email": "sentinel-test@example.invalid", "street_address": "1 Test Street",
            "zip_code": "94043", "city": "Test City", "state": "CA", "country": "United States",
            "credit_card_number": "4111111111111111", "credit_card_expiration_month": "12",
            "credit_card_expiration_year": str(datetime.now(timezone.utc).year + 1), "credit_card_cvv": "123",
        })
        page = PageText()
        page.feed(order_body)
        text = " ".join(" ".join(page.parts).split())
        # The footer also contains session/request UUIDs. Require the actual
        # upstream Confirmation # field, not an arbitrary UUID in the page.
        confirmation = re.search(r"\bConfirmation\s*#\s*([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\b", text, re.I)
        order_marker = "your order is complete" in text.lower()
        known = all(code is not None for code in (home, added, cart, order))
        passed = (home == 200 and added in {302, 303} and cart == 200 and PRODUCT_ID in cart_body
                  and order == 200 and confirmation is not None and order_marker)
        return {
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "home_status": home, "add_to_cart_status": added, "cart_status": cart,
            "checkout_status": order, "cart_contains_product": PRODUCT_ID in cart_body,
            "order_confirmation_id": confirmation.group(1) if passed else None,
            "order_confirmation_found": confirmation is not None, "order_page_confirmed": order_marker,
            "passed": bool(passed) if known else None,
            "errors": {key: value for key, value in (("home", home_error), ("add", add_error),
                                                     ("cart", cart_error), ("checkout", order_error)) if value},
        }
