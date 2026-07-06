from __future__ import annotations

import unittest

from weather_quant.market import GammaMarketClient, parse_market_buckets


class MarketParsingTest(unittest.TestCase):
    def test_parse_gamma_market_outcomes_prices_and_token_ids(self) -> None:
        market = {
            "id": "123",
            "question": "NYC high temperature?",
            "slug": "nyc-high-temp",
            "conditionId": "0xabc",
            "outcomes": '["74 or below", "75 to 76", "77 or above"]',
            "outcomePrices": '["0.12", "0.33", "0.55"]',
            "clobTokenIds": '["a", "b", "c"]',
        }

        buckets = tuple(parse_market_buckets(market, default_unit="F"))

        self.assertEqual(len(buckets), 3)
        self.assertEqual(buckets[1].outcome, "75 to 76")
        self.assertEqual(buckets[1].price, 0.33)
        self.assertEqual(buckets[1].token_id, "b")
        self.assertEqual(buckets[1].bucket.lower, 74.5)
        self.assertEqual(buckets[1].bucket.upper, 76.5)

    def test_parse_binary_yes_no_temperature_market_uses_question_bucket(self) -> None:
        market = {
            "id": "456",
            "question": "Will New York City's high temperature be 90° or above on July 3?",
            "slug": "nyc-high-temp-90-above-july-3",
            "conditionId": "0xdef",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.22", "0.78"]',
            "clobTokenIds": '["yes-token", "no-token"]',
        }

        buckets = tuple(parse_market_buckets(market, default_unit="F"))

        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0].price, 0.22)
        self.assertEqual(buckets[0].token_id, "yes-token")
        self.assertEqual(buckets[0].bucket.lower, 89.5)
        self.assertIsNone(buckets[0].bucket.upper)

    def test_numeric_non_weather_markets_are_ignored(self) -> None:
        market = {
            "id": "789",
            "question": "Will Spain win the 2026 FIFA World Cup?",
            "slug": "will-spain-win-the-2026-fifa-world-cup",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.12", "0.88"]',
            "clobTokenIds": '["yes-token", "no-token"]',
        }

        buckets = tuple(parse_market_buckets(market, default_unit="F"))

        self.assertEqual(buckets, ())

    def test_get_market_buckets_combines_multiple_binary_markets(self) -> None:
        class FakeCache:
            def get(self, key, *, max_age_seconds=None):  # noqa: ANN001
                return None

            def set(self, key, value):  # noqa: ANN001
                return None

        class FakeHttpClient:
            def get_json(self, path: str, *, params=None, headers=None):  # noqa: ANN001
                self.path = path
                self.params = params
                return [
                    {
                        "id": "1",
                        "question": "Will NYC high temperature be 84 to 85°F on July 3?",
                        "slug": "nyc-84-85",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.31", "0.69"]',
                        "clobTokenIds": '["a", "b"]',
                    },
                    {
                        "id": "2",
                        "question": "Will NYC high temperature be 86 to 87°F on July 3?",
                        "slug": "nyc-86-87",
                        "outcomes": '["Yes", "No"]',
                        "outcomePrices": '["0.27", "0.73"]',
                        "clobTokenIds": '["c", "d"]',
                    },
                ]

        client = GammaMarketClient(http_client=FakeHttpClient(), cache=FakeCache())

        buckets = client.get_market_buckets(query="NYC high temperature July 3")

        self.assertEqual([bucket.price for bucket in buckets], [0.31, 0.27])
        self.assertEqual([bucket.token_id for bucket in buckets], ["a", "c"])

    def test_discover_weather_buckets_from_events_keyset_pages(self) -> None:
        class FakeCache:
            def get(self, key, *, max_age_seconds=None):  # noqa: ANN001
                return None

            def set(self, key, value):  # noqa: ANN001
                return None

        class FakeHttpClient:
            calls = []

            def get_json(self, path: str, *, params=None, headers=None):  # noqa: ANN001
                self.calls.append((path, dict(params or {})))
                if params and params.get("after_cursor") == "page-2":
                    return {
                        "events": [
                            {
                                "id": "event-2",
                                "title": "Shanghai low temperature July 4",
                                "slug": "shanghai-low-temp-july-4",
                                "markets": [
                                    {
                                        "id": "2",
                                        "question": "Will Shanghai low temperature be 29 to 30°C?",
                                        "slug": "shanghai-29-30",
                                        "outcomes": '["Yes", "No"]',
                                        "outcomePrices": '["0.25", "0.75"]',
                                        "clobTokenIds": '["c", "d"]',
                                    },
                                ],
                            }
                        ],
                    }
                return {
                    "events": [
                        {
                            "id": "event-1",
                            "title": "Shanghai high temperature July 3",
                            "slug": "shanghai-high-temp-july-3",
                            "markets": [
                                {
                                    "id": "1",
                                    "question": "85 to 86",
                                    "slug": "shanghai-85-86",
                                    "outcomes": '["85 to 86"]',
                                    "outcomePrices": '["0.40"]',
                                    "clobTokenIds": '["a"]',
                                },
                            ],
                        }
                    ],
                    "next_cursor": "page-2",
                }

        http = FakeHttpClient()
        client = GammaMarketClient(http_client=http, cache=FakeCache())

        buckets = client.discover_weather_market_buckets(
            query="Shanghai temperature",
            default_unit="F",
            max_pages=2,
        )

        self.assertEqual([bucket.token_id for bucket in buckets], ["a"])
        self.assertEqual(buckets[0].bucket.lower, 84.5)
        self.assertEqual(http.calls[0][1]["title_search"], "Shanghai temperature")
        self.assertEqual(http.calls[1][1]["after_cursor"], "page-2")

    def test_discover_weather_buckets_filters_one_kind_and_date(self) -> None:
        class FakeCache:
            def get(self, key, *, max_age_seconds=None):  # noqa: ANN001
                return None

            def set(self, key, value):  # noqa: ANN001
                return None

        class FakeHttpClient:
            def get_json(self, path: str, *, params=None, headers=None):  # noqa: ANN001
                return {
                    "events": [
                        {
                            "id": "event-1",
                            "title": "Shanghai temperature July 3",
                            "markets": [
                                {
                                    "id": "high-33",
                                    "question": "Will the highest temperature in Shanghai be 33°C on July 3?",
                                    "slug": "shanghai-high-33",
                                    "outcomes": '["Yes", "No"]',
                                    "outcomePrices": '["0.91", "0.09"]',
                                    "clobTokenIds": '["high-yes", "high-no"]',
                                },
                                {
                                    "id": "low-20",
                                    "question": "Will the lowest temperature in Shanghai be 20°C on July 3?",
                                    "slug": "shanghai-low-20",
                                    "outcomes": '["Yes", "No"]',
                                    "outcomePrices": '["0.12", "0.88"]',
                                    "clobTokenIds": '["low-yes", "low-no"]',
                                },
                                {
                                    "id": "high-34-next-day",
                                    "question": "Will the highest temperature in Shanghai be 34°C on July 4?",
                                    "slug": "shanghai-high-34-july-4",
                                    "outcomes": '["Yes", "No"]',
                                    "outcomePrices": '["0.31", "0.69"]',
                                    "clobTokenIds": '["next-day-yes", "next-day-no"]',
                                },
                            ],
                        }
                    ],
                }

        client = GammaMarketClient(http_client=FakeHttpClient(), cache=FakeCache())

        buckets = client.discover_weather_market_buckets(
            query="Shanghai",
            default_unit="C",
            kind="high",
            target_date="2026-07-03",
        )

        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0].token_id, "high-yes")
        self.assertIn("highest", buckets[0].question)
        self.assertIn("July 3", buckets[0].question)

    def test_discover_weather_buckets_expands_plain_city_query(self) -> None:
        class FakeCache:
            def get(self, key, *, max_age_seconds=None):  # noqa: ANN001
                return None

            def set(self, key, value):  # noqa: ANN001
                return None

        class FakeHttpClient:
            calls = []

            def get_json(self, path: str, *, params=None, headers=None):  # noqa: ANN001
                self.calls.append(dict(params or {}))
                if params and params.get("title_search") == "Shanghai temperature":
                    return {
                        "events": [
                            {
                                "id": "event-1",
                                "title": "Shanghai high temperature July 3",
                                "markets": [
                                    {
                                        "id": "1",
                                        "question": "Will Shanghai high temperature be 35°C or above?",
                                        "slug": "shanghai-35-above",
                                        "outcomes": '["Yes", "No"]',
                                        "outcomePrices": '["0.41", "0.59"]',
                                        "clobTokenIds": '["yes", "no"]',
                                    },
                                ],
                            }
                        ],
                    }
                return {"events": []}

        http = FakeHttpClient()
        client = GammaMarketClient(http_client=http, cache=FakeCache())

        buckets = client.discover_weather_market_buckets(query="Shanghai", default_unit="C")

        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0].token_id, "yes")
        self.assertEqual(http.calls[0]["title_search"], "Shanghai")
        self.assertIn(
            "Shanghai temperature",
            [call["title_search"] for call in http.calls],
        )

    def test_discover_weather_buckets_falls_back_from_generated_temperature_query(self) -> None:
        class FakeCache:
            def get(self, key, *, max_age_seconds=None):  # noqa: ANN001
                return None

            def set(self, key, value):  # noqa: ANN001
                return None

        class FakeHttpClient:
            calls = []

            def get_json(self, path: str, *, params=None, headers=None):  # noqa: ANN001
                self.calls.append(dict(params or {}))
                if params and params.get("title_search") == "Shanghai temperature":
                    return {
                        "events": [
                            {
                                "id": "event-1",
                                "title": "Shanghai weather July 3",
                                "markets": [
                                    {
                                        "id": "1",
                                        "question": "Will the highest temperature in Shanghai be 33°C on July 3?",
                                        "slug": "shanghai-33",
                                        "outcomes": '["Yes", "No"]',
                                        "outcomePrices": '["0.36", "0.64"]',
                                        "clobTokenIds": '["yes", "no"]',
                                    },
                                ],
                            }
                        ],
                    }
                return {"events": []}

        http = FakeHttpClient()
        client = GammaMarketClient(http_client=http, cache=FakeCache())

        buckets = client.discover_weather_market_buckets(
            query="Shanghai high temperature",
            default_unit="C",
            kind="high",
            target_date="2026-07-03",
        )

        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0].token_id, "yes")
        self.assertEqual(http.calls[0]["title_search"], "Shanghai high temperature")
        self.assertIn(
            "Shanghai temperature",
            [call["title_search"] for call in http.calls],
        )


if __name__ == "__main__":
    unittest.main()
