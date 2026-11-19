"""Read the shop owner's Binance Pay history, to check customers' payments by themselves.

Uses GET /sapi/v1/pay/transactions ("Get Pay Trade History") with a read-only API key
("Enable Reading"; never trading or withdrawals). Each record is one Pay transfer; a
positive amount was received. The "Order ID" a payer sees in the Binance app is the
record's orderId field (not documented by Binance, but present in real responses since
2024); the documented transactionId (like "P_A25PQJVAD2171114") is a different code, so a
customer may send either.

    python -m shopbot.binance --check   test the key in .env from this computer
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import sys
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

log = logging.getLogger("shopbot.binance")

BASE_URLS = (  # the same API; api1-api4 are listed by Binance as faster but less stable
    "https://api.binance.com",
    "https://api-gcp.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
)
PAY_HISTORY = "/sapi/v1/pay/transactions"
KEY_RIGHTS = "/sapi/v1/account/apiRestrictions"
ACCOUNT = "/api/v3/account"  # its "uid" is the account's Binance ID
SERVER_TIME = "/api/v3/time"
RECV_WINDOW = 10_000  # ms a signed request stays valid (Binance allows up to 60 000)
PAGE_SIZE = 100  # Binance's maximum per request
MAX_PAGES = 10  # pages read back in time for one search
MIN_GAP = 1.0  # seconds between calls: each costs 3000 of the 180 000 weight a minute
CACHE_FOR = 5.0  # seconds a fetched page is reused (several customers checking at once)
INCOMING = {"C2C", "PAY"}  # a transfer to the owner, or a payment to the owner's QR code
ORDER_ID_RE = re.compile(r"^[0-9]{15,24}$")
TRANSACTION_ID_RE = re.compile(r"^[A-Z]{1,3}_[A-Z0-9_]{6,40}$")
EPOCH_2020 = 1_577_836_800  # 2020-01-01 UTC: an Order ID's top bits count seconds from here


class BinanceError(Exception):
    """Binance couldn't be asked; the message says why."""


class BinanceKeyError(BinanceError):
    """The API key or secret is wrong, lacks "Enable Reading", or isn't allowed from this
    server's IP address. The owner has to fix it."""


class BinanceUnavailable(BinanceError):
    """Binance can't be reached right now (network, maintenance, too many requests)."""


@dataclass(frozen=True)
class Payment:
    order_id: str  # what the payer sees as "Order ID" ("" if Binance didn't send one)
    transaction_id: str
    order_type: str
    amount: Decimal  # positive: received
    currency: str
    time_ms: int
    payer: str  # the payer's Binance nickname, if any
    note: str

    @property
    def received(self) -> bool:
        return self.amount > 0 and self.order_type in INCOMING

    def cents(self) -> int:
        """The amount in cents, rounded down (for coins worth 1 $ each)."""
        return int((self.amount * 100).to_integral_value(rounding=ROUND_DOWN))


def clean_order_id(text: str) -> Optional[str]:
    """The Order ID (digits) or transaction ID a customer sent, or None if it isn't one.
    Leading zeros are dropped: "0466..." is the same payment as "466...", and one payment
    must have only one spelling, or it could be claimed twice."""
    value = re.sub(r"[\s\-]", "", text or "").strip().strip("#").upper()
    if value.startswith("ORDERID:"):
        value = value[len("ORDERID:"):]
    if value.isdigit():
        value = value.lstrip("0")
    if ORDER_ID_RE.fullmatch(value) or TRANSACTION_ID_RE.fullmatch(value):
        return value
    return None


def order_id_time(order_id: str) -> Optional[int]:
    """When a payment with this Order ID was made (ms), read from the ID itself. Binance
    doesn't document this, so it only narrows the first search; None if it looks wrong."""
    if not ORDER_ID_RE.fullmatch(order_id):
        return None
    seconds = EPOCH_2020 + (int(order_id) >> 31)
    if not EPOCH_2020 < seconds < time.time() + 86_400:
        return None
    return seconds * 1000


def _payment(raw: dict) -> Optional[Payment]:
    try:
        amount = Decimal(str(raw.get("amount", "0")))
    except InvalidOperation:
        return None
    payer = raw.get("payerInfo") or {}
    order_id = str(raw.get("orderId") or "")
    return Payment(
        order_id=order_id.lstrip("0") if order_id.isdigit() else order_id,  # as clean_order_id
        transaction_id=str(raw.get("transactionId") or ""),
        order_type=str(raw.get("orderType") or ""),
        amount=amount,
        currency=str(raw.get("currency") or "").upper(),
        time_ms=int(raw.get("transactionTime") or 0),
        payer=str(payer.get("name") or "") if isinstance(payer, dict) else "",
        note=str(raw.get("note") or ""),
    )


