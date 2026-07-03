"""Polymarket Gamma and CLOB market-data adapters."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from weather_quant.buckets import BucketParseError, parse_temperature_bucket
from weather_quant.cache import FileCache
from weather_quant.http import JsonHttpClient
from weather_quant.models import (
    MarketBucket,
    OrderBookLevel,
    OrderBookSnapshot,
    TemperatureUnit,
)


class MarketDataError(RuntimeError):
    """Raised when Polymarket data cannot be loaded or parsed."""


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [part.strip() for part in text.split(",") if part.strip()]
        if isinstance(parsed, list):
            return parsed
        return []
    return []


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class GammaMarketClient:
    """Read Polymarket market metadata and outcome prices from Gamma."""

    def __init__(
        self,
        *,
        gamma_base_url: str = "https://gamma-api.polymarket.com",
        clob_base_url: str = "https://clob.polymarket.com",
        http_client: JsonHttpClient | None = None,
        clob_http_client: JsonHttpClient | None = None,
        cache: FileCache | None = None,
        cache_max_age_seconds: int = 60,
    ) -> None:
        self.gamma_base_url = gamma_base_url.rstrip("/")
        self.clob_base_url = clob_base_url.rstrip("/")
        self.http = http_client or JsonHttpClient(base_url=self.gamma_base_url)
        self.clob_http = clob_http_client or JsonHttpClient(base_url=self.clob_base_url)
        self.cache = cache or FileCache()
        self.cache_max_age_seconds = cache_max_age_seconds

    def search_markets(
        self,
        *,
        query: str | None = None,
        slug: str | None = None,
        condition_id: str | None = None,
        limit: int = 50,
        active: bool = True,
        closed: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": limit,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
        }
        if query:
            params["search"] = query
        if slug:
            params["slug"] = slug
        if condition_id:
            params["condition_ids"] = condition_id

        cache_key = {"provider": "polymarket-gamma", "path": "/markets", "params": params}
        data = self.cache.get(cache_key, max_age_seconds=self.cache_max_age_seconds)
        if data is None:
            data = self.http.get_json("/markets", params=params)
            self.cache.set(cache_key, data)
        if not isinstance(data, list):
            raise MarketDataError("Gamma /markets response is not a list.")
        return [item for item in data if isinstance(item, dict)]

    def list_events_keyset(
        self,
        *,
        title_search: str | None = None,
        limit: int = 100,
        after_cursor: str | None = None,
        closed: bool = False,
        live: bool | None = None,
    ) -> dict[str, Any]:
        """Read one page from Gamma /events/keyset."""

        params: dict[str, Any] = {
            "limit": max(1, min(int(limit), 500)),
            "closed": str(closed).lower(),
        }
        if title_search:
            params["title_search"] = title_search
        if after_cursor:
            params["after_cursor"] = after_cursor
        if live is not None:
            params["live"] = str(live).lower()

        cache_key = {"provider": "polymarket-gamma", "path": "/events/keyset", "params": params}
        data = self.cache.get(cache_key, max_age_seconds=self.cache_max_age_seconds)
        if data is None:
            data = self.http.get_json("/events/keyset", params=params)
            self.cache.set(cache_key, data)
        if not isinstance(data, Mapping):
            raise MarketDataError("Gamma /events/keyset response is not a mapping.")
        return dict(data)

    def search_events_keyset(
        self,
        *,
        title_search: str | None = None,
        limit: int = 100,
        max_pages: int = 3,
        closed: bool = False,
        live: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch events with cursor pagination using next_cursor/after_cursor."""

        events: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max(1, max_pages)):
            page = self.list_events_keyset(
                title_search=title_search,
                limit=limit,
                after_cursor=cursor,
                closed=closed,
                live=live,
            )
            raw_events = page.get("events") or []
            if not isinstance(raw_events, list):
                raise MarketDataError("Gamma /events/keyset events field is not a list.")
            events.extend(item for item in raw_events if isinstance(item, dict))
            next_cursor = page.get("next_cursor")
            cursor = str(next_cursor) if next_cursor else None
            if not cursor:
                break
        return events

    def discover_weather_market_buckets(
        self,
        *,
        query: str,
        default_unit: TemperatureUnit = "F",
        kind: str | None = None,
        target_date: str | date | None = None,
        include_orderbooks: bool = False,
        refresh_clob_midpoints: bool = False,
        limit: int = 100,
        max_pages: int = 3,
    ) -> tuple[MarketBucket, ...]:
        """Discover weather markets from events, then parse their nested markets."""

        buckets: list[MarketBucket] = []
        seen: set[tuple[str, str]] = set()
        for event_query in _weather_event_queries(
            query,
            kind=kind,
            target_date=target_date,
        ):
            events = self.search_events_keyset(
                title_search=event_query,
                limit=limit,
                max_pages=max_pages,
                closed=False,
            )
            for event in events:
                for market in _event_markets(event):
                    if not _weather_market_matches(
                        market,
                        kind=kind,
                        target_date=target_date,
                    ):
                        continue
                    for bucket in parse_market_buckets(market, default_unit=default_unit):
                        key = (bucket.market_id, bucket.outcome)
                        if key in seen:
                            continue
                        seen.add(key)
                        buckets.append(bucket)
            if buckets:
                break
        bucket_tuple = _single_weather_market_group(tuple(buckets))
        if refresh_clob_midpoints:
            bucket_tuple = tuple(self._with_midpoints(bucket_tuple))
        if include_orderbooks:
            bucket_tuple = tuple(self._with_orderbooks(bucket_tuple))
        return bucket_tuple

    def find_market(
        self,
        *,
        query: str | None = None,
        slug: str | None = None,
        condition_id: str | None = None,
    ) -> dict[str, Any]:
        markets = self.search_markets(query=query, slug=slug, condition_id=condition_id)
        if slug:
            for market in markets:
                if str(market.get("slug") or "") == slug:
                    return market
        if condition_id:
            for market in markets:
                if str(market.get("conditionId") or market.get("condition_id") or "") == condition_id:
                    return market
        if not markets:
            raise MarketDataError("No matching Polymarket market found.")
        return markets[0]

    def get_market_buckets(
        self,
        *,
        query: str | None = None,
        slug: str | None = None,
        condition_id: str | None = None,
        default_unit: TemperatureUnit = "F",
        refresh_clob_midpoints: bool = False,
        include_orderbooks: bool = False,
    ) -> tuple[MarketBucket, ...]:
        markets = self.search_markets(query=query, slug=slug, condition_id=condition_id)
        if slug:
            markets = [
                market for market in markets
                if str(market.get("slug") or "") == slug
            ]
        if condition_id:
            markets = [
                market for market in markets
                if str(market.get("conditionId") or market.get("condition_id") or "")
                == condition_id
            ]
        if not markets:
            raise MarketDataError("No matching Polymarket market found.")
        buckets = tuple(
            bucket
            for market in markets
            for bucket in parse_market_buckets(market, default_unit=default_unit)
        )
        if refresh_clob_midpoints:
            buckets = tuple(self._with_midpoints(buckets))
        if include_orderbooks:
            buckets = tuple(self._with_orderbooks(buckets))
        return buckets

    def _with_midpoints(self, buckets: Iterable[MarketBucket]) -> Iterable[MarketBucket]:
        for bucket in buckets:
            if not bucket.token_id:
                yield bucket
                continue
            midpoint = self.get_midpoint(bucket.token_id)
            if midpoint is None:
                yield bucket
                continue
            yield MarketBucket(
                market_id=bucket.market_id,
                question=bucket.question,
                slug=bucket.slug,
                condition_id=bucket.condition_id,
                outcome=bucket.outcome,
                price=midpoint,
                bucket=bucket.bucket,
                token_id=bucket.token_id,
                raw_payload=bucket.raw_payload,
            )

    def get_midpoint(self, token_id: str) -> float | None:
        cache_key = {"provider": "polymarket-clob", "path": "/midpoint", "token_id": token_id}
        data = self.cache.get(cache_key, max_age_seconds=self.cache_max_age_seconds)
        if data is None:
            data = self.clob_http.get_json("/midpoint", params={"token_id": token_id})
            self.cache.set(cache_key, data)
        if isinstance(data, Mapping):
            return _safe_float(data.get("mid") or data.get("midpoint"))
        return None

    def _with_orderbooks(self, buckets: Iterable[MarketBucket]) -> Iterable[MarketBucket]:
        for bucket in buckets:
            if not bucket.token_id:
                yield bucket
                continue
            try:
                orderbook = self.get_orderbook(bucket.token_id)
            except MarketDataError:
                yield bucket
                continue
            price = orderbook.midpoint if orderbook.midpoint is not None else bucket.price
            yield MarketBucket(
                market_id=bucket.market_id,
                question=bucket.question,
                slug=bucket.slug,
                condition_id=bucket.condition_id,
                outcome=bucket.outcome,
                price=price,
                bucket=bucket.bucket,
                token_id=bucket.token_id,
                orderbook=orderbook,
                raw_payload=bucket.raw_payload,
            )

    def get_orderbook(self, token_id: str) -> OrderBookSnapshot:
        cache_key = {"provider": "polymarket-clob", "path": "/book", "token_id": token_id}
        data = self.cache.get(cache_key, max_age_seconds=self.cache_max_age_seconds)
        if data is None:
            data = self.clob_http.get_json("/book", params={"token_id": token_id})
            self.cache.set(cache_key, data)
        if not isinstance(data, Mapping):
            raise MarketDataError("CLOB /book response is not a mapping.")
        return parse_orderbook_snapshot(token_id=token_id, payload=data)

    def get_best_bid(self, token_id: str) -> float | None:
        return self.get_orderbook(token_id).best_bid

    def get_best_ask(self, token_id: str) -> float | None:
        return self.get_orderbook(token_id).best_ask

    def get_bid_ask(self, token_id: str) -> tuple[float | None, float | None]:
        orderbook = self.get_orderbook(token_id)
        return orderbook.best_bid, orderbook.best_ask

    def get_spread(self, token_id: str) -> float | None:
        return self.get_orderbook(token_id).spread


