from __future__ import annotations

import unittest
from datetime import date

from weather_quant.buckets import build_regular_buckets
from weather_quant.models import CityConfig, EnsembleForecast, ForecastPoint
from weather_quant.probability import TemperatureProbabilityModel


class TemperatureProbabilityModelTest(unittest.TestCase):
    def test_distribution_normalizes_market_buckets(self) -> None:
        city = CityConfig(
            city_id="test",
            name="Test City",
            latitude=0,
            longitude=0,
            settlement_unit="F",
            model_error_std=2.0,
            min_distribution_std=1.0,
        )
        ensemble = EnsembleForecast(
            city=city,
            target_date=date(2026, 7, 3),
            kind="high",
            points=(
                ForecastPoint(
                    city_id="test",
                    target_date=date(2026, 7, 3),
                    kind="high",
                    value=80,
                    unit="F",
                    source_model="gfs",
                ),
                ForecastPoint(
                    city_id="test",
                    target_date=date(2026, 7, 3),
                    kind="high",
                    value=82,
                    unit="F",
                    source_model="ecmwf",
                ),
            ),
        )
        buckets = build_regular_buckets(start=78, end=84, unit="F")

        distribution = TemperatureProbabilityModel(
            normalize_market_buckets=True,
        ).build_distribution(ensemble, buckets)

        self.assertAlmostEqual(sum(item.probability for item in distribution.probabilities), 1.0)
        self.assertAlmostEqual(distribution.mean, 81.0)
        self.assertGreater(distribution.std, 2.0)

    def test_default_distribution_does_not_force_single_bucket_to_one(self) -> None:
        city = CityConfig(
            city_id="test",
            name="Test City",
            latitude=0,
            longitude=0,
            settlement_unit="F",
            model_error_std=2.0,
            min_distribution_std=1.0,
        )
        ensemble = EnsembleForecast(
            city=city,
            target_date=date(2026, 7, 3),
            kind="high",
            points=(
                ForecastPoint(
                    city_id="test",
                    target_date=date(2026, 7, 3),
                    kind="high",
                    value=80,
                    unit="F",
                    source_model="gfs",
                ),
            ),
        )
        buckets = build_regular_buckets(start=79, end=79, unit="F", include_tails=False)

        distribution = TemperatureProbabilityModel().build_distribution(ensemble, buckets)

        self.assertLess(distribution.probabilities[0].probability, 1.0)
        self.assertGreater(distribution.probabilities[0].probability, 0.0)


if __name__ == "__main__":
    unittest.main()
