from __future__ import annotations

import unittest

from weather_quant.buckets import parse_temperature_bucket
from weather_quant.config import city_from_mapping
from weather_quant.models import (
    MarketBucket,
    OrderBookLevel,
    OrderBookSnapshot,
    Portfolio,
    Position,
)
from weather_quant.portfolio import (
    calculate_hedge_lock,
    calculate_portfolio_scenarios,
    generate_passive_exit_plan,
    orderbook_overround,
    recommend_passive_entry,
    value_position,
)


def market_bucket(
    outcome: str,
    *,
    bid: float,
    ask: float,
    probability: float = 0.0,
) -> MarketBucket:
    return MarketBucket(
        market_id="test",
        question="Test weather market",
        slug=None,
        condition_id=None,
        outcome=outcome,
        price=(bid + ask) / 2.0,
        bucket=parse_temperature_bucket(outcome),
        token_id=f"token-{outcome}",
        orderbook=OrderBookSnapshot(
            token_id=f"token-{outcome}",
            bids=(OrderBookLevel(price=bid, size=10_000),),
            asks=(OrderBookLevel(price=ask, size=10_000),),
        ),
        raw_payload={"probability": probability},
    )


class WeatherPortfolioTest(unittest.TestCase):
    def test_portfolio_payoff_scenario_matrix(self) -> None:
        buckets = (
            market_bucket("79", bid=0.20, ask=0.22),
            market_bucket("80", bid=0.40, ask=0.42),
        )
        portfolio = Portfolio(
            positions=(
                Position(
                    outcome="80",
                    bucket=parse_temperature_bucket("80"),
                    shares=100,
                    total_cost=30,
                ),
            )
        )

        scenarios = calculate_portfolio_scenarios(portfolio, buckets)

        pnl_by_outcome = {scenario.outcome: scenario.net_pnl for scenario in scenarios}
        self.assertAlmostEqual(pnl_by_outcome["79"], -30.0)
        self.assertAlmostEqual(pnl_by_outcome["80"], 70.0)

    def test_full_coverage_true_arbitrage_and_hedge_cost(self) -> None:
        buckets = (
            market_bucket("80", bid=0.35, ask=0.40, probability=0.50),
            market_bucket("81", bid=0.18, ask=0.20, probability=0.50),
        )
        portfolio = Portfolio(
            positions=(
                Position(
                    outcome="80",
                    bucket=parse_temperature_bucket("80"),
                    shares=100,
                    total_cost=20,
                ),
            )
        )

        result = calculate_hedge_lock(
            portfolio,
            buckets,
            target_profit=0,
            fee_rate=0.0,
        )

        self.assertTrue(result.is_true_arbitrage)
        self.assertFalse(result.is_tail_risk_lock)
        self.assertAlmostEqual(result.hedge_cost, 5.0)
        self.assertAlmostEqual(result.hedge_legs[0].shares, 25.0)
        self.assertAlmostEqual(result.worst_case_pnl, 0.0)

    def test_tail_risk_lock_after_excluding_low_probability_bucket(self) -> None:
        buckets = (
            market_bucket("80", bid=0.35, ask=0.40, probability=0.49),
            market_bucket("81", bid=0.18, ask=0.20, probability=0.49),
            market_bucket("82", bid=0.45, ask=0.50, probability=0.02),
        )
        portfolio = Portfolio(
            positions=(
                Position(
                    outcome="80",
                    bucket=parse_temperature_bucket("80"),
                    shares=100,
                    total_cost=20,
                ),
            )
        )

        result = calculate_hedge_lock(
            portfolio,
            buckets,
            tail_probability_cutoff=0.05,
            max_tail_probability=0.05,
            fee_rate=0.0,
        )

        self.assertFalse(result.is_true_arbitrage)
        self.assertTrue(result.is_tail_risk_lock)
        self.assertEqual(result.recommendation, "HEDGE_LOCK")
        self.assertAlmostEqual(result.covered_probability, 0.98)
        self.assertAlmostEqual(result.uncovered_tail_probability, 0.02)
        self.assertLess(result.worst_case_pnl, result.covered_worst_case_pnl)

    def test_overround_detection_blocks_true_arbitrage_label(self) -> None:
        buckets = (
            market_bucket("80", bid=0.30, ask=0.40, probability=0.33),
            market_bucket("81", bid=0.30, ask=0.40, probability=0.33),
            market_bucket("82", bid=0.30, ask=0.40, probability=0.34),
        )
        metrics = orderbook_overround(buckets)

        self.assertTrue(metrics["is_overround"])
        self.assertAlmostEqual(float(metrics["ask_sum"]), 1.20)

        result = calculate_hedge_lock(
            Portfolio(),
            buckets,
            fee_rate=0.0,
        )
        self.assertFalse(result.is_true_arbitrage)
        self.assertEqual(result.recommendation, "SKIP_OVERROUND")

    def test_infeasible_hedge_discards_runaway_intermediate_solution(self) -> None:
        buckets = (
            market_bucket("80", bid=0.30, ask=0.40, probability=0.34),
            market_bucket("81", bid=0.30, ask=0.40, probability=0.33),
            market_bucket("82", bid=0.30, ask=0.40, probability=0.33),
        )
        portfolio = Portfolio(
            positions=(
                Position(
                    outcome="80",
                    bucket=parse_temperature_bucket("80"),
                    shares=100,
                    total_cost=30,
                ),
            )
        )

        result = calculate_hedge_lock(portfolio, buckets, fee_rate=0.0)

        self.assertFalse(result.is_feasible)
        self.assertEqual(result.recommendation, "HEDGE_INFEASIBLE")
        self.assertEqual(result.hedge_legs, ())
        self.assertEqual(result.hedge_cost, 0.0)
        self.assertAlmostEqual(result.worst_case_pnl, -30.0)
        self.assertAlmostEqual(result.covered_worst_case_pnl, -30.0)
        self.assertTrue(any("未收敛" in note for note in result.notes))

    def test_passive_exit_ladder_and_mark_vs_liquidation_value(self) -> None:
        bucket = market_bucket("80", bid=0.40, ask=0.60)
        position = Position(
            outcome="80",
            bucket=parse_temperature_bucket("80"),
            shares=100,
            total_cost=30,
        )

        valuation = value_position(position, bucket, fee_rate=0.0)
        plan = generate_passive_exit_plan(position, bucket, fee_rate=0.0)

        self.assertAlmostEqual(valuation.mark_value, 50.0)
        self.assertAlmostEqual(valuation.liquidation_value, 40.0)
        self.assertAlmostEqual(valuation.cashout_ratio or 0.0, 0.80)
        self.assertEqual(plan.action, "DISTRIBUTE_PASSIVE")
        self.assertEqual(len(plan.ladder), 3)
        self.assertAlmostEqual(plan.ladder[0].shares, 20.0)
        self.assertAlmostEqual(plan.ladder[0].limit_price, 0.45)
        self.assertAlmostEqual(plan.retained_shares, 20.0)

    def test_passive_entry_requires_fee_and_exit_cost_edge(self) -> None:
        bucket = market_bucket("80", bid=0.30, ask=0.31)

        recommendation = recommend_passive_entry(
            bucket,
            model_probability=0.32,
            min_edge=0.03,
            taker_fee_rate=0.05,
        )

        self.assertEqual(recommendation.action, "SKIP_NO_EDGE")
        self.assertLess(recommendation.net_edge, 0.03)

    def test_station_level_fields_are_preserved(self) -> None:
        city = city_from_mapping(
            "new-york",
            {
                "name": "New York",
                "latitude": 40.7,
                "longitude": -74.0,
                "settlement_station": "Central Park",
                "station_id": "KNYC",
                "metar_source": "NOAA/METAR",
                "forecast_granularity": "station",
                "settlement_unit": "F",
            },
        )

        self.assertEqual(city.settlement_station, "Central Park")
        self.assertEqual(city.station_id, "KNYC")
        self.assertEqual(city.metar_source, "NOAA/METAR")
        self.assertEqual(city.forecast_granularity, "station")


if __name__ == "__main__":
    unittest.main()
