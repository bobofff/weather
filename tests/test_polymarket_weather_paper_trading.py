from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from weather_quant import web_api
from weather_quant.db import init_database
from weather_quant.models import (
    CityConfig,
    MarketBucket,
    OrderBookLevel,
    OrderBookSnapshot,
    TemperatureBucket,
)
from weather_quant.settlement import SettlementObservation
from weather_quant.storage import WeatherStorage


def city() -> CityConfig:
    return CityConfig(
        city_id="paper-city",
        name="Paper City",
        latitude=40.0,
        longitude=-74.0,
        timezone="America/New_York",
        settlement_station="Central Park",
        station_id="KNYC",
        metar_source="NOAA/METAR",
        forecast_granularity="station",
        settlement_unit="F",
    )


def context() -> dict[str, str]:
    return {
        "cityId": "paper-city",
        "cityName": "Paper City",
        "targetDate": "2026-07-05",
        "kind": "high",
        "settlementStation": "Central Park",
        "stationId": "KNYC",
        "metarSource": "NOAA/METAR",
    }


def buckets() -> tuple[TemperatureBucket, ...]:
    return (
        TemperatureBucket(label="80 to 83", lower=80, upper=83, unit="F"),
        TemperatureBucket(label="83 to 86", lower=83, upper=86, unit="F"),
        TemperatureBucket(label="86 to 89", lower=86, upper=89, unit="F"),
    )


def market_bucket(index: int, *, bid: float = 0.18, ask: float = 0.20, ask_size: float = 500) -> MarketBucket:
    bucket = buckets()[index]
    return MarketBucket(
        market_id=f"m{index}",
        question="paper test",
        slug="paper-test",
        condition_id="0xpaper",
        outcome=bucket.label,
        price=(bid + ask) / 2,
        bucket=bucket,
        token_id=f"token-{index}",
        orderbook=OrderBookSnapshot(
            token_id=f"token-{index}",
            bids=(OrderBookLevel(price=bid, size=500),),
            asks=(OrderBookLevel(price=ask, size=ask_size),),
        ),
    )


def no_ask_bucket() -> MarketBucket:
    bucket = buckets()[0]
    return MarketBucket(
        market_id="m-no-ask",
        question="paper test",
        slug="paper-test",
        condition_id="0xpaper",
        outcome=bucket.label,
        price=0.20,
        bucket=bucket,
        token_id="token-0",
        orderbook=OrderBookSnapshot(
            token_id="token-0",
            bids=(OrderBookLevel(price=0.18, size=500),),
        ),
    )


def signal(
    *,
    bucket: TemperatureBucket | None = None,
    probability: float = 0.70,
    recommendation: str = "BUY_YES",
) -> dict[str, object]:
    bucket = bucket or buckets()[0]
    return {
        "outcome": bucket.label,
        "bucketLabel": bucket.label,
        "bucketKey": bucket.canonical_key,
        "tokenId": "token-0",
        "ensembleProbability": probability,
        "expectedExitCost": 0.0,
        "edge": probability - 0.20,
        "recommendation": recommendation,
    }


def markets_csv() -> str:
    return "\n".join(
        [
            "outcome,price,best_bid,best_ask,ask_size,token_id",
            "80 to 83,0.19,0.18,0.20,500,token-0",
            "83 to 86,0.19,0.18,0.20,500,token-1",
        ]
    )


