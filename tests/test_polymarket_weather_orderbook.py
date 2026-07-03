from __future__ import annotations

import unittest

from weather_quant.buckets import parse_temperature_bucket
from weather_quant.engine import build_bucket_signal
from weather_quant.models import (
    MarketBucket,
    OrderBookLevel,
    OrderBookSnapshot,
    binary_contract_fee,
)
from weather_quant.risk import PositionSizer, RiskConfig


def make_bucket(orderbook: OrderBookSnapshot, *, price: float = 0.50) -> MarketBucket:
    return MarketBucket(
        market_id="test",
        question="Test weather market",
        slug=None,
        condition_id=None,
        outcome="80",
        price=price,
        bucket=parse_temperature_bucket("80"),
        token_id=orderbook.token_id,
        orderbook=orderbook,
    )


class OrderBookExecutionTest(unittest.TestCase):
    def test_market_buy_vwap_walks_asks(self) -> None:
        orderbook = OrderBookSnapshot(
            token_id="yes",
            asks=(
                OrderBookLevel(price=0.50, size=100),
                OrderBookLevel(price=0.40, size=100),
            ),
        )

        estimate = orderbook.estimate_market_buy(65, fee_rate=0.0)

        self.assertTrue(estimate.is_complete)
        self.assertAlmostEqual(estimate.filled_shares, 150.0)
        self.assertAlmostEqual(estimate.vwap or 0.0, 65 / 150)
        self.assertAlmostEqual(estimate.slippage or 0.0, (65 / 150 - 0.40) / 0.40)

    def test_market_sell_vwap_walks_bids(self) -> None:
        orderbook = OrderBookSnapshot(
            token_id="yes",
            bids=(
                OrderBookLevel(price=0.55, size=100),
                OrderBookLevel(price=0.60, size=100),
            ),
        )

        estimate = orderbook.estimate_market_sell(150, fee_rate=0.0)

        self.assertTrue(estimate.is_complete)
        self.assertAlmostEqual(estimate.net_value, 87.5)
        self.assertAlmostEqual(estimate.vwap or 0.0, 87.5 / 150)

    def test_partial_fill_when_depth_is_insufficient(self) -> None:
        orderbook = OrderBookSnapshot(
            token_id="yes",
            asks=(OrderBookLevel(price=0.40, size=10),),
        )

        estimate = orderbook.estimate_market_buy(10, fee_rate=0.0)

        self.assertFalse(estimate.is_complete)
        self.assertAlmostEqual(estimate.filled_shares, 10.0)
        self.assertAlmostEqual(estimate.remaining_usdc, 6.0)

    def test_fee_formula(self) -> None:
        self.assertAlmostEqual(
            binary_contract_fee(shares=100, price=0.40, fee_rate=0.05),
            1.2,
        )

    def test_cashout_ratio_uses_liquidation_value_over_mark_value(self) -> None:
        orderbook = OrderBookSnapshot(
            token_id="yes",
            bids=(OrderBookLevel(price=0.45, size=50),),
            asks=(OrderBookLevel(price=0.55, size=50),),
        )

        self.assertAlmostEqual(
            orderbook.cashout_ratio(shares=50, mark_price=0.50, fee_rate=0.0) or 0.0,
            0.90,
        )

    def test_liquidity_cap_limits_kelly_position(self) -> None:
        orderbook = OrderBookSnapshot(
            token_id="yes",
            bids=(OrderBookLevel(price=0.40, size=100),),
            asks=(OrderBookLevel(price=0.40, size=1000),),
        )
        signal = build_bucket_signal(
            market_bucket=make_bucket(orderbook, price=0.40),
            probability=0.80,
            buy_edge_threshold=0.01,
            use_orderbook=True,
            fee_rate=0.0,
            max_entry_slippage=0.01,
            max_exit_slippage=0.01,
            depth_usage_fraction=0.10,
        )

        recommendation = PositionSizer().size_yes(
            signal,
            RiskConfig(
                bankroll=1_000,
                kelly_mode="half",
                min_edge=0.01,
                max_trade_fraction=1.0,
                fee_rate=0.0,
                max_entry_slippage=0.01,
                max_exit_slippage=0.01,
                depth_usage_fraction=0.10,
            ),
        )

        self.assertTrue(recommendation.should_trade)
        self.assertAlmostEqual(recommendation.depth_based_stake_cap or 0.0, 4.0)
        self.assertAlmostEqual(recommendation.stake, 4.0)

    def test_illiquid_signal_is_downgraded(self) -> None:
        orderbook = OrderBookSnapshot(
            token_id="yes",
            asks=(OrderBookLevel(price=0.40, size=100),),
        )

        signal = build_bucket_signal(
            market_bucket=make_bucket(orderbook, price=0.40),
            probability=0.70,
            buy_edge_threshold=0.03,
            use_orderbook=True,
            fee_rate=0.0,
            min_cashout_ratio=0.80,
        )

        self.assertEqual(signal.recommendation, "HOLD_TO_RESOLUTION_ONLY")


if __name__ == "__main__":
    unittest.main()
