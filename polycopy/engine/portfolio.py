"""Position ledger.

Each open position is split into *lots*, one per leader whose trades built it.
That lets PolyCopy mirror a leader's exit precisely (sell exactly the shares
copied from that leader) and measure every leader's live copy performance.
Realized PnL is written to ``pnl_events`` as it happens, which powers the daily
PnL chart and per-leader analytics.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from polycopy.db import Database
from polycopy.engine.risk import ExposureSnapshot
from polycopy.models import Fill, MarketInfo

DUST_SHARES = 0.01
DAY = 86400.0


@dataclass(slots=True)
class SellResult:
    shares: float
    proceeds: float
    realized: float
    closed: bool


class Portfolio:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------- paper cash
    def ensure_paper_account(self, starting_balance: float) -> None:
        if self.db.one("SELECT id FROM paper_account WHERE id=1") is None:
            self.db.insert("paper_account", {
                "id": 1, "cash": starting_balance, "starting_balance": starting_balance,
                "created_at": time.time(),
            })

    def paper_cash(self) -> float:
        return float(self.db.scalar("SELECT cash FROM paper_account WHERE id=1") or 0.0)

    def paper_starting_balance(self) -> float:
        return float(self.db.scalar("SELECT starting_balance FROM paper_account WHERE id=1") or 0.0)

    def _paper_cash_add(self, amount: float) -> None:
        self.db.execute("UPDATE paper_account SET cash = cash + ? WHERE id=1", (amount,))

    def reset_paper(self, starting_balance: float) -> None:
        with self.db.transaction() as db:
            for table in ("lots",):
                db.execute(
                    f"DELETE FROM {table} WHERE position_id IN "
                    "(SELECT id FROM positions WHERE mode='paper')"
                )
            for table in ("positions", "orders", "signals", "pnl_events", "equity"):
                db.execute(f"DELETE FROM {table} WHERE mode='paper'")
            db.execute("DELETE FROM paper_account")
            db.execute(
                "INSERT INTO paper_account(id, cash, starting_balance, created_at) VALUES(1,?,?,?)",
                (starting_balance, starting_balance, time.time()),
            )
        for key in ("paper_peak_pnl", "paper_day"):
            self.db.kv_set(key, None)

    # --------------------------------------------------------------- queries
    def open_positions(self, mode: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM positions WHERE mode=? AND status='open' ORDER BY opened_at DESC",
            (mode,),
        )

    def position(self, position_id: int) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM positions WHERE id=?", (position_id,))

    def open_position_for(self, mode: str, asset_id: str) -> dict[str, Any] | None:
        return self.db.one(
            "SELECT * FROM positions WHERE mode=? AND asset_id=? AND status='open'",
            (mode, asset_id),
        )

    def open_in_condition(self, mode: str, condition_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM positions WHERE mode=? AND condition_id=? AND status='open'",
            (mode, condition_id),
        )

    def lots(self, position_id: int) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM lots WHERE position_id=? AND shares > 0 ORDER BY id", (position_id,)
        )

    def lot(self, position_id: int, wallet: str) -> dict[str, Any] | None:
        return self.db.one(
            "SELECT * FROM lots WHERE position_id=? AND wallet=?", (position_id, wallet.lower())
        )

    def open_lots_for_leader(self, mode: str, wallet: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT l.*, p.asset_id, p.condition_id, p.title, p.outcome FROM lots l "
            "JOIN positions p ON p.id = l.position_id "
            "WHERE p.mode=? AND p.status='open' AND l.wallet=? AND l.shares > 0",
            (mode, wallet.lower()),
        )

    def all_open_lots(self, mode: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT l.*, p.asset_id, p.condition_id, p.title FROM lots l "
            "JOIN positions p ON p.id = l.position_id "
            "WHERE p.mode=? AND p.status='open' AND l.shares > 0",
            (mode,),
        )

    # ---------------------------------------------------------------- writes
    def record_buy(
        self,
        mode: str,
        *,
        wallet: str,
        asset_id: str,
        market: MarketInfo,
        fill: Fill,
        title: str | None = None,
        outcome: str | None = None,
        slug: str | None = None,
        event_slug: str | None = None,
        icon: str | None = None,
        category: str | None = None,
    ) -> int:
        now = time.time()
        wallet = wallet.lower()
        cost = fill.usdc + fill.fee
        with self.db.transaction() as db:
            pos = db.one(
                "SELECT * FROM positions WHERE mode=? AND asset_id=? AND status='open'",
                (mode, asset_id),
            )
            if pos is None:
                leaders = [wallet]
                position_id = db.insert("positions", {
                    "mode": mode,
                    "asset_id": asset_id,
                    "condition_id": market.condition_id,
                    "title": title or market.question,
                    "outcome": outcome or market.outcome_label(asset_id),
                    "slug": slug or market.slug,
                    "event_slug": event_slug or market.event_slug,
                    "icon": icon or market.icon,
                    "category": category,
                    "shares": fill.shares,
                    "cost": cost,
                    "invested": cost,
                    "fees": fill.fee,
                    "status": "open",
                    "opened_at": now,
                    "end_ts": market.expected_resolution_ts(),
                    "last_price": fill.avg_price,
                    "last_price_ts": now,
                    "leaders": leaders,
                })
            else:
                position_id = pos["id"]
                leaders = list(pos.get("leaders") or [])
                if wallet not in leaders:
                    leaders.append(wallet)
                db.execute(
                    "UPDATE positions SET shares=shares+?, cost=cost+?, invested=invested+?, "
                    "fees=fees+?, leaders=? WHERE id=?",
                    (fill.shares, cost, cost, fill.fee, _json(leaders), position_id),
                )
            db.execute(
                "INSERT INTO lots(position_id, wallet, shares, cost, invested, opened_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(position_id, wallet) DO UPDATE SET "
                "shares=shares+excluded.shares, cost=cost+excluded.cost, "
                "invested=invested+excluded.invested, closed_at=NULL",
                (position_id, wallet, fill.shares, cost, cost, now),
            )
            if mode == "paper":
                db.execute("UPDATE paper_account SET cash = cash - ? WHERE id=1", (cost,))
        return position_id

    def record_sell(
        self,
        mode: str,
        position_id: int,
        fill: Fill,
        *,
        wallet: str | None = None,
        category: str | None = None,
    ) -> SellResult:
        """Book a sale. With ``wallet`` the shares come out of that leader's lot;
        otherwise they come out of every lot pro rata."""
        now = time.time()
        proceeds_total = fill.usdc - fill.fee
        with self.db.transaction() as db:
            pos = db.one("SELECT * FROM positions WHERE id=?", (position_id,))
            if pos is None or pos["shares"] <= 0:
                return SellResult(0.0, 0.0, 0.0, True)
            requested = min(fill.shares, pos["shares"])
            lots = db.query(
                "SELECT * FROM lots WHERE position_id=? AND shares > 0 ORDER BY id", (position_id,)
            )
            allocations = _allocate(lots, requested, wallet) or _allocate(lots, requested, None)
            sold = sum(qty for _, qty in allocations) if allocations else requested
            realized_total = 0.0
            cost_out_total = 0.0
            if not allocations and pos["shares"] > 0:
                cost_out_total = pos["cost"] * sold / pos["shares"]
                realized_total = proceeds_total - cost_out_total
                db.insert("pnl_events", {
                    "mode": mode, "ts": now, "position_id": position_id, "wallet": None,
                    "kind": "sell", "amount": realized_total,
                    "category": category or pos.get("category"),
                })
            for lot, qty in allocations:
                if qty <= 0:
                    continue
                share_of_sale = qty / sold if sold > 0 else 0.0
                proceeds = proceeds_total * share_of_sale
                cost_out = lot["cost"] * (qty / lot["shares"]) if lot["shares"] > 0 else 0.0
                realized = proceeds - cost_out
                remaining = lot["shares"] - qty
                closed_lot = remaining <= DUST_SHARES
                db.execute(
                    "UPDATE lots SET shares=?, cost=?, realized_pnl=realized_pnl+?, closed_at=? "
                    "WHERE id=?",
                    (
                        0.0 if closed_lot else remaining,
                        0.0 if closed_lot else lot["cost"] - cost_out,
                        realized,
                        now if closed_lot else None,
                        lot["id"],
                    ),
                )
                db.insert("pnl_events", {
                    "mode": mode, "ts": now, "position_id": position_id,
                    "wallet": lot["wallet"], "kind": "sell", "amount": realized,
                    "category": category or pos.get("category"),
                })
                realized_total += realized
                cost_out_total += cost_out
            remaining_shares = pos["shares"] - sold
            closed = remaining_shares <= DUST_SHARES
            db.execute(
                "UPDATE positions SET shares=?, cost=?, realized_pnl=realized_pnl+?, fees=fees+?, "
                "status=?, result=?, closed_at=?, exit_pending=? WHERE id=?",
                (
                    0.0 if closed else remaining_shares,
                    0.0 if closed else max(pos["cost"] - cost_out_total, 0.0),
                    realized_total,
                    fill.fee,
                    "closed" if closed else "open",
                    "sold" if closed else None,
                    now if closed else None,
                    0 if closed else pos["exit_pending"],
                    position_id,
                ),
            )
            if mode == "paper":
                db.execute("UPDATE paper_account SET cash = cash + ? WHERE id=1", (proceeds_total,))
        return SellResult(sold, proceeds_total, realized_total, closed)

    def settle(self, mode: str, position_id: int, payout_per_share: float) -> float:
        """Close a position at resolution. Returns realized PnL."""
        now = time.time()
        with self.db.transaction() as db:
            pos = db.one("SELECT * FROM positions WHERE id=?", (position_id,))
            if pos is None or pos["status"] != "open":
                return 0.0
            lots = db.query("SELECT * FROM lots WHERE position_id=? AND shares > 0", (position_id,))
            realized_total = 0.0
            for lot in lots:
                payout = lot["shares"] * payout_per_share
                realized = payout - lot["cost"]
                realized_total += realized
                db.execute(
                    "UPDATE lots SET shares=0, cost=0, realized_pnl=realized_pnl+?, closed_at=? "
                    "WHERE id=?",
                    (realized, now, lot["id"]),
                )
                db.insert("pnl_events", {
                    "mode": mode, "ts": now, "position_id": position_id, "wallet": lot["wallet"],
                    "kind": "resolution", "amount": realized, "category": pos.get("category"),
                })
            if not lots:
                realized_total = pos["shares"] * payout_per_share - pos["cost"]
            payout_total = pos["shares"] * payout_per_share
            result = "won" if payout_per_share > 0.75 else "lost" if payout_per_share < 0.25 else "split"
            db.execute(
                "UPDATE positions SET status='resolved', result=?, payout=?, "
                "realized_pnl=realized_pnl+?, closed_at=?, last_price=?, last_price_ts=?, "
                "redeemed=?, exit_pending=0 WHERE id=?",
                (
                    result, payout_total, realized_total, now, payout_per_share, now,
                    1 if (mode == "paper" or payout_total <= 0) else 0, position_id,
                ),
            )
            if mode == "paper":
                db.execute("UPDATE paper_account SET cash = cash + ? WHERE id=1", (payout_total,))
        return realized_total

    def reduce_to_wallet_balance(self, position_id: int, wallet_shares: float) -> None:
        """Live safety net: never track more shares than the wallet actually holds."""
        pos = self.position(position_id)
        if pos is None or pos["shares"] <= wallet_shares + DUST_SHARES:
            return
        factor = max(wallet_shares, 0.0) / pos["shares"] if pos["shares"] > 0 else 0.0
        with self.db.transaction() as db:
            db.execute("UPDATE lots SET shares=shares*?, cost=cost*? WHERE position_id=?",
                       (factor, factor, position_id))
            db.execute("UPDATE positions SET shares=shares*?, cost=cost*? WHERE id=?",
                       (factor, factor, position_id))

    def mark(self, position_id: int, price: float) -> None:
        self.db.execute(
            "UPDATE positions SET last_price=?, last_price_ts=? WHERE id=?",
            (price, time.time(), position_id),
        )

    def set_exit_pending(self, position_id: int, pending: bool) -> None:
        self.db.execute("UPDATE positions SET exit_pending=? WHERE id=?",
                        (int(pending), position_id))

    # -------------------------------------------------------------- analytics
    def exposure(self, mode: str, cash: float, base_capital: float) -> ExposureSnapshot:
        positions = self.open_positions(mode)
        market: dict[str, float] = {}
        total = 0.0
        value = 0.0
        unrealized = 0.0
        for p in positions:
            total += p["cost"]
            market[p["condition_id"]] = market.get(p["condition_id"], 0.0) + p["cost"]
            price = p["last_price"] if p["last_price"] is not None else (
                p["cost"] / p["shares"] if p["shares"] else 0.0
            )
            value += p["shares"] * price
            unrealized += p["shares"] * price - p["cost"]
        leader: dict[str, float] = {}
        for lot in self.all_open_lots(mode):
            leader[lot["wallet"]] = leader.get(lot["wallet"], 0.0) + lot["cost"]
        realized = self.realized_total(mode)
        return ExposureSnapshot(
            equity=cash + value + self.unredeemed_value(mode),
            cash=cash,
            bot_pnl_total=realized + unrealized,
            total_exposure=total,
            market_exposure=market,
            leader_exposure=leader,
            open_positions=len(positions),
        )

    def unredeemed_value(self, mode: str) -> float:
        if mode != "live":
            return 0.0
        return float(self.db.scalar(
            "SELECT COALESCE(SUM(payout),0) FROM positions WHERE mode='live' AND status='resolved' "
            "AND redeemed=0 AND payout > 0"
        ) or 0.0)

    def realized_total(self, mode: str) -> float:
        return float(self.db.scalar(
            "SELECT COALESCE(SUM(amount),0) FROM pnl_events WHERE mode=?", (mode,)
        ) or 0.0)

    def realized_since(self, mode: str, since: float) -> float:
        return float(self.db.scalar(
            "SELECT COALESCE(SUM(amount),0) FROM pnl_events WHERE mode=? AND ts>=?", (mode, since)
        ) or 0.0)

    def unrealized_total(self, mode: str) -> float:
        total = 0.0
        for p in self.open_positions(mode):
            price = p["last_price"] if p["last_price"] is not None else 0.0
            total += p["shares"] * price - p["cost"]
        return total

    def leader_live_stats(self, mode: str, wallet: str) -> dict[str, float]:
        row = self.db.one(
            "SELECT COUNT(*) AS closed, COALESCE(SUM(l.invested),0) AS invested, "
            "COALESCE(SUM(l.realized_pnl),0) AS pnl FROM lots l JOIN positions p "
            "ON p.id=l.position_id WHERE p.mode=? AND l.wallet=? AND l.shares <= 0",
            (mode, wallet.lower()),
        ) or {}
        invested = float(row.get("invested") or 0.0)
        pnl = float(row.get("pnl") or 0.0)
        return {
            "closed": int(row.get("closed") or 0),
            "invested": invested,
            "pnl": pnl,
            "roi": pnl / invested if invested > 0 else 0.0,
        }


def _allocate(lots: list[dict[str, Any]], qty: float, wallet: str | None):
    if wallet is not None:
        wallet = wallet.lower()
        for lot in lots:
            if lot["wallet"] == wallet:
                return [(lot, min(qty, lot["shares"]))]
        return []
    total = sum(lot["shares"] for lot in lots)
    if total <= 0:
        return []
    return [(lot, qty * lot["shares"] / total) for lot in lots]


def _json(value: Any) -> str:
    return json.dumps(value)
