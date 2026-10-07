"""Exercise the real SDK code paths against mocked Polymarket HTTP responses.

These fixtures follow the response shapes the official ``polymarket-client``
SDK parses (data API v2 envelopes, gamma keyset pages, CLOB books), so the
tests catch any mismatch between PolyCopy and the SDK it is built on.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from polycopy.gateway.polymarket import PolymarketData

DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

WALLET = "0x1111111111111111111111111111111111111111"
CID = "0x" + "ab" * 32
TX = "0x" + "cd" * 32


def envelope(items, *, cursor=None):
    return {"data": items, "pagination": {"has_more": cursor is not None, "next_cursor": cursor}}


def gamma_market(**overrides):
    market = {
        "id": "501",
        "conditionId": CID,
        "question": "Will the Knicks beat the Celtics?",
        "slug": "knicks-celtics",
        "outcomes": json.dumps(["Knicks", "Celtics"]),
        "outcomePrices": json.dumps(["0.55", "0.45"]),
        "clobTokenIds": json.dumps(["111", "222"]),
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "negRisk": False,
        "endDate": "2026-10-08T02:00:00Z",
        "startDate": "2026-10-01T00:00:00Z",
        "liquidityNum": 15000,
        "volume24hr": 4000,
        "bestBid": 0.54,
        "bestAsk": 0.56,
        "spread": 0.02,
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.01,
        "feesEnabled": True,
        "feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.2},
        "gameStartTime": "2026-10-07T23:30:00Z",
        "sportsMarketType": "moneyline",
        "events": [{"id": "9", "slug": "nba-knicks-celtics", "title": "Knicks vs Celtics"}],
        "tags": [{"id": "1", "slug": "sports", "label": "Sports"}],
    }
    market.update(overrides)
    return market


@pytest.fixture
async def data():
    gateway = PolymarketData()
    yield gateway
    await gateway.close()


@respx.mock
async def test_leaderboard(data):
    route = respx.get(f"{DATA}/v2/leaderboard").mock(
        return_value=httpx.Response(200, json=envelope([
            {"rank": 1, "user_id": WALLET, "pnl": 12345.6, "volume": 99999, "user_name": "alice",
             "profile_image": "", "x_username": "", "verified": False},
        ]))
    )
    rows = await data.leaderboard(window="week", category="sports", limit=50)
    assert route.called
    params = route.calls.last.request.url.params
    assert params["time_period"] == "week"
    assert params["category"] == "sports"
    assert params["sort_by"] == "PNL"
    assert rows[0].wallet == WALLET
    assert rows[0].pnl == pytest.approx(12345.6)
    assert rows[0].name == "alice"


@respx.mock
async def test_user_trades_requests_maker_and_taker_fills(data):
    route = respx.get(f"{DATA}/v2/trades").mock(
        return_value=httpx.Response(200, json=envelope([
            {"proxy_wallet": WALLET, "token_id": "111", "condition_id": CID, "side": "BUY",
             "size": 100, "price": 0.42, "timestamp": 1791300000, "transaction_hash": TX,
             "title": "Will the Knicks beat the Celtics?", "slug": "knicks-celtics",
             "event_slug": "nba-knicks-celtics", "outcome": "Knicks", "outcome_index": 0,
             "name": "alice", "pseudonym": "Brave-Fox"},
        ]))
    )
    trades = await data.user_trades(WALLET, since_ts=1791000000, max_items=100)
    params = route.calls.last.request.url.params
    assert params["taker_only"] == "false"
    assert params["user"] == WALLET
    assert params["start"] == "1791000000"
    trade = trades[0]
    assert (trade.asset_id, trade.side, trade.size, trade.price) == ("111", "BUY", 100.0, 0.42)
    assert trade.usdc == pytest.approx(42.0)
    assert trade.ts == 1791300000
    assert trade.outcome == "Knicks"


@respx.mock
async def test_user_activity_parses_trades_and_merges(data):
    route = respx.get(f"{DATA}/v2/activity").mock(
        return_value=httpx.Response(200, json=envelope([
            {"type": "TRADE", "proxy_wallet": WALLET, "timestamp": 1791300100,
             "transaction_hash": TX, "condition_id": CID, "token_id": "111", "side": "SELL",
             "size": 50, "usdc_size": 30, "price": 0.6, "outcome": "Knicks", "title": "T"},
            {"type": "MERGE", "proxy_wallet": WALLET, "timestamp": 1791300200,
             "transaction_hash": TX, "condition_id": CID, "usdc_size": 25, "title": "T"},
            {"type": "DEPOSIT", "proxy_wallet": WALLET, "timestamp": 1791300300,
             "transaction_hash": TX, "usdc_size": 1000},
        ]))
    )
    events = await data.user_activity(WALLET, since_ts=1791300000)
    params = route.calls.last.request.url.params
    assert params["type"] == "TRADE,MERGE"
    assert params["sort_direction"] == "DESC"
    assert [e.kind for e in events] == ["TRADE", "MERGE"]
    sell, merge = events
    assert sell.side == "SELL" and sell.usdc == 30 and sell.size == 50
    assert merge.condition_id == CID and merge.size == 25 and merge.asset_id == ""


@respx.mock
async def test_positions(data):
    respx.get(f"{DATA}/v2/positions").mock(
        return_value=httpx.Response(200, json=envelope([
            {"proxy_wallet": WALLET, "token_id": "111", "condition_id": CID,
             "current_size": 80, "avg_price": 0.4, "entry_cost_usdc": 32, "entry_fees_usdc": 0.4,
             "total_cost_usdc": 32.4, "current_price": 0.55, "current_value": 44,
             "total_size": 100, "realized_pnl": 3, "unrealized_pnl": 12, "total_pnl": 15,
             "percent_pnl": 37.5, "percent_realized_pnl": 9, "status": "OPEN",
             "redeemable": False, "mergeable": False, "negative_risk": False, "archived": False,
             "verified": True, "title": "T", "outcome": "Knicks", "end_date": "2026-10-08"},
        ]))
    )
    positions = await data.user_positions(WALLET, condition_ids=[CID])
    assert positions[0].size == 80
    assert positions[0].current_price == 0.55
    assert positions[0].end_date == "2026-10-08"


@respx.mock
async def test_markets_by_condition_maps_gamma_fields(data):
    respx.get(f"{GAMMA}/markets/keyset").mock(
        return_value=httpx.Response(200, json={"markets": [gamma_market()], "next_cursor": None})
    )
    markets = await data.markets_by_condition([CID])
    m = markets[CID]
    assert m.yes_token == "111" and m.no_token == "222"
    assert m.yes_label == "Knicks" and m.no_label == "Celtics"
    assert m.yes_price == 0.55 and m.no_price == 0.45
    assert m.event_slug == "nba-knicks-celtics"
    assert m.fees.enabled and m.fees.rate == 0.05 and m.fees.exponent == 1
    assert m.liquidity == 15000
    assert m.game_start_ts is not None and m.end_ts is not None and m.end_ts > m.game_start_ts
    assert not m.closed and m.accepting_orders
    assert m.resolution_payout("111") is None


@respx.mock
async def test_resolved_market_and_closed_fallback(data):
    calls = []

    def respond(request):
        calls.append(dict(request.url.params))
        if request.url.params.get("closed") == "true":
            market = gamma_market(closed=True, active=False, acceptingOrders=False,
                                  outcomePrices=json.dumps(["0", "1"]))
            return httpx.Response(200, json={"markets": [market], "next_cursor": None})
        return httpx.Response(200, json={"markets": [], "next_cursor": None})

    respx.get(f"{GAMMA}/markets/keyset").mock(side_effect=respond)
    markets = await data.markets_by_condition([CID])
    assert len(calls) == 2  # open lookup, then the closed=true fallback
    m = markets[CID]
    assert m.closed and m.is_resolved()
    assert m.resolution_payout("111") == 0.0
    assert m.resolution_payout("222") == 1.0


@respx.mock
async def test_order_book_is_best_first(data):
    respx.get(f"{CLOB}/book").mock(
        return_value=httpx.Response(200, json={
            "market": CID, "asset_id": "111", "timestamp": str(int(time.time() * 1000)),
            "bids": [{"price": "0.50", "size": "10"}, {"price": "0.53", "size": "20"}],
            "asks": [{"price": "0.60", "size": "5"}, {"price": "0.55", "size": "30"}],
            "min_order_size": "5", "tick_size": "0.01", "neg_risk": False, "hash": "abc",
            "last_trade_price": "0.54",
        })
    )
    book = await data.order_book("111")
    assert book.best_bid == 0.53 and book.best_ask == 0.55
    assert [lvl.price for lvl in book.asks] == [0.55, 0.60]
    assert book.tick_size == 0.01 and book.min_order_size == 5


@respx.mock
async def test_geoblock(data):
    respx.get("https://polymarket.com/api/geoblock").mock(
        return_value=httpx.Response(200, json={"blocked": True, "ip": "1.2.3.4",
                                               "country": "US", "region": "NY"})
    )
    status = await data.geoblock()
    assert status.blocked and status.country == "US"


@respx.mock
async def test_geoblock_failure_is_not_blocking(data):
    respx.get("https://polymarket.com/api/geoblock").mock(side_effect=httpx.ConnectError("down"))
    status = await data.geoblock()
    assert not status.blocked and not status.checked


async def test_wallet_candidates_derivation(data, monkeypatch):
    """Derived candidate addresses include the EOA and are probed for activity."""
    from eth_account import Account

    key = "0x" + "11" * 32
    signer = Account.from_key(key).address

    async def no_value(**_):
        raise RuntimeError("offline")

    monkeypatch.setattr(data.client, "get_portfolio_value", no_value)

    class Pager:
        async def first_page(self):
            raise RuntimeError("offline")

    monkeypatch.setattr(data.client, "list_activity", lambda **_: Pager())
    candidates = await data.wallet_candidates(key)
    types = {c["wallet_type"] for c in candidates}
    assert {"EOA", "POLY_PROXY", "GNOSIS_SAFE", "DEPOSIT_WALLET"} <= types
    assert any(c["address"] == signer for c in candidates)


@respx.mock
async def test_historical_lookup_asks_for_closed_markets_first(data):
    calls = []

    def respond(request):
        calls.append(dict(request.url.params))
        market = gamma_market(closed=True, outcomePrices=json.dumps(["1", "0"]))
        return httpx.Response(200, json={"markets": [market], "next_cursor": None})

    respx.get(f"{GAMMA}/markets/keyset").mock(side_effect=respond)
    markets = await data.markets_by_condition([CID], prefer_closed=True)
    assert len(calls) == 1 and calls[0]["closed"] == "true"
    assert markets[CID].resolution_payout("111") == 1.0
