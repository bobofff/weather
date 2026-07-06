from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from weather_quant.cache import FileCache
from weather_quant.config import load_trading_config
from weather_quant.db import init_database
from weather_quant.ensemble import (
    aggregate_member_daily_value,
    build_bucket_distribution,
    build_ensemble_run,
    ensemble_chart_data,
    ensemble_signal_rows,
)
from weather_quant.models import (
    CityConfig,
    EnsembleMemberForecast,
    MarketBucket,
    OrderBookLevel,
    OrderBookSnapshot,
    TemperatureBucket,
)
from weather_quant.storage import WeatherStorage
from weather_quant.weather import OpenMeteoEnsembleClient, OpenMeteoForecastClient


def city() -> CityConfig:
    return CityConfig(
        city_id="test-city",
        name="Test City",
        latitude=40.0,
        longitude=-74.0,
        timezone="America/New_York",
        settlement_station="Central Park",
        station_id="KNYC",
        metar_source="NOAA/METAR",
        forecast_granularity="station",
        settlement_unit="F",
    )


def member(member_id: str, values: tuple[float, ...]) -> EnsembleMemberForecast:
    return EnsembleMemberForecast(
        provider="fixture",
        model="fixture_ens",
        member_id=member_id,
        hourly_times=(
            "2026-07-05T00:00",
            "2026-07-05T12:00",
            "2026-07-05T23:00",
        ),
        hourly_values=values,
        unit="F",
        timezone="America/New_York",
    )


def buckets() -> tuple[TemperatureBucket, ...]:
    return (
        TemperatureBucket(label="80 to 83", lower=80, upper=83, unit="F"),
        TemperatureBucket(label="83 to 86", lower=83, upper=86, unit="F"),
    )


def market_buckets() -> tuple[MarketBucket, ...]:
    return (
        MarketBucket(
            market_id="m1",
            question="test",
            slug="test",
            condition_id="0x1",
            outcome="80 to 83",
            price=0.19,
            bucket=buckets()[0],
            token_id="token-1",
            orderbook=OrderBookSnapshot(
                token_id="token-1",
                bids=(OrderBookLevel(price=0.18, size=100),),
                asks=(OrderBookLevel(price=0.20, size=100),),
            ),
        ),
        MarketBucket(
            market_id="m2",
            question="test",
            slug="test",
            condition_id="0x1",
            outcome="83 to 86",
            price=0.31,
            bucket=buckets()[1],
            token_id="token-2",
            orderbook=OrderBookSnapshot(
                token_id="token-2",
                bids=(OrderBookLevel(price=0.29, size=100),),
                asks=(OrderBookLevel(price=0.33, size=100),),
            ),
        ),
    )


