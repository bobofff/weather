from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from weather_quant.backtest import (
    HistoricalOrderBookSignal,
    HistoricalSignal,
    run_backtest,
    run_orderbook_snapshot_backtest,
)
from weather_quant.models import OrderBookLevel, OrderBookSnapshot
from weather_quant.risk import RiskConfig


class WeatherBacktestTest(unittest.TestCase):
    def test_run_backtest_sizes_and_settles_trades(self) -> None:
        result = run_backtest(
            (
                HistoricalSignal(
                    city_id="new-york",
                    target_date=date(2026, 7, 1),
                    kind="high",
                    outcome="80",
                    probability=0.60,
                    market_price=0.50,
                    settled_outcome=True,
                ),
                HistoricalSignal(
                    city_id="new-york",
                    target_date=date(2026, 7, 2),
                    kind="high",
                    outcome="81",
                    probability=0.60,
                    market_price=0.50,
                    settled_outcome=False,
                ),
            ),
            risk=RiskConfig(
                bankroll=1_000,
                kelly_mode="half",
                min_edge=0.01,
                max_trade_fraction=0.03,
            ),
        )

        self.assertEqual(len(result.trades), 2)
        self.assertEqual(result.stats.settled_trades, 2)
        self.assertAlmostEqual(result.stats.win_rate, 0.5)
        self.assertLess(result.stats.total_pnl, 1.0)

    def test_orderbook_snapshot_backtest_uses_vwap_entry(self) -> None:
        result = run_orderbook_snapshot_backtest(
            (
                HistoricalOrderBookSignal(
                    timestamp=datetime(2026, 7, 1, tzinfo=timezone.utc),
                    city_id="new-york",
                    target_date=date(2026, 7, 1),
                    kind="high",
                    outcome="80",
                    probability=0.70,
                    settled_outcome=True,
                    orderbook=OrderBookSnapshot(
                        token_id="yes-token",
                        bids=(OrderBookLevel(price=0.39, size=100),),
                        asks=(OrderBookLevel(price=0.40, size=100),),
                    ),
                ),
            ),
            risk=RiskConfig(
                bankroll=1_000,
                kelly_mode="half",
                min_edge=0.01,
                max_trade_fraction=0.03,
                fee_rate=0.0,
            ),
        )

        self.assertEqual(len(result.trades), 1)
        self.assertAlmostEqual(result.trades[0].price, 0.40)
        self.assertGreater(result.stats.total_pnl, 0.0)


if __name__ == "__main__":
    unittest.main()
