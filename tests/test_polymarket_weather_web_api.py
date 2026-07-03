from __future__ import annotations

import unittest
from datetime import date

from weather_quant import web_api
from weather_quant.buckets import parse_temperature_bucket
from weather_quant.models import (
    EnsembleForecast,
    ForecastPoint,
    MarketBucket,
    OrderBookLevel,
    OrderBookSnapshot,
)


def live_bucket() -> MarketBucket:
    return MarketBucket(
        market_id="live",
        question="Live market",
        slug="nyc-live",
        condition_id="0xlive",
        outcome="80",
        price=0.50,
        bucket=parse_temperature_bucket("80"),
        token_id="token-80",
        orderbook=OrderBookSnapshot(
            token_id="token-80",
            bids=(OrderBookLevel(price=0.42, size=100),),
            asks=(OrderBookLevel(price=0.58, size=100),),
        ),
    )


class WebApiPortfolioPayloadTest(unittest.TestCase):
    def test_market_payload_returns_live_orderbook_without_csv(self) -> None:
        class FakeGammaMarketClient:
            kwargs = None

            def get_market_buckets(self, **kwargs):  # noqa: ANN003
                type(self).kwargs = kwargs
                return (live_bucket(),)

        original_client = web_api.GammaMarketClient
        web_api.GammaMarketClient = FakeGammaMarketClient  # type: ignore[assignment]
        try:
            result = web_api.market_payload(
                {
                    "marketSlug": "nyc-live",
                    "unit": "F",
                    "includeOrderbooks": True,
                }
            )
        finally:
            web_api.GammaMarketClient = original_client

        self.assertEqual(result["summary"]["marketSource"], "polymarket")
        self.assertEqual(result["summary"]["marketCount"], 1)
        self.assertEqual(result["buckets"][0]["outcome"], "80")
        self.assertAlmostEqual(result["buckets"][0]["bestBid"], 0.42)
        self.assertAlmostEqual(result["buckets"][0]["bestAsk"], 0.58)
        self.assertEqual(result["buckets"][0]["orderbook"]["bids"][0]["size"], 100)
        self.assertEqual(FakeGammaMarketClient.kwargs["slug"], "nyc-live")
        self.assertTrue(FakeGammaMarketClient.kwargs["include_orderbooks"])

    def test_market_payload_auto_discovers_from_event_query(self) -> None:
        class FakeGammaMarketClient:
            kwargs = None

            def discover_weather_market_buckets(self, **kwargs):  # noqa: ANN003
                type(self).kwargs = kwargs
                return (live_bucket(),)

        original_client = web_api.GammaMarketClient
        web_api.GammaMarketClient = FakeGammaMarketClient  # type: ignore[assignment]
        try:
            result = web_api.market_payload(
                {
                    "marketQuery": "shanghai",
                    "unit": "F",
                    "temperatureKind": "high",
                    "targetDate": "2026-07-03",
                    "includeOrderbooks": True,
                }
            )
        finally:
            web_api.GammaMarketClient = original_client

        self.assertEqual(result["summary"]["marketSource"], "polymarket")
        self.assertEqual(result["summary"]["selector"]["method"], "events_keyset")
        self.assertEqual(FakeGammaMarketClient.kwargs["query"], "shanghai")
        self.assertEqual(FakeGammaMarketClient.kwargs["kind"], "high")
        self.assertEqual(FakeGammaMarketClient.kwargs["target_date"], "2026-07-03")
        self.assertTrue(FakeGammaMarketClient.kwargs["include_orderbooks"])

    def test_market_payload_requires_selector(self) -> None:
        with self.assertRaisesRegex(ValueError, "Provide a city/search term"):
            web_api.market_payload({"unit": "F"})

    def test_live_polymarket_market_selector_loads_orderbook_buckets(self) -> None:
        class FakeGammaMarketClient:
            kwargs = None

            def get_market_buckets(self, **kwargs):  # noqa: ANN003
                type(self).kwargs = kwargs
                return (live_bucket(),)

        original_client = web_api.GammaMarketClient
        web_api.GammaMarketClient = FakeGammaMarketClient  # type: ignore[assignment]
        try:
            result = web_api.portfolio_payload(
                {
                    "positionsCsv": "outcome,shares,total_cost\n80,10,3\n",
                    "marketSlug": "nyc-live",
                    "unit": "F",
                    "feeRate": 0,
                }
            )
        finally:
            web_api.GammaMarketClient = original_client

        self.assertEqual(result["summary"]["marketSource"], "polymarket")
        self.assertEqual(result["summary"]["marketCount"], 1)
        self.assertAlmostEqual(result["valuations"][0]["bestBid"], 0.42)
        self.assertAlmostEqual(result["valuations"][0]["bestAsk"], 0.58)
        self.assertAlmostEqual(result["scenarios"][0]["probability"], 0.50)
        self.assertEqual(FakeGammaMarketClient.kwargs["slug"], "nyc-live")
        self.assertTrue(FakeGammaMarketClient.kwargs["include_orderbooks"])

    def test_inline_market_csv_takes_precedence_over_live_selector(self) -> None:
        class FailingGammaMarketClient:
            def get_market_buckets(self, **kwargs):  # noqa: ANN003
                raise AssertionError("live client should not be called when CSV is present")

        original_client = web_api.GammaMarketClient
        web_api.GammaMarketClient = FailingGammaMarketClient  # type: ignore[assignment]
        try:
            result = web_api.portfolio_payload(
                {
                    "positionsCsv": "outcome,shares,total_cost\n80,10,3\n",
                    "marketsCsv": "outcome,price,best_bid,best_ask\n80,0.40,0.35,0.45\n",
                    "marketSlug": "nyc-live",
                    "unit": "F",
                    "feeRate": 0,
                }
            )
        finally:
            web_api.GammaMarketClient = original_client

        self.assertEqual(result["summary"]["marketSource"], "csv")
        self.assertAlmostEqual(result["valuations"][0]["bestBid"], 0.35)

    def test_forecast_payload_fetches_selected_open_meteo_models(self) -> None:
        class FakeWeatherEnsembleProvider:
            kwargs = None

            def fetch_ensemble(self, city, **kwargs):  # noqa: ANN003
                type(self).kwargs = {"city": city, **kwargs}
                target_date = kwargs["target_date"]
                kind = kwargs["kind"]
                return EnsembleForecast(
                    city=city,
                    target_date=target_date,
                    kind=kind,
                    points=(
                        ForecastPoint(
                            city_id=city.city_id,
                            target_date=target_date,
                            kind=kind,
                            value=21.5,
                            unit=city.settlement_unit,
                            source_model="ecmwf_ifs025",
                            raw_payload={"provider": "open-meteo"},
                        ),
                    ),
                )

        original_provider = web_api.WeatherEnsembleProvider
        web_api.WeatherEnsembleProvider = FakeWeatherEnsembleProvider  # type: ignore[assignment]
        try:
            result = web_api.forecast_payload(
                {
                    "city": "Munich",
                    "targetDate": "2026-07-03",
                    "temperatureKind": "high",
                    "models": ["ecmwf_ifs025"],
                }
            )
        finally:
            web_api.WeatherEnsembleProvider = original_provider

        self.assertEqual(result["summary"]["cityId"], "munich")
        self.assertEqual(result["summary"]["unit"], "C")
        self.assertEqual(result["summary"]["modelCount"], 1)
        self.assertAlmostEqual(result["summary"]["mean"], 21.5)
        self.assertEqual(result["points"][0]["provider"], "open-meteo")
        self.assertEqual(FakeWeatherEnsembleProvider.kwargs["city"].city_id, "munich")
        self.assertEqual(FakeWeatherEnsembleProvider.kwargs["target_date"], date(2026, 7, 3))
        self.assertEqual(FakeWeatherEnsembleProvider.kwargs["kind"], "high")
        self.assertEqual(FakeWeatherEnsembleProvider.kwargs["models"], ("ecmwf_ifs025",))


if __name__ == "__main__":
    unittest.main()