class BinancePay:
    """A small client for the owner's own Pay history (read only)."""

    def __init__(self, api_key: str, api_secret: str, *,
                 base_urls: tuple[str, ...] = BASE_URLS, http: Optional[httpx.AsyncClient] = None):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.base_urls = base_urls
        self._http = http
        self._offset_ms = 0  # Binance's clock minus ours
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._cache: dict[tuple[int, int], tuple[float, list[Payment]]] = {}

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20.0)
        return self._http

    # ---------------------------------------------------------------- requests
    async def _call(self, path: str, params: Optional[dict] = None, *, signed: bool = True) -> Any:
        """One request, trying the other API hosts if one can't be reached."""
        async with self._lock:  # one call at a time, at most one a second
            wait = self._last_call + MIN_GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._call_any_host(path, params or {}, signed)
            finally:
                self._last_call = time.monotonic()

    async def _call_any_host(self, path: str, params: dict, signed: bool) -> Any:
        problem: Optional[BinanceError] = None
        for base in self.base_urls:
            try:
                return await self._request(base, path, params, signed)
            except BinanceUnavailable as exc:
                problem = exc
                log.info("Binance host %s didn't answer (%s); trying the next one", base, exc)
        raise problem or BinanceUnavailable("Binance can't be reached")

    async def _request(self, base: str, path: str, params: dict, signed: bool,
                       retried: bool = False) -> Any:
        query = dict(params)
        headers = {}
        if signed:
            query["timestamp"] = int(time.time() * 1000) + self._offset_ms
            query["recvWindow"] = RECV_WINDOW
            payload = urlencode(query)
            query["signature"] = hmac.new(self.api_secret, payload.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = self.api_key
        try:
            response = await self._client().get(base + path, params=query, headers=headers)
        except httpx.HTTPError as exc:
            raise BinanceUnavailable(f"network: {exc.__class__.__name__}") from exc
        try:
            data = response.json()
        except ValueError:
            data = None
        code = data.get("code") if isinstance(data, dict) else None
        refused = isinstance(data, dict) and data.get("success") is False
        if response.status_code == 200 and not (isinstance(code, int) and code < 0) and not refused:
            return data
        message = (data.get("msg") or data.get("message")) if isinstance(data, dict) else response.text[:200]
        if code == -1021 and signed and not retried:  # our clock is off: ask Binance's, retry once
            server = await self._request(base, SERVER_TIME, {}, False)
            self._offset_ms = int(server["serverTime"]) - int(time.time() * 1000)
            log.warning("This computer's clock is %d ms off Binance's; adjusted", self._offset_ms)
            return await self._request(base, path, params, signed, retried=True)
        if response.status_code in (401, 403) or code in (-2008, -2014, -2015, -1022):
            raise BinanceKeyError(f"Binance refused the API key ({message}). Check the key and "
                                  "secret, that \"Enable Reading\" is on, and that the key allows "
                                  "this server's IP address.")
        if response.status_code == 451:  # lasting, and the same on every host
            raise BinanceError("Binance doesn't serve this server's location (HTTP 451)")
        if response.status_code in (418, 429) or response.status_code >= 500:
            raise BinanceUnavailable(f"Binance is busy (HTTP {response.status_code})")
        raise BinanceError(f"Binance answered HTTP {response.status_code}: {message}")

    # ----------------------------------------------------------------- history
    async def page(self, start_ms: int, end_ms: int) -> list[Payment]:
        """Up to 100 payments between start_ms and end_ms (both in ms)."""
        key = (start_ms // 1000, end_ms // 1000)
        cached = self._cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < CACHE_FOR:
            return cached[1]
        data = await self._call(PAY_HISTORY, {"startTime": start_ms, "endTime": end_ms,
                                              "limit": PAGE_SIZE})
        records = data.get("data") if isinstance(data, dict) else None
        if not isinstance(records, list):
            raise BinanceError("Binance's answer has no payment list")
        payments = [p for p in (_payment(r) for r in records if isinstance(r, dict)) if p is not None]
        self._cache = {k: v for k, v in self._cache.items() if time.monotonic() - v[0] < CACHE_FOR}
        self._cache[key] = (time.monotonic(), payments)
        return payments

    async def search(self, wanted: str, start_ms: int, end_ms: int) -> Optional[Payment]:
        """The payment with this Order ID or transaction ID between start_ms and end_ms.
        A full page may hold only part of the window (Binance doesn't say which part, and
        its docs and real answers disagree on the order), so a full window is split in two
        and both halves are searched, newest first, up to MAX_PAGES requests."""
        windows = [(start_ms, end_ms)]
        for _ in range(MAX_PAGES):
            if not windows:
                return None
            start, end = windows.pop()
            if end <= start:
                continue
            payments = await self.page(start, end)
            for payment in payments:
                if wanted in (payment.order_id, payment.transaction_id.upper()):
                    return payment
            if len(payments) >= PAGE_SIZE and end - start > 1000:
                middle = (start + end) // 2
                windows += [(start, middle), (middle + 1, end)]  # the newer half first
        return None

    async def find(self, wanted: str, since_ms: int) -> Optional[Payment]:
        """The payment a customer claims, made after since_ms; None if it isn't there (yet)."""
        offset = self._offset_ms
        found = await self._find(wanted, since_ms)
        if found is None and self._offset_ms != offset:
            # Our clock was corrected during the search, which therefore ended too early
            # (by our slow clock): search again up to Binance's "now".
            found = await self._find(wanted, since_ms)
        return found

    async def _find(self, wanted: str, since_ms: int) -> Optional[Payment]:
        now = int(time.time() * 1000) + self._offset_ms
        hint = order_id_time(wanted)
        if hint is not None and hint >= since_ms - 3_600_000:  # first look near when it was paid
            found = await self.search(wanted, max(since_ms, hint - 3_600_000), min(now, hint + 3_600_000))
            if found is not None:
                return found
        return await self.search(wanted, since_ms, now)

    async def key_rights(self) -> dict:
        """What the key may do (enableReading, enableWithdrawals, ipRestrict...)."""
        data = await self._call(KEY_RIGHTS)
        return data if isinstance(data, dict) else {}

    async def account_id(self) -> str:
        """The Binance ID (UID) of the account the key belongs to ("" if not told)."""
        data = await self._call(ACCOUNT, {"omitZeroBalances": "true"})
        uid = data.get("uid") if isinstance(data, dict) else None
        return str(uid) if uid else ""


DANGEROUS_RIGHTS = ("enableWithdrawals", "enableInternalTransfer", "permitsUniversalTransfer",
                    "enableSpotAndMarginTrading", "enableMargin", "enableFutures",
                    "enableVanillaOptions", "enablePortfolioMarginTrading")


async def check(api_key: str, api_secret: str, days: int = 7, binance_id: str = "") -> str:
    """A one-paragraph report on the key, for the deploy log and the owner."""
    client = BinancePay(api_key, api_secret)
    try:
        rights = await client.key_rights()
        risky = [name for name in DANGEROUS_RIGHTS if rights.get(name)]
        now = int(time.time() * 1000)
        payments = await client.page(now - days * 86_400_000, now)
        received = [p for p in payments if p.received]
        lines = [f"Binance: the key works from this server; {len(received)} Pay payment(s) "
                 f"received in the last {days} days are visible."]
        if not rights.get("enableReading"):
            lines.append("WARNING: the key doesn't have \"Enable Reading\".")
        if risky:
            lines.append("WARNING: this key can do more than read (" + ", ".join(risky) + "). "
                         "Create a new key with only \"Enable Reading\" and delete this one.")
        if not rights.get("ipRestrict"):
            lines.append("Tip: restrict the key to this server's IP address in Binance's API "
                         "Management.")
        if binance_id:
            try:
                uid = await client.account_id()
            except BinanceError:
                uid = ""  # only a check: the payments above are what matters
            if uid and uid != binance_id:
                lines.append(f"WARNING: this key belongs to Binance ID {uid}, but customers are "
                             f"told to pay Binance ID {binance_id} (BINANCE_PAY_ID). Payments to "
                             f"{binance_id} can't be seen with this key: use a key of that "
                             "account, or set BINANCE_PAY_ID to the right ID.")
        transfers = [p for p in received if p.order_type == "C2C"]
        if transfers and not any(p.order_id for p in transfers):
            lines.append("WARNING: Binance no longer sends the Order ID (orderId) with payments, "
                         "so customers' Order IDs can't be found. Deposits go to the owner.")
        return "\n".join(lines)
    except BinanceError as exc:
        return f"Binance: {exc}"
    finally:
        await client.close()


def main() -> int:
    from .config import ConfigError, Settings

    if sys.argv[1:] != ["--check"]:
        print(__doc__)
        return 2
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not (settings.binance_api_key and settings.binance_api_secret):
        print("Binance: no API key set (BINANCE_API_KEY and BINANCE_API_SECRET), so deposits "
              "are checked by hand.")
        return 0
    report = asyncio.run(check(settings.binance_api_key, settings.binance_api_secret,
                               binance_id=settings.binance_pay_id))
    print(report)
    return 0 if report.startswith("Binance: the key works") else 1


if __name__ == "__main__":
    sys.exit(main())