def parse_market_buckets(
    market: Mapping[str, Any],
    *,
    default_unit: TemperatureUnit = "F",
) -> Iterable[MarketBucket]:
    outcomes = _json_list(market.get("outcomes") or market.get("tokens"))
    prices = _json_list(market.get("outcomePrices") or market.get("outcome_prices"))
    token_ids = _json_list(
        market.get("clobTokenIds")
        or market.get("clob_token_ids")
        or market.get("tokenIds")
    )
    market_id = str(market.get("id") or market.get("marketId") or market.get("market_id") or "")
    question = str(market.get("question") or market.get("title") or "")
    slug = str(market.get("slug")) if market.get("slug") else None
    condition_id = (
        str(market.get("conditionId"))
        if market.get("conditionId")
        else str(market.get("condition_id")) if market.get("condition_id") else None
    )
    if not _looks_like_temperature_market(market, outcomes):
        return
    binary_bucket = parse_binary_market_bucket(market, default_unit=default_unit)
    if binary_bucket is not None:
        yield binary_bucket
        return

    for index, raw_outcome in enumerate(outcomes):
        if isinstance(raw_outcome, Mapping):
            outcome = str(raw_outcome.get("outcome") or raw_outcome.get("name") or "")
            token_id = (
                str(raw_outcome.get("token_id"))
                if raw_outcome.get("token_id")
                else str(raw_outcome.get("clobTokenId"))
                if raw_outcome.get("clobTokenId")
                else None
            )
            price = _safe_float(raw_outcome.get("price") or raw_outcome.get("lastPrice"))
        else:
            outcome = str(raw_outcome)
            token_id = str(token_ids[index]) if index < len(token_ids) and token_ids[index] else None
            price = _safe_float(prices[index]) if index < len(prices) else None

        if not outcome:
            continue
        if price is None:
            price = 0.0
        try:
            bucket = parse_temperature_bucket(outcome, default_unit=default_unit)
        except BucketParseError:
            continue
        yield MarketBucket(
            market_id=market_id,
            question=question,
            slug=slug,
            condition_id=condition_id,
            outcome=outcome,
            price=price,
            bucket=bucket,
            token_id=token_id,
            raw_payload=dict(market),
        )


