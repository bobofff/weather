from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from weather_quant.cache import FileCache
from weather_quant.ensemble import (
    build_bucket_distribution,
    build_ensemble_run,
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
from weather_quant.settlement import (
    AviationWeatherMetarSettlementClient,
    SettlementObservation,
)
from weather_quant.storage import WeatherStorage
from weather_quant import web_api


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
            price=0.40,
            bucket=buckets()[0],
            token_id="token-1",
            orderbook=OrderBookSnapshot(
                token_id="token-1",
                bids=(OrderBookLevel(price=0.39, size=100),),
                asks=(OrderBookLevel(price=0.41, size=100),),
            ),
        ),
        MarketBucket(
            market_id="m2",
            question="test",
            slug="test",
            condition_id="0x2",
            outcome="83 to 86",
            price=0.45,
            bucket=buckets()[1],
            token_id="token-2",
            orderbook=OrderBookSnapshot(
                token_id="token-2",
                bids=(OrderBookLevel(price=0.44, size=100),),
                asks=(OrderBookLevel(price=0.46, size=100),),
            ),
        ),
    )


class SettlementImportTest(unittest.TestCase):
    def test_aviation_weather_client_reduces_metar_temperatures(self) -> None:
        class FakeHttp:
            def get_json(self, path, *, params=None, headers=None):  # noqa: ANN001, ANN003
                self.path = path
                self.params = params
                return [
                    {"obsTime": "2026-07-05T12:00:00Z", "temp": 27.0},
                    {"obsTime": "2026-07-05T20:00:00Z", "temp": 29.0},
                    {"obsTime": "2026-07-06T03:00:00Z", "temp": 18.0},
                ]

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_http = FakeHttp()
            client = AviationWeatherMetarSettlementClient(
                http_client=fake_http,  # type: ignore[arg-type]
                cache=FileCache(Path(tmpdir) / "cache"),
            )
            observation = client.fetch_observation(
                city(),
                target_date=date(2026, 7, 5),
                kind="high",
                buckets=buckets(),
            )

        self.assertEqual(fake_http.path, "/api/data/metar")
        self.assertEqual(fake_http.params["ids"], "KNYC")
        self.assertAlmostEqual(observation.observed_value, 84.2)
        self.assertEqual(observation.unit, "F")
        self.assertEqual(observation.bucket_label, "83 to 86")
        self.assertEqual(observation.observation_count, 3)

    def test_storage_reconciles_signal_outcomes_from_imported_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "weather.db"
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
                    observed_value=84.0,
                    unit="F",
                    source_provider="fixture",
                    source_url="https://example.test",
                    station_id="KNYC",
                    settlement_station="Central Park",
                    observation_count=24,
                    observation_start=datetime(2026, 7, 5, tzinfo=timezone.utc),
                    observation_end=datetime(2026, 7, 6, tzinfo=timezone.utc),
                    bucket_label="83 to 86",
                    bucket_key=buckets()[1].canonical_key,
                ),
                import_run_key=import_run_key,
            )

            outcomes = storage.reconcile_signal_outcomes(
                city_id="test-city",
                target_date="2026-07-05",
                kind="high",
            )
            calibration = storage.calibration_summary()

        self.assertEqual(len(outcomes), 2)
        winners = [row for row in outcomes if row["won"]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["outcome"], "83 to 86")
        self.assertEqual(calibration["summary"]["outcomeCount"], 2)
        self.assertAlmostEqual(calibration["summary"]["hitRate"], 0.5)

    def test_web_api_imports_settlement_without_csv(self) -> None:
        class FakeImporter:
            def import_observation(self, city_config, *, target_date, kind, buckets=()):  # noqa: ANN001, ANN003
                return SettlementObservation(
                    city=city_config,
                    target_date=target_date,
                    kind=kind,
                    observed_value=84.0,
                    unit=city_config.settlement_unit,
                    source_provider="fixture",
                    source_url="https://example.test",
                    station_id=city_config.station_id,
                    settlement_station=city_config.settlement_station,
                    observation_count=24,
                    bucket_label=None,
                    bucket_key=None,
                )

        original_importer = web_api.SettlementImporter
        web_api.SettlementImporter = lambda: FakeImporter()  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                result = web_api.settlement_import_payload(
                    {
                        "city": "new-york",
                        "targetDate": "2026-07-05",
                        "temperatureKind": "high",
                        "includeMarketBuckets": False,
                        "dbPath": str(Path(tmpdir) / "weather.db"),
                    }
                )
        finally:
            web_api.SettlementImporter = original_importer

        self.assertEqual(result["summary"]["status"], "success")
        self.assertEqual(result["settlement"]["sourceProvider"], "fixture")
        self.assertEqual(result["settlement"]["observedValue"], 84.0)
        self.assertEqual(result["summary"]["outcomeCount"], 0)


if __name__ == "__main__":
    unittest.main()
