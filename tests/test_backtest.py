from polycopy.backtest import _as_of, run_backtest
from polycopy.config import Settings
from polycopy.gateway.simulated import SimData, SimWorld
from polycopy.models import MarketInfo


async def test_walk_forward_backtest_runs_on_simulated_market():
    data = SimData(SimWorld(seed=5, n_wallets=40, history_days=40))
    settings = Settings()
    report = await run_backtest(data, settings, window_days=20, progress=lambda *_: None)
    assert "## Results in the evaluation window" in report
    assert "PolyCopy selection (score)" in report and "Every candidate" in report


def test_selection_window_hides_later_results():
    settled_later = MarketInfo(condition_id="0x1", question="q", yes_token="a", no_token="b",
                               yes_price=1.0, no_price=0.0, closed=True, closed_ts=2000.0)
    settled_before = MarketInfo(condition_id="0x2", question="q", yes_token="c", no_token="d",
                                yes_price=1.0, no_price=0.0, closed=True, closed_ts=500.0)
    view = _as_of({"0x1": settled_later, "0x2": settled_before}, cutoff=1000.0)
    assert view["0x1"].resolution_payout("a") is None
    assert view["0x2"].resolution_payout("c") == 1.0