def _looks_like_temperature_market(market: Mapping[str, Any], outcomes: list[Any]) -> bool:
    fragments = [
        str(market.get("question") or ""),
        str(market.get("title") or ""),
        str(market.get("description") or ""),
        str(market.get("slug") or ""),
        str(market.get("eventTitle") or ""),
        str(market.get("eventSlug") or ""),
        str(market.get("eventDescription") or ""),
        " ".join(_outcome_name(outcome) for outcome in outcomes),
    ]
    text = " ".join(fragments).lower()
    indicators = (
        "temperature",
        "weather",
        "temp",
        "degree",
        "degrees",
        "fahrenheit",
        "celsius",
        "°f",
        "°c",
        "high temp",
        "low temp",
    )
    return any(indicator in text for indicator in indicators)


def _weather_event_queries(
    query: str,
    *,
    kind: str | None = None,
    target_date: str | date | None = None,
) -> tuple[str, ...]:
    base = " ".join(query.strip().split())
    if not base:
        return ()
    lowered = base.lower()
    if any(token in lowered for token in ("temperature", "weather", "temp", "°f", "°c")):
        return (base,)
    kind_words = _kind_search_words(kind)
    date_words = _date_search_words(target_date)
    specific: list[str] = []
    for kind_word in kind_words:
        if date_words:
            for date_word in date_words:
                specific.append(f"{base} {kind_word} temperature {date_word}")
        specific.append(f"{base} {kind_word} temperature")
    return tuple(dict.fromkeys((
        base,
        *specific,
        f"{base} temperature",
        f"{base} weather",
    )))


