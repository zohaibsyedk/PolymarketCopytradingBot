"""Login and order placement through the real SDK signing path, with Polymarket mocked.

This proves PolyCopy's live wrapper drives the official SDK correctly: API-key
derivation, balance lookup, order construction/signing for a FAK market order
with a price cap, response parsing into a Fill, and rejection handling.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx
from eth_account import Account

from polycopy.gateway.polymarket import PolymarketAccount
from polycopy.models import BookLevel, OrderBook

CLOB = "https://clob.polymarket.com"
KEY = "0x" + "4c" * 32
SIGNER = Account.from_key(KEY).address
CID = "0x" + "ab" * 32
SECRET = base64.urlsafe_b64encode(b"s" * 32).decode()


def mock_clob(router, order_response: dict) -> dict:
    calls: dict = {"orders": []}
    router.post(f"{CLOB}/auth/api-key").mock(
        return_value=httpx.Response(200, json={"apiKey": "key-1", "secret": SECRET,
                                               "passphrase": "pass-1"})
    )
    router.get(f"{CLOB}/auth/derive-api-key").mock(
        return_value=httpx.Response(200, json={"apiKey": "key-1", "secret": SECRET,
                                               "passphrase": "pass-1"})
    )
    router.get(f"{CLOB}/auth/api-keys").mock(
        return_value=httpx.Response(200, json={"apiKeys": ["key-1"]})
    )
    router.get(f"{CLOB}/balance-allowance").mock(
        return_value=httpx.Response(200, json={"balance": "250500000", "allowances": {}})
    )
    router.get(f"{CLOB}/auth/ban-status/closed-only").mock(
        return_value=httpx.Response(200, json={"closed_only": False})
    )
    router.get(url__regex=rf"{CLOB}/markets-by-token/.*").mock(
        return_value=httpx.Response(200, json={"condition_id": CID})
    )
    router.get(f"{CLOB}/clob-markets/{CID}").mock(
        return_value=httpx.Response(200, json={"nr": False, "t": [{"t": "111"}, {"t": "222"}],
                                               "mts": 0.01, "fd": {"r": 0.05, "e": 1}})
    )
    router.get(f"{CLOB}/tick-size").mock(
        return_value=httpx.Response(200, json={"minimum_tick_size": 0.01})
    )
    router.get(f"{CLOB}/neg-risk").mock(return_value=httpx.Response(200, json={"neg_risk": False}))

    def on_order(request):
        calls["orders"].append(json.loads(request.content))
        return httpx.Response(200, json=order_response)

    router.post(f"{CLOB}/order").mock(side_effect=on_order)
    return calls


def book() -> OrderBook:
    return OrderBook(asset_id="111", bids=[BookLevel(0.53, 500)], asks=[BookLevel(0.55, 500)],
                     tick_size=0.01, min_order_size=5)


@respx.mock(assert_all_called=False)
async def test_login_and_market_buy_eoa(respx_mock):
    calls = mock_clob(respx_mock, {
        "errorMsg": "", "makingAmount": "10", "orderID": "0xorder1", "status": "matched",
        "success": True, "takingAmount": "18.18", "tradeIDs": ["t1"], "transactionsHashes": [],
    })
    account, cache = await PolymarketAccount.login(private_key=KEY, wallet=SIGNER)
    try:
        assert account.info.wallet_type == "EOA"
        assert account.info.cash == pytest.approx(250.5)
        assert cache["credentials"]["key"] == "key-1"
        fill = await account.market_buy("111", usdc=10.0, max_price=0.5734, book=book())
        assert fill.ok, fill.error
        assert fill.shares == pytest.approx(18.18)
        assert fill.usdc == pytest.approx(10.0)
        assert fill.order_id == "0xorder1"
        sent = calls["orders"][-1]
        order = sent["order"]
        assert order["tokenId"] == "111"
        assert str(order["side"]).upper() in ("BUY", "0")
        assert sent["orderType"] == "FAK"
        assert order["signature"].startswith("0x")
    finally:
        await account.close()


@respx.mock(assert_all_called=False)
async def test_market_sell_and_rejection(respx_mock):
    mock_clob(respx_mock, {
        "errorMsg": "no orders found to match with FAK order. FAK orders are partially filled "
                    "or killed if no match is found.",
        "makingAmount": "", "orderID": "", "status": "", "success": False, "takingAmount": "",
    })
    account, _ = await PolymarketAccount.login(private_key=KEY, wallet=SIGNER)
    try:
        fill = await account.market_sell("111", shares=20, min_price=0.5, book=book())
        assert not fill.ok
        assert fill.error_code == "fak_not_filled"
    finally:
        await account.close()


async def test_login_requires_wallet():
    with pytest.raises(Exception, match="wallet address is required"):
        await PolymarketAccount.login(private_key=KEY, wallet=None)
