from __future__ import annotations

import unittest
import tempfile
from datetime import date
from pathlib import Path

from weather_quant import web_api
from weather_quant.buckets import parse_temperature_bucket
from weather_quant.models import (
    EnsembleForecast,
    ForecastPoint,
    MarketBucket,
    OrderBookLevel,
    OrderBookSnapshot,
)
from weather_quant.station_lookup import StationLookupClient


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

    def test_market_payload_city_selector_uses_location_name(self) -> None:
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
                    "city": "new-york",
                    "locationName": "New York",
                    "unit": "F",
                    "timezone": "America/New_York",
                    "temperatureKind": "high",
                    "targetDate": "2026-07-03",
                }
            )
        finally:
            web_api.GammaMarketClient = original_client

        self.assertEqual(result["summary"]["marketSource"], "polymarket")
        self.assertEqual(result["summary"]["timezone"], "America/New_York")
        self.assertEqual(FakeGammaMarketClient.kwargs["query"], "New York")
        self.assertEqual(FakeGammaMarketClient.kwargs["kind"], "high")
        self.assertEqual(FakeGammaMarketClient.kwargs["target_date"], "2026-07-03")

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

    def test_forecast_payload_returns_provider_warnings(self) -> None:
        class FakeWeatherEnsembleProvider:
            def fetch_ensemble(self, city, **kwargs):  # noqa: ANN003
                target_date = kwargs["target_date"]
                kind = kwargs["kind"]
                return EnsembleForecast(
                    city=city,
                    target_date=target_date,
                    kind=kind,
                    provider_warnings=("ecmwf_ifs025: temporary upstream failure",),
                    points=(
                        ForecastPoint(
                            city_id=city.city_id,
                            target_date=target_date,
                            kind=kind,
                            value=22.0,
                            unit=city.settlement_unit,
                            source_model="icon_seamless",
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
                    "models": ["ecmwf_ifs025", "icon_seamless"],
                }
            )
        finally:
            web_api.WeatherEnsembleProvider = original_provider

        self.assertEqual(result["summary"]["modelCount"], 1)
        self.assertEqual(result["summary"]["failedModelCount"], 1)
        self.assertEqual(
            result["summary"]["warnings"],
            ["ecmwf_ifs025: temporary upstream failure"],
        )

    def test_forecast_payload_accepts_custom_coordinates(self) -> None:
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
                            value=18.25,
                            unit=city.settlement_unit,
                            source_model="icon_seamless",
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
                    "latitude": "48.3538",
                    "longitude": "11.7861",
                    "locationName": "Munich Airport",
                    "targetDate": "2026-07-03",
                    "temperatureKind": "low",
                    "unit": "C",
                    "timezone": "Europe/Berlin",
                    "settlementStation": "Munich Airport station",
                    "stationId": "EDDM",
                    "forecastGranularity": "station",
                    "elevation": "453",
                    "cellSelection": "nearest",
                    "models": ["icon_seamless"],
                }
            )
        finally:
            web_api.WeatherEnsembleProvider = original_provider

        city = FakeWeatherEnsembleProvider.kwargs["city"]
        self.assertEqual(city.name, "Munich Airport")
        self.assertEqual(city.city_id, "munich-48p3538-11p7861")
        self.assertAlmostEqual(city.latitude, 48.3538)
        self.assertAlmostEqual(city.longitude, 11.7861)
        self.assertEqual(city.timezone, "Europe/Berlin")
        self.assertEqual(city.settlement_station, "Munich Airport station")
        self.assertEqual(city.station_id, "EDDM")
        self.assertEqual(city.forecast_granularity, "station")
        self.assertEqual(city.settlement_unit, "C")
        self.assertAlmostEqual(city.elevation, 453.0)
        self.assertEqual(city.cell_selection, "nearest")
        self.assertEqual(result["summary"]["cityName"], "Munich Airport")
        self.assertAlmostEqual(result["summary"]["latitude"], 48.3538)
        self.assertAlmostEqual(result["summary"]["longitude"], 11.7861)
        self.assertEqual(FakeWeatherEnsembleProvider.kwargs["models"], ("icon_seamless",))

    def test_custom_coordinates_require_latitude_and_longitude(self) -> None:
        with self.assertRaisesRegex(ValueError, "both latitude and longitude"):
            web_api.forecast_payload(
                {
                    "latitude": "48.3538",
                    "targetDate": "2026-07-03",
                    "temperatureKind": "high",
                }
            )

    def test_city_list_seeds_default_city_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = web_api.city_list_payload({"dbPath": str(Path(tmpdir) / "weather.db")})

        city_ids = {city["cityId"] for city in result["cities"]}
        self.assertIn("munich", city_ids)
        self.assertIn("ankara", city_ids)

    def test_saved_city_can_be_used_by_forecast_payload(self) -> None:
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
                            value=29.0,
                            unit=city.settlement_unit,
                            source_model="gfs_seamless",
                            raw_payload={"provider": "open-meteo"},
                        ),
                    ),
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "weather.db")
            web_api.city_save_payload(
                {
                    "dbPath": db_path,
                    "cityId": "airport-test",
                    "name": "Airport Test",
                    "latitude": 35.0,
                    "longitude": 139.0,
                    "timezone": "Asia/Tokyo",
                    "settlementUnit": "C",
                    "forecastGranularity": "station",
                    "elevation": 12,
                    "cellSelection": "nearest",
                    "models": ["gfs_seamless"],
                }
            )

            original_provider = web_api.WeatherEnsembleProvider
            web_api.WeatherEnsembleProvider = FakeWeatherEnsembleProvider  # type: ignore[assignment]
            try:
                result = web_api.forecast_payload(
                    {
                        "dbPath": db_path,
                        "city": "airport-test",
                        "useStoredCity": True,
                        "targetDate": "2026-07-03",
                        "temperatureKind": "high",
                    }
                )
            finally:
                web_api.WeatherEnsembleProvider = original_provider

        city = FakeWeatherEnsembleProvider.kwargs["city"]
        self.assertEqual(city.city_id, "airport-test")
        self.assertEqual(city.name, "Airport Test")
        self.assertAlmostEqual(city.latitude, 35.0)
        self.assertEqual(city.timezone, "Asia/Tokyo")
        self.assertEqual(city.weather_models, ("gfs_seamless",))
        self.assertEqual(result["summary"]["cityId"], "airport-test")

    def test_city_save_uses_editing_city_id_as_update_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "weather.db")
            result = web_api.city_save_payload(
                {
                    "dbPath": db_path,
                    "editingCityId": "ankara",
                    "cityId": "munich",
                    "name": "Ankara Edited",
                    "latitude": 40.1281,
                    "longitude": 32.9951,
                    "timezone": "Europe/Istanbul",
                    "settlementUnit": "C",
                    "settlementStation": "Esenboga Intl Airport Station",
                    "stationId": "LTAC",
                    "forecastGranularity": "station",
                    "elevation": 953,
                    "cellSelection": "nearest",
                    "models": ["ecmwf_ifs025"],
                }
            )

            storage = web_api._city_storage({"dbPath": db_path})
            ankara = storage.get_city("ankara")
            munich = storage.get_city("munich")

        self.assertEqual(result["city"]["cityId"], "ankara")
        self.assertIsNotNone(ankara)
        self.assertIsNotNone(munich)
        self.assertEqual(ankara.name, "Ankara Edited")
        self.assertEqual(ankara.station_id, "LTAC")
        self.assertNotEqual(munich.name, "Ankara Edited")

    def test_station_lookup_payload_returns_first_match(self) -> None:
        class FakeStationLookupClient:
            kwargs = None

            def lookup(self, **kwargs):  # noqa: ANN003
                type(self).kwargs = kwargs
                return (
                    {
                        "name": "Munich Airport",
                        "stationId": "EDDM",
                        "latitude": 48.3538,
                        "longitude": 11.7861,
                        "timezone": "Europe/Berlin",
                        "elevation": 453.0,
                        "source": "aviationweather",
                    },
                )

        original_client = web_api.StationLookupClient
        web_api.StationLookupClient = FakeStationLookupClient  # type: ignore[assignment]
        try:
            result = web_api.station_lookup_payload(
                {"settlementStation": "Munich Airport", "stationId": "EDDM"}
            )
        finally:
            web_api.StationLookupClient = original_client

        self.assertEqual(result["station"]["stationId"], "EDDM")
        self.assertAlmostEqual(result["station"]["latitude"], 48.3538)
        self.assertEqual(result["matches"][0]["source"], "aviationweather")
        self.assertEqual(FakeStationLookupClient.kwargs["settlement_station"], "Munich Airport")
        self.assertEqual(FakeStationLookupClient.kwargs["station_id"], "EDDM")

    def test_station_lookup_client_prefers_icao_station_info(self) -> None:
        class NullCache:
            def get(self, key, *, max_age_seconds=None):  # noqa: ANN001, ANN003
                return None

            def set(self, key, value):  # noqa: ANN001, ANN003
                return None

        class FakeHttpClient:
            def get_json(self, url, *, params=None, headers=None):  # noqa: ANN001, ANN003
                if "aviationweather" in url:
                    return [
                        {
                            "icaoId": "EDDM",
                            "site": "Munich Airport",
                            "lat": 48.3538,
                            "lon": 11.7861,
                            "elev": 453,
                            "country": "DE",
                        }
                    ]
                return {"results": []}

        client = StationLookupClient(http_client=FakeHttpClient(), cache=NullCache())
        matches = client.lookup(settlement_station="Munich Airport EDDM", limit=5)

        self.assertEqual(matches[0]["stationId"], "EDDM")
        self.assertEqual(matches[0]["name"], "Munich Airport")
        self.assertAlmostEqual(matches[0]["latitude"], 48.3538)
        self.assertAlmostEqual(matches[0]["longitude"], 11.7861)
        self.assertEqual(matches[0]["source"], "aviationweather")

    def test_station_lookup_client_falls_back_to_open_meteo(self) -> None:
        class NullCache:
            def get(self, key, *, max_age_seconds=None):  # noqa: ANN001, ANN003
                return None

            def set(self, key, value):  # noqa: ANN001, ANN003
                return None

        class FakeHttpClient:
            def get_json(self, url, *, params=None, headers=None):  # noqa: ANN001, ANN003
                if "aviationweather" in url:
                    raise RuntimeError("stationinfo unavailable")
                return {
                    "results": [
                        {
                            "name": "Munich Airport",
                            "latitude": 48.3538,
                            "longitude": 11.7861,
                            "elevation": 453,
                            "timezone": "Europe/Berlin",
                            "country_code": "DE",
                        }
                    ]
                }

        client = StationLookupClient(http_client=FakeHttpClient(), cache=NullCache())
        matches = client.lookup(settlement_station="Munich Airport EDDM", limit=5)

        self.assertEqual(matches[0]["name"], "Munich Airport")
        self.assertAlmostEqual(matches[0]["latitude"], 48.3538)
        self.assertEqual(matches[0]["timezone"], "Europe/Berlin")
        self.assertEqual(matches[0]["source"], "open-meteo-geocoding")

    def test_station_lookup_does_not_treat_intl_as_icao(self) -> None:
        class NullCache:
            def get(self, key, *, max_age_seconds=None):  # noqa: ANN001, ANN003
                return None

            def set(self, key, value):  # noqa: ANN001, ANN003
                return None

        class FakeHttpClient:
            station_ids: list[str] = []
            geocoding_queries: list[str] = []

            def get_json(self, url, *, params=None, headers=None):  # noqa: ANN001, ANN003
                if "aviationweather" in url:
                    type(self).station_ids.append(params["ids"])
                    return []
                type(self).geocoding_queries.append(params["name"])
                return {
                    "results": [
                        {
                            "name": "Esenboga",
                            "latitude": 40.1281,
                            "longitude": 32.9951,
                            "elevation": 953,
                            "timezone": "Europe/Istanbul",
                            "country_code": "TR",
                        }
                    ]
                }

        client = StationLookupClient(http_client=FakeHttpClient(), cache=NullCache())
        matches = client.lookup(settlement_station="Esenboğa Intl Airport Station", limit=5)

        self.assertEqual(FakeHttpClient.station_ids, ["LTAC"])
        self.assertNotIn("INTL", FakeHttpClient.station_ids)
        self.assertIn("Esenboga", FakeHttpClient.geocoding_queries)
        self.assertAlmostEqual(matches[0]["latitude"], 40.1281)
        self.assertEqual(matches[0]["stationId"], "LTAC")

    def test_ensemble_defaults_to_ecmwf_aifs(self) -> None:
        self.assertEqual(web_api._model_from_payload({}), "ecmwf_aifs025")

    def test_optional_market_buckets_can_use_city_selector_for_signal(self) -> None:
        class FakeGammaMarketClient:
            kwargs = None

            def discover_weather_market_buckets(self, **kwargs):  # noqa: ANN003
                type(self).kwargs = kwargs
                return (live_bucket(),)

        original_client = web_api.GammaMarketClient
        web_api.GammaMarketClient = FakeGammaMarketClient  # type: ignore[assignment]
        try:
            buckets = web_api._optional_market_buckets_for_payload(
                {
                    "city": "Munich",
                    "temperatureKind": "high",
                    "targetDate": "2026-07-04",
                    "includeOrderbooks": True,
                },
                unit="C",
                allow_city_selector=True,
            )
        finally:
            web_api.GammaMarketClient = original_client

        self.assertEqual(len(buckets), 1)
        self.assertEqual(FakeGammaMarketClient.kwargs["query"], "Munich")
        self.assertEqual(FakeGammaMarketClient.kwargs["kind"], "high")
        self.assertEqual(FakeGammaMarketClient.kwargs["target_date"], "2026-07-04")
        self.assertTrue(FakeGammaMarketClient.kwargs["include_orderbooks"])


if __name__ == "__main__":
    unittest.main()