def _kind_search_words(kind: str | None) -> tuple[str, ...]:
    normalized = (kind or "").strip().lower()
    if normalized in {"high", "highest", "max", "maximum"}:
        return ("highest", "high")
    if normalized in {"low", "lowest", "min", "minimum"}:
        return ("lowest", "low")
    return ("highest", "lowest", "high", "low")


def _kind_filter(kind: str | None) -> tuple[str, ...]:
    normalized = (kind or "").strip().lower()
    if normalized in {"high", "highest", "max", "maximum"}:
        return ("highest", "high temperature", "max temperature", "maximum temperature")
    if normalized in {"low", "lowest", "min", "minimum"}:
        return ("lowest", "low temperature", "min temperature", "minimum temperature")
    return ()


def _date_search_words(value: str | date | None) -> tuple[str, ...]:
    parsed = _parse_date(value)
    if parsed is None:
        return ()
    return (
        f"{parsed.strftime('%B')} {parsed.day}",
        f"{parsed.strftime('%b')} {parsed.day}",
        parsed.isoformat(),
    )


def _date_filter_words(value: str | date | None) -> tuple[str, ...]:
    parsed = _parse_date(value)
    if parsed is None:
        return ()
    day = str(parsed.day)
    return (
        f"{parsed.strftime('%B')} {parsed.day}".lower(),
        f"{parsed.strftime('%b')} {parsed.day}".lower(),
        f"{parsed.month}/{parsed.day}",
        f"{parsed.month}-{parsed.day}",
        parsed.isoformat(),
        f"july {day}" if parsed.month == 7 else "",
    )


def _parse_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%B %d", "%b %d", "%m/%d", "%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return date(date.today().year, parsed.month, parsed.day)
        except ValueError:
            continue
    return None


def _weather_market_matches(
    market: Mapping[str, Any],
    *,
    kind: str | None,
    target_date: str | date | None,
) -> bool:
    text = _market_text(market)
    kind_terms = _kind_filter(kind)
    if kind_terms and not any(term in text for term in kind_terms):
        return False

    date_terms = tuple(term for term in _date_filter_words(target_date) if term)
    if date_terms and not any(term in text for term in date_terms):
        return False
    return True


def _market_text(market: Mapping[str, Any]) -> str:
    return " ".join(
        str(market.get(key) or "")
        for key in (
            "question",
            "title",
            "description",
            "slug",
            "eventTitle",
            "eventSlug",
            "eventDescription",
        )
    ).lower()


def _single_weather_market_group(
    buckets: tuple[MarketBucket, ...],
) -> tuple[MarketBucket, ...]:
    if not buckets:
        return ()

    groups: dict[tuple[str, str, str], list[MarketBucket]] = {}
    for bucket in buckets:
        raw = bucket.raw_payload
        key = (
            str(raw.get("eventId") or raw.get("eventSlug") or raw.get("eventTitle") or ""),
            _detected_kind(_market_text(raw)),
            _detected_date_key(_market_text(raw)),
        )
        groups.setdefault(key, []).append(bucket)
    return tuple(max(groups.values(), key=len))


def _detected_kind(text: str) -> str:
    if any(term in text for term in ("lowest", "low temperature", "min temperature")):
        return "low"
    if any(term in text for term in ("highest", "high temperature", "max temperature")):
        return "high"
    return ""


def _detected_date_key(text: str) -> str:
    months = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    )
    for month in months:
        marker = f"{month} "
        if marker not in text:
            continue
        tail = text.split(marker, 1)[1].strip()
        day = ""
        for char in tail:
            if not char.isdigit():
                break
            day += char
        if day:
            return f"{month}-{day}"
    return ""