class PaperTradingTest(unittest.TestCase):
    def test_schema_comments_complete_and_no_foreign_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "weather.db"
            init_database(db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                tables = [
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                ]
                missing_comments = []
                for table in tables:
                    self.assertEqual(
                        connection.execute(f"PRAGMA foreign_key_list({table})").fetchall(),
                        [],
                    )
                    if not connection.execute(
                        """
                        SELECT 1 FROM schema_comments
                        WHERE object_type = 'table' AND object_name = ?
                        """,
                        (table,),
                    ).fetchone():
                        missing_comments.append((table, None))
                    for column in [
                        row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                    ]:
                        if not connection.execute(
                            """
                            SELECT 1 FROM schema_comments
                            WHERE object_type = 'column'
                              AND object_name = ?
                              AND column_name = ?
                            """,
                            (table, column),
                        ).fetchone():
                            missing_comments.append((table, column))

        self.assertEqual(missing_comments, [])

    def test_buy_yes_paper_buy_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = WeatherStorage(Path(tmpdir) / "weather.db", initialize=True)
            result = storage.execute_paper_buy(
                signal=signal(),
                market_buckets=(market_bucket(0),),
                context=context(),
                initial_cash=100,
                stake_usdc=20,
                fee_rate=0.0,
            )
            portfolio = storage.paper_portfolio(
                initial_cash=100,
                market_buckets=(market_bucket(0),),
                fee_rate=0.0,
            )

        self.assertTrue(result["summary"]["accepted"])
        self.assertAlmostEqual(result["preview"]["filledShares"], 100)
        self.assertAlmostEqual(result["preview"]["vwap"], 0.20)
        self.assertAlmostEqual(result["preview"]["netCost"], 20)
        self.assertAlmostEqual(portfolio["summary"]["cash"], 80)
        self.assertEqual(portfolio["positions"][0]["status"], "OPEN")

    def test_rejects_insufficient_balance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = WeatherStorage(Path(tmpdir) / "weather.db", initialize=True)
            result = storage.execute_paper_buy(
                signal=signal(),
                market_buckets=(market_bucket(0),),
                context=context(),
                initial_cash=5,
                stake_usdc=20,
                fee_rate=0.0,
            )

        self.assertFalse(result["summary"]["accepted"])
        self.assertEqual(result["summary"]["rejectReason"], "INSUFFICIENT_BALANCE")
        self.assertEqual(result["order"]["status"], "REJECTED")

    def test_rejects_non_buy_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = WeatherStorage(Path(tmpdir) / "weather.db", initialize=True)
            preview = storage.paper_buy_preview(
                signal=signal(recommendation="WATCH"),
                market_buckets=(market_bucket(0),),
                context=context(),
                stake_usdc=20,
                fee_rate=0.0,
            )

        self.assertEqual(preview["rejectReason"], "NO_BUY_SIGNAL")

    def test_rejects_edge_too_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = WeatherStorage(Path(tmpdir) / "weather.db", initialize=True)
            preview = storage.paper_buy_preview(
                signal=signal(probability=0.21),
                market_buckets=(market_bucket(0),),
                context=context(),
                stake_usdc=20,
                min_edge=0.03,
                fee_rate=0.0,
            )

        self.assertEqual(preview["rejectReason"], "EDGE_TOO_LOW")

    def test_rejects_no_ask(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = WeatherStorage(Path(tmpdir) / "weather.db", initialize=True)
            preview = storage.paper_buy_preview(
                signal=signal(),
                market_buckets=(no_ask_bucket(),),
                context=context(),
                stake_usdc=20,
                fee_rate=0.0,
            )

        self.assertEqual(preview["rejectReason"], "NO_ASK")

    def test_settlement_reconcile_updates_realized_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "weather.db"
            storage = WeatherStorage(db_path, initialize=True)
            storage.execute_paper_buy(
                signal=signal(),
                market_buckets=(market_bucket(0),),
                context=context(),
                initial_cash=100,
                stake_usdc=20,
                fee_rate=0.0,
            )
            import_run_key = storage.save_settlement_import_run(
                city=city(),
                target_date="2026-07-05",
                kind="high",
                provider="fixture",
                station_id="KNYC",
                status="success",
            )
            storage.save_settlement(
                SettlementObservation(
                    city=city(),
                    target_date=date(2026, 7, 5),
                    kind="high",
                    observed_value=82.0,
                    unit="F",
                    source_provider="fixture",
                    source_url="https://example.test",
                    station_id="KNYC",
                    settlement_station="Central Park",
                    observation_count=24,
                    observation_start=datetime(2026, 7, 5, tzinfo=timezone.utc),
                    observation_end=datetime(2026, 7, 6, tzinfo=timezone.utc),
                    bucket_label="80 to 83",
                    bucket_key=buckets()[0].canonical_key,
                ),
                import_run_key=import_run_key,
            )
            result = storage.reconcile_paper_positions(
                city_id="paper-city",
                target_date="2026-07-05",
                kind="high",
            )
            portfolio = storage.paper_portfolio(initial_cash=100)

        self.assertEqual(result["summary"]["settledPositionCount"], 1)
        self.assertAlmostEqual(result["summary"]["payout"], 100)
        self.assertAlmostEqual(result["summary"]["realizedPnl"], 80)
        self.assertAlmostEqual(portfolio["summary"]["cash"], 180)
        self.assertEqual(portfolio["positions"][0]["status"], "SETTLED")

    def test_exit_preview_estimates_market_sell(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = WeatherStorage(Path(tmpdir) / "weather.db", initialize=True)
            buy = storage.execute_paper_buy(
                signal=signal(),
                market_buckets=(market_bucket(0),),
                context=context(),
                initial_cash=100,
                stake_usdc=20,
                fee_rate=0.0,
            )
            position_key = buy["position"]["positionKey"]
            preview = storage.paper_exit_preview(
                position_key=position_key,
                market_buckets=(market_bucket(0, bid=0.35, ask=0.40),),
                fee_rate=0.0,
            )

        self.assertTrue(preview["accepted"])
        self.assertAlmostEqual(preview["sellValue"], 35)
        self.assertAlmostEqual(preview["realizedPnl"], 15)

    def test_paper_mark_positions_records_latest_orderbook_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = WeatherStorage(Path(tmpdir) / "weather.db", initialize=True)
            storage.execute_paper_buy(
                signal=signal(),
                market_buckets=(market_bucket(0),),
                context=context(),
                initial_cash=100,
                stake_usdc=20,
                fee_rate=0.0,
            )
            result = storage.paper_mark_positions(
                initial_cash=100,
                market_buckets=(market_bucket(0, bid=0.35, ask=0.40),),
                fee_rate=0.0,
                target_profit=0.10,
            )
            portfolio = storage.paper_portfolio(initial_cash=100)

        self.assertEqual(result["summary"]["markCount"], 1)
        self.assertEqual(result["summary"]["warningCount"], 0)
        mark = result["marks"][0]
        self.assertAlmostEqual(mark["bestBid"], 0.35)
        self.assertAlmostEqual(mark["liquidationValue"], 35)
        self.assertAlmostEqual(mark["executablePnl"], 15)
        self.assertIn("TARGET_PROFIT", mark["exitSignal"])
        self.assertAlmostEqual(portfolio["summary"]["markValue"], 37.5)
        self.assertAlmostEqual(portfolio["positions"][0]["latestMark"]["liquidationValue"], 35)

    def test_adjacent_hedge_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = WeatherStorage(Path(tmpdir) / "weather.db", initialize=True)
            storage.execute_paper_buy(
                signal=signal(),
                market_buckets=(market_bucket(0), market_bucket(1)),
                context=context(),
                initial_cash=100,
                stake_usdc=20,
                fee_rate=0.0,
            )
            result = storage.paper_hedge_preview(
                signals=(
                    signal(probability=0.60),
                    signal(bucket=buckets()[1], probability=0.30),
                ),
                market_buckets=(market_bucket(0), market_bucket(1)),
                stake_usdc=20,
                fee_rate=0.0,
                min_adjacent_probability=0.10,
            )

        self.assertEqual(result["adjacent"]["recommendation"], "HEDGE_ADJACENT")
        self.assertEqual(result["adjacent"]["adjacentOutcome"], "83 to 86")
        self.assertGreater(result["adjacent"]["riskReduction"], 0)

    def test_tail_risk_lock_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = WeatherStorage(Path(tmpdir) / "weather.db", initialize=True)
            storage.execute_paper_buy(
                signal=signal(),
                market_buckets=(market_bucket(0), market_bucket(1), market_bucket(2)),
                context=context(),
                initial_cash=100,
                stake_usdc=20,
                fee_rate=0.0,
            )
            result = storage.paper_hedge_preview(
                signals=(
                    signal(probability=0.60),
                    signal(bucket=buckets()[1], probability=0.36),
                    signal(bucket=buckets()[2], probability=0.04),
                ),
                market_buckets=(market_bucket(0), market_bucket(1), market_bucket(2)),
                stake_usdc=20,
                fee_rate=0.0,
                tail_probability_cutoff=0.05,
                max_tail_probability=0.05,
                target_profit=0.0,
            )

        self.assertTrue(result["tailRiskLock"]["isTailRiskLock"])
        self.assertAlmostEqual(result["tailRiskLock"]["coveredProbability"], 0.96)
        self.assertAlmostEqual(result["tailRiskLock"]["uncoveredTailProbability"], 0.04)
        self.assertFalse(result["summary"]["isTrueArbitrage"])

    def test_web_api_preview_buy_portfolio_and_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "weather.db"
            payload = {
                "dbPath": str(db_path),
                "unit": "F",
                "initialCash": 100,
                "stakeUsdc": 20,
                "feeRate": 0.0,
                "marketsCsv": markets_csv(),
                "signal": signal(),
                "context": context(),
            }
            preview = web_api.paper_preview_payload(payload)
            buy = web_api.paper_buy_payload(payload)
            portfolio = web_api.paper_portfolio_payload(
                {"dbPath": str(db_path), "initialCash": 100}
            )
            storage = WeatherStorage(db_path, initialize=True)
            import_run_key = storage.save_settlement_import_run(
                city=city(),
                target_date="2026-07-05",
                kind="high",
                provider="fixture",
                station_id="KNYC",
                status="success",
            )
            storage.save_settlement(
                SettlementObservation(
                    city=city(),
                    target_date=date(2026, 7, 5),
                    kind="high",
                    observed_value=82.0,
                    unit="F",
                    source_provider="fixture",
                    source_url="https://example.test",
                    station_id="KNYC",
                    settlement_station="Central Park",
                    observation_count=24,
                    bucket_label="80 to 83",
                    bucket_key=buckets()[0].canonical_key,
                ),
                import_run_key=import_run_key,
            )
            reconciled = web_api.paper_reconcile_payload(
                {
                    "dbPath": str(db_path),
                    "initialCash": 100,
                    "cityId": "paper-city",
                    "targetDate": "2026-07-05",
                    "kind": "high",
                }
            )

        self.assertTrue(preview["preview"]["accepted"])
        self.assertTrue(buy["summary"]["accepted"])
        self.assertEqual(portfolio["summary"]["openPositionCount"], 1)
        self.assertEqual(reconciled["summary"]["settledPositionCount"], 1)
        self.assertAlmostEqual(reconciled["summary"]["realizedPnl"], 80)

    def test_web_api_mark_uses_supplied_market_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "weather.db"
            payload = {
                "dbPath": str(db_path),
                "unit": "F",
                "initialCash": 100,
                "stakeUsdc": 20,
                "feeRate": 0.0,
                "marketsCsv": markets_csv(),
                "signal": signal(),
                "context": context(),
            }
            web_api.paper_buy_payload(payload)
            marked = web_api.paper_mark_payload(
                {
                    **payload,
                    "marketsCsv": "\n".join(
                        [
                            "outcome,price,best_bid,best_ask,bid_size,ask_size,token_id",
                            "80 to 83,0.375,0.35,0.40,500,500,token-0",
                        ]
                    ),
                    "targetProfit": 0.10,
                }
            )

        self.assertEqual(marked["summary"]["markCount"], 1)
        self.assertAlmostEqual(marked["marks"][0]["bestBid"], 0.35)
        self.assertAlmostEqual(marked["portfolio"]["summary"]["liquidationValue"], 35)

    def test_web_api_mark_fetches_live_orderbook_for_open_position_tokens(self) -> None:
        class FakeGammaMarketClient:
            seen_tokens: list[str] = []

            def __init__(self, *, cache_max_age_seconds: int = 5) -> None:
                self.cache_max_age_seconds = cache_max_age_seconds

            def get_orderbook(self, token_id: str) -> OrderBookSnapshot:
                self.seen_tokens.append(token_id)
                return OrderBookSnapshot(
                    token_id=token_id,
                    bids=(OrderBookLevel(price=0.35, size=500),),
                    asks=(OrderBookLevel(price=0.40, size=500),),
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "weather.db"
            payload = {
                "dbPath": str(db_path),
                "unit": "F",
                "initialCash": 100,
                "stakeUsdc": 20,
                "feeRate": 0.0,
                "marketsCsv": markets_csv(),
                "signal": signal(),
                "context": context(),
            }
            web_api.paper_buy_payload(payload)
            with patch.object(web_api, "GammaMarketClient", FakeGammaMarketClient):
                marked = web_api.paper_mark_payload(
                    {
                        "dbPath": str(db_path),
                        "initialCash": 100,
                        "feeRate": 0.0,
                        "targetProfit": 0.10,
                    }
                )

        self.assertEqual(FakeGammaMarketClient.seen_tokens, ["token-0"])
        self.assertEqual(marked["summary"]["markCount"], 1)
        self.assertAlmostEqual(marked["marks"][0]["liquidationValue"], 35)
        self.assertIn("TARGET_PROFIT", marked["marks"][0]["exitSignal"])


if __name__ == "__main__":
    unittest.main()