class EnsembleProbabilityTest(unittest.TestCase):
    def test_hourly_members_aggregate_to_daily_high_and_low(self) -> None:
        high = aggregate_member_daily_value(
            member("m1", (80.0, 82.0, 81.0)),
            target_date=date(2026, 7, 5),
            kind="high",
            timezone_name="America/New_York",
            unit="F",
        )
        low = aggregate_member_daily_value(
            member("m1", (80.0, 77.0, 81.0)),
            target_date=date(2026, 7, 5),
            kind="low",
            timezone_name="America/New_York",
            unit="F",
        )

        self.assertIsNotNone(high)
        self.assertIsNotNone(low)
        self.assertAlmostEqual(high.value, 82.0)  # type: ignore[union-attr]
        self.assertAlmostEqual(low.value, 77.0)  # type: ignore[union-attr]

    def test_timezone_conversion_is_used_for_target_date(self) -> None:
        forecast = EnsembleMemberForecast(
            provider="fixture",
            model="fixture_ens",
            member_id="m1",
            hourly_times=("2026-07-05T02:00:00+00:00", "2026-07-05T16:00:00+00:00"),
            hourly_values=(70.0, 90.0),
            unit="F",
            timezone="UTC",
        )

        daily = aggregate_member_daily_value(
            forecast,
            target_date=date(2026, 7, 5),
            kind="high",
            timezone_name="America/New_York",
            unit="F",
        )

        self.assertIsNotNone(daily)
        self.assertAlmostEqual(daily.value, 90.0)  # type: ignore[union-attr]

    def test_member_hits_build_bucket_probability_and_stats(self) -> None:
        run = build_ensemble_run(
            provider="fixture",
            model="fixture_ens",
            city=city(),
            target_date=date(2026, 7, 5),
            kind="high",
            members=(
                member("m1", (80.0, 82.0, 81.0)),
                member("m2", (84.0, 85.0, 84.5)),
                member("m3", (90.0, 89.0, 88.0)),
            ),
        )

        distribution = build_bucket_distribution(run, buckets())

        self.assertEqual(distribution.total_members, 3)
        self.assertEqual(distribution.unmatched_count, 1)
        self.assertEqual(distribution.probabilities[0].hit_count, 1)
        self.assertEqual(distribution.probabilities[1].hit_count, 1)
        self.assertAlmostEqual(distribution.probabilities[0].probability, 1 / 3)
        self.assertAlmostEqual(distribution.probabilities[1].probability, 1 / 3)
        self.assertAlmostEqual(distribution.empirical_mean or 0.0, 85.6666666667)
        self.assertAlmostEqual(distribution.p10 or 0.0, 82.6)
        self.assertAlmostEqual(distribution.p50 or 0.0, 85.0)
        self.assertAlmostEqual(distribution.p90 or 0.0, 89.0)

    def test_chart_data_and_signal_edge(self) -> None:
        run = build_ensemble_run(
            provider="fixture",
            model="fixture_ens",
            city=city(),
            target_date=date(2026, 7, 5),
            kind="high",
            members=(
                member("m1", (80.0, 82.0, 81.0)),
                member("m2", (84.0, 85.0, 84.5)),
                member("m3", (90.0, 89.0, 88.0)),
            ),
        )
        distribution = build_bucket_distribution(run, buckets())

        chart = ensemble_chart_data(distribution, market_buckets=market_buckets())
        rows = ensemble_signal_rows(
            distribution,
            market_buckets(),
            fee_rate=0.05,
            min_edge=0.03,
        )

        self.assertEqual(chart["bucketLabels"], ["80 to 83", "83 to 86"])
        self.assertEqual(len(chart["cdfPoints"]), 3)
        self.assertAlmostEqual(chart["marketImpliedProbabilities"][0], 0.19)
        self.assertAlmostEqual(rows[0]["ensembleProbability"], 1 / 3)
        self.assertAlmostEqual(rows[0]["executableEntryCost"], 0.20)
        self.assertAlmostEqual(rows[0]["fee"], 0.008)
        self.assertAlmostEqual(rows[0]["expectedExitCost"], 0.01)
        self.assertAlmostEqual(rows[0]["marketImpliedProbability"], 0.19)
        self.assertAlmostEqual(rows[0]["rawEdge"], 1 / 3 - 0.19)
        self.assertAlmostEqual(rows[0]["spread"], 0.02)
        self.assertEqual(rows[0]["bestAskSize"], 100)
        self.assertEqual(rows[0]["askDepth"], 100)
        self.assertAlmostEqual(rows[0]["edge"], 1 / 3 - 0.20 - 0.008 - 0.01)
        self.assertGreater(rows[0]["signalScore"], 75)
        self.assertEqual(rows[0]["recommendation"], "BUY_YES")
        self.assertEqual(rows[0]["reason"], "executable edge clears threshold")

    def test_sqlite_init_comments_and_storage_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "weather.db"
            init_database(db_path)
            with sqlite3.connect(db_path) as connection:
                comment = connection.execute(
                    """
                    SELECT comment FROM schema_comments
                    WHERE object_type = 'table' AND object_name = 'weather_ensemble_runs'
                    """
                ).fetchone()[0]
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
            self.assertIn("集合预报", comment)
            self.assertEqual(missing_comments, [])

            run = build_ensemble_run(
                provider="fixture",
                model="fixture_ens",
                city=city(),
                target_date=date(2026, 7, 5),
                kind="high",
                members=(
                    member("m1", (80.0, 82.0, 81.0)),
                    member("m2", (84.0, 85.0, 84.5)),
                ),
            )
            distribution = build_bucket_distribution(run, buckets())
            storage = WeatherStorage(db_path, initialize=True)
            storage.save_distribution(distribution)
            group = storage.save_market_snapshots(market_buckets())
            storage.save_signal_snapshots(
                run_key=run.run_key,
                rows=ensemble_signal_rows(distribution, market_buckets(), fee_rate=0.05),
                market_snapshot_group=group,
            )

            runs = storage.recent_runs(limit=5)
            probabilities = storage.recent_probabilities(limit=5)
            with sqlite3.connect(db_path) as connection:
                member_count = connection.execute(
                    "SELECT COUNT(*) FROM weather_ensemble_members"
                ).fetchone()[0]
                signal_count = connection.execute(
                    "SELECT COUNT(*) FROM weather_signal_snapshots"
                ).fetchone()[0]

            self.assertEqual(runs[0]["station_id"], "KNYC")
            self.assertEqual(runs[0]["settlement_station"], "Central Park")
            self.assertEqual(member_count, 2)
            self.assertEqual(signal_count, 2)
            self.assertTrue(probabilities)

    def test_open_meteo_ensemble_client_parses_mock_payload_without_network(self) -> None:
        class FakeHttp:
            def get_json(self, path, *, params=None, headers=None):  # noqa: ANN001, ANN003
                self.path = path
                self.params = params
                return {
                    "timezone": "America/New_York",
                    "hourly_units": {
                        "temperature_2m_member01": "°F",
                        "temperature_2m_member02": "°F",
                    },
                    "hourly": {
                        "time": ["2026-07-05T00:00", "2026-07-05T12:00"],
                        "temperature_2m_member01": [80.0, 83.0],
                        "temperature_2m_member02": [78.0, 81.0],
                    },
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_http = FakeHttp()
            client = OpenMeteoEnsembleClient(
                http_client=fake_http,  # type: ignore[arg-type]
                cache=FileCache(Path(tmpdir) / "cache"),
            )
            run = client.fetch_run(
                city(),
                target_date=date(2026, 7, 5),
                kind="high",
                model="ecmwf_ifs025",
            )

        self.assertEqual(run.member_count, 2)
        self.assertEqual(run.members[0].member_id, "member01")
        self.assertAlmostEqual(run.daily_values[0].value, 83.0)
        self.assertEqual(fake_http.path, "/v1/ensemble")
        self.assertEqual(fake_http.params["models"], "ecmwf_ifs025")

    def test_open_meteo_clients_forward_location_options(self) -> None:
        class FakeForecastHttp:
            params = None

            def get_json(self, path, *, params=None, headers=None):  # noqa: ANN001, ANN003
                self.path = path
                self.params = params
                return {
                    "daily_units": {"temperature_2m_max": "°C"},
                    "daily": {
                        "time": ["2026-07-05"],
                        "temperature_2m_max": [24.0],
                        "temperature_2m_min": [18.0],
                    },
                }

        custom_city = CityConfig(
            city_id="custom",
            name="Custom",
            latitude=48.3538,
            longitude=11.7861,
            timezone="Europe/Berlin",
            settlement_unit="C",
            elevation=453.0,
            cell_selection="nearest",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_http = FakeForecastHttp()
            client = OpenMeteoForecastClient(
                http_client=fake_http,  # type: ignore[arg-type]
                cache=FileCache(Path(tmpdir) / "cache"),
            )
            client.fetch_point(
                custom_city,
                target_date=date(2026, 7, 5),
                kind="high",
                model="icon_seamless",
            )

        self.assertEqual(fake_http.path, "/v1/forecast")
        self.assertEqual(fake_http.params["elevation"], 453.0)
        self.assertEqual(fake_http.params["cell_selection"], "nearest")
        self.assertEqual(fake_http.params["temperature_unit"], "celsius")

    def test_open_meteo_ensemble_client_forwards_location_options(self) -> None:
        class FakeEnsembleHttp:
            params = None

            def get_json(self, path, *, params=None, headers=None):  # noqa: ANN001, ANN003
                self.path = path
                self.params = params
                return {
                    "timezone": "Europe/Berlin",
                    "hourly_units": {"temperature_2m_member01": "°C"},
                    "hourly": {
                        "time": ["2026-07-05T00:00", "2026-07-05T12:00"],
                        "temperature_2m_member01": [18.0, 24.0],
                    },
                }

        custom_city = CityConfig(
            city_id="custom",
            name="Custom",
            latitude=48.3538,
            longitude=11.7861,
            timezone="Europe/Berlin",
            settlement_unit="C",
            elevation=453.0,
            cell_selection="nearest",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_http = FakeEnsembleHttp()
            client = OpenMeteoEnsembleClient(
                http_client=fake_http,  # type: ignore[arg-type]
                cache=FileCache(Path(tmpdir) / "cache"),
            )
            client.fetch_run(
                custom_city,
                target_date=date(2026, 7, 5),
                kind="high",
                model="ecmwf_ifs025",
            )

        self.assertEqual(fake_http.path, "/v1/ensemble")
        self.assertEqual(fake_http.params["elevation"], 453.0)
        self.assertEqual(fake_http.params["cell_selection"], "nearest")
        self.assertEqual(fake_http.params["temperature_unit"], "celsius")

    def test_open_meteo_ensemble_client_uses_ensemble_api_host_by_default(self) -> None:
        client = OpenMeteoEnsembleClient()

        self.assertEqual(client.base_url, "https://ensemble-api.open-meteo.com")

    def test_config_keeps_forecast_and_ensemble_base_urls_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            path.write_text(
                """
                {
                  "open_meteo_base_url": "https://forecast.example.test",
                  "open_meteo_ensemble_base_url": "https://ensemble.example.test"
                }
                """,
                encoding="utf-8",
            )

            config = load_trading_config(path)

        self.assertEqual(config.open_meteo_base_url, "https://forecast.example.test")
        self.assertEqual(
            config.open_meteo_ensemble_base_url,
            "https://ensemble.example.test",
        )


if __name__ == "__main__":
    unittest.main()