def _event_markets(event: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    raw_markets = event.get("markets") or []
    if not isinstance(raw_markets, list):
        return ()

    event_context = {
        "eventId": event.get("id"),
        "eventSlug": event.get("slug"),
        "eventTitle": event.get("title"),
        "eventDescription": event.get("description"),
    }
    return (
        {**market, **event_context}
        for market in raw_markets
        if isinstance(market, Mapping)
    )


def parse_orderbook_snapshot(
    *,
    token_id: str,
    payload: Mapping[str, Any],
) -> OrderBookSnapshot:
    bids_payload = (
        payload.get("bids")
        or payload.get("bid")
        or payload.get("buy")
        or payload.get("BUY")
        or ()
    )
    asks_payload = (
        payload.get("asks")
        or payload.get("ask")
        or payload.get("sell")
        or payload.get("SELL")
        or ()
    )
    token = str(payload.get("token_id") or payload.get("asset_id") or payload.get("assetId") or token_id)
    return OrderBookSnapshot(
        token_id=token,
        bids=tuple(_parse_orderbook_levels(bids_payload)),
        asks=tuple(_parse_orderbook_levels(asks_payload)),
        raw_payload=dict(payload),
    )


def _parse_orderbook_levels(payload: Any) -> Iterable[OrderBookLevel]:
    if isinstance(payload, Mapping):
        iterable: Iterable[Any] = payload.values()
    elif isinstance(payload, Iterable) and not isinstance(payload, (str, bytes)):
        iterable = payload
    else:
        return ()

    levels: list[OrderBookLevel] = []
    for item in iterable:
        price: float | None
        size: float | None
        if isinstance(item, Mapping):
            price = _safe_float(
                item.get("price")
                or item.get("p")
                or item.get("px")
                or item.get("rate")
            )
            size = _safe_float(
                item.get("size")
                or item.get("s")
                or item.get("quantity")
                or item.get("qty")
                or item.get("amount")
            )
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price = _safe_float(item[0])
            size = _safe_float(item[1])
        else:
            continue
        if price is None or size is None:
            continue
        if price < 0.0 or price > 1.0 or size <= 0.0:
            continue
        levels.append(OrderBookLevel(price=price, size=size))
    return levels


def parse_binary_market_bucket(
    market: Mapping[str, Any],
    *,
    default_unit: TemperatureUnit,
) -> MarketBucket | None:
    outcomes = _json_list(market.get("outcomes") or market.get("tokens"))
    normalized_outcomes = tuple(_outcome_name(outcome).strip().lower() for outcome in outcomes)
    if not normalized_outcomes:
        return None
    if "yes" not in normalized_outcomes:
        return None
    if any(
        outcome
        for outcome in normalized_outcomes
        if outcome and outcome not in {"yes", "no"}
    ):
        return None

    question = str(market.get("question") or market.get("title") or "")
    if not question:
        return None
    try:
        bucket = parse_temperature_bucket(question, default_unit=default_unit)
    except BucketParseError:
        return None

    yes_index = normalized_outcomes.index("yes")
    prices = _json_list(market.get("outcomePrices") or market.get("outcome_prices"))
    token_ids = _json_list(
        market.get("clobTokenIds")
        or market.get("clob_token_ids")
        or market.get("tokenIds")
    )
    raw_yes = outcomes[yes_index]
    price = _safe_float(_outcome_price(raw_yes))
    if price is None and yes_index < len(prices):
        price = _safe_float(prices[yes_index])
    if price is None:
        price = 0.0
    token_id = _outcome_token_id(raw_yes)
    if token_id is None and yes_index < len(token_ids) and token_ids[yes_index]:
        token_id = str(token_ids[yes_index])

    market_id = str(market.get("id") or market.get("marketId") or market.get("market_id") or "")
    slug = str(market.get("slug")) if market.get("slug") else None
    condition_id = (
        str(market.get("conditionId"))
        if market.get("conditionId")
        else str(market.get("condition_id")) if market.get("condition_id") else None
    )
    return MarketBucket(
        market_id=market_id,
        question=question,
        slug=slug,
        condition_id=condition_id,
        outcome=bucket.label,
        price=price,
        bucket=bucket,
        token_id=token_id,
        raw_payload=dict(market),
    )


def _outcome_name(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("outcome") or value.get("name") or value.get("title") or "")
    return str(value)


def _outcome_price(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    return value.get("price") or value.get("lastPrice") or value.get("midpoint")


def _outcome_token_id(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    token_id = (
        value.get("token_id")
        or value.get("clobTokenId")
        or value.get("clob_token_id")
        or value.get("tokenId")
    )
    return str(token_id) if token_id else None
