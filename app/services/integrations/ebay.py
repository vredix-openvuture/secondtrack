"""eBay integration — market-price suggestions via the Browse API.

Enable via SECONDTRACK_EBAY_ENABLED=1 with an eBay developer App ID (Client ID)
and Cert ID (Client Secret) from https://developer.ebay.com. We fetch an
application OAuth token (client-credentials grant, cached ~2h) and query the
Browse API for current listings of a part name to suggest a price. Production
endpoints; read-only.
"""
from __future__ import annotations

import base64
import statistics
import time

import httpx

from ... import runtime

_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_SCOPE = "https://api.ebay.com/oauth/api_scope"

# In-process token cache (single uvicorn worker): {"token": str, "exp": float}.
_token_cache: dict = {"token": "", "exp": 0.0}


def is_enabled() -> bool:
    return bool(
        runtime.get_bool("ebay_enabled")
        and runtime.get("ebay_client_id")
        and runtime.get("ebay_client_secret")
    )


def _require() -> None:
    if not is_enabled():
        raise RuntimeError("eBay integration is disabled")


def _marketplace() -> str:
    return runtime.get("ebay_marketplace") or "EBAY_DE"


def _get_token() -> str:
    now = time.time()
    if _token_cache["token"] and _token_cache["exp"] > now + 60:
        return _token_cache["token"]
    cid = runtime.get("ebay_client_id")
    secret = runtime.get("ebay_client_secret")
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    with httpx.Client(timeout=20.0) as c:
        r = c.post(
            _OAUTH_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": _SCOPE},
        )
        r.raise_for_status()
        j = r.json()
    _token_cache["token"] = j["access_token"]
    _token_cache["exp"] = now + float(j.get("expires_in", 7200))
    return _token_cache["token"]


def suggest_price(query: str, limit: int = 50) -> dict:
    """Look up current used-condition eBay listings for `query` and return a
    price suggestion: {suggested, median, min, max, count, currency, query}.
    `suggested` is the median asking price (a robust rough market value)."""
    _require()
    token = _get_token()
    with httpx.Client(timeout=20.0) as c:
        r = c.get(
            _BROWSE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": _marketplace(),
            },
            params={
                "q": query,
                "limit": limit,
                # Used condition (3000) + Seller refurbished (2500), fixed price.
                "filter": "conditionIds:{3000|2500},buyingOptions:{FIXED_PRICE}",
            },
        )
        r.raise_for_status()
        data = r.json()

    prices: list[float] = []
    currency = "EUR"
    for it in data.get("itemSummaries", []) or []:
        p = it.get("price") or {}
        val = p.get("value")
        if val is None:
            continue
        try:
            prices.append(float(val))
            currency = p.get("currency", currency)
        except (TypeError, ValueError):
            continue

    if not prices:
        return {"suggested": None, "median": None, "min": None, "max": None,
                "count": 0, "currency": currency, "query": query}
    prices.sort()
    med = statistics.median(prices)
    return {
        "suggested": round(med, 2),
        "median": round(med, 2),
        "min": round(prices[0], 2),
        "max": round(prices[-1], 2),
        "count": len(prices),
        "currency": currency,
        "query": query,
    }
