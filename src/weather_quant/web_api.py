"""JSON helpers used by the Node/HTML frontend."""

from __future__ import annotations

import json
import sys
from datetime import date
from typing import Any

from weather_quant.config import get_city
from weather_quant.ensemble import (
    build_bucket_distribution,
    default_buckets_for_run,
    ensemble_chart_data,
    ensemble_signal_rows,
)
from weather_quant.llm import OpenAILlmSummaryClient
from weather_quant.market import GammaMarketClient, MarketDataError
from weather_quant.models import DEFAULT_TAKER_FEE_RATE, CityConfig, MarketBucket, Portfolio, TemperatureKind
from weather_quant.paper_trading import (
    DEFAULT_ACCOUNT_KEY,
    DEFAULT_INITIAL_CASH,
    DEFAULT_MAX_CITY_DATE_EXPOSURE,
    DEFAULT_MAX_MARKET_EXPOSURE,
    DEFAULT_MAX_SPREAD,
    DEFAULT_MIN_ASK_DEPTH_SHARES,
    DEFAULT_MIN_EDGE,
    DEFAULT_STAKE_USDC,
    bucket_from_key,
    market_buckets_from_payload,
)
from weather_quant.portfolio import (
    calculate_hedge_lock,
    generate_passive_exit_plan,
    market_buckets_from_positions,
    market_best_ask,
    market_best_bid,
    market_mark_price,
    orderbook_overround,
    parse_inline_market_buckets,
    parse_inline_positions,
    portfolio_cashout_ratio,
    portfolio_liquidation_value,
    portfolio_mark_value,
    probabilities_from_market_buckets,
    value_portfolio,
)
from weather_quant.storage import WeatherStorage
from weather_quant.settlement import SettlementImporter
from weather_quant.station_lookup import StationLookupClient
from weather_quant.units import normalize_unit
from weather_quant.weather import OpenMeteoEnsembleClient, WeatherEnsembleProvider


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _bool_value(value: Any, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _target_date_from_payload(payload: dict[str, Any]) -> date:
    text = _optional_text(payload.get("targetDate") or payload.get("date"))
    if not text:
        raise ValueError("Provide targetDate.")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("targetDate must use YYYY-MM-DD format.") from exc


def _date_from_payload_keys(payload: dict[str, Any], *keys: str) -> date | None:
    for key in keys:
        text = _optional_text(payload.get(key))
        if not text:
            continue
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{key} must use YYYY-MM-DD format.") from exc
    return None


def _kind_from_payload(payload: dict[str, Any]) -> TemperatureKind:
    text = str(payload.get("temperatureKind") or payload.get("kind") or "high").strip().lower()
    if text not in {"high", "low"}:
        raise ValueError("temperatureKind must be high or low.")
    return text  # type: ignore[return-value]


def _models_from_payload(payload: dict[str, Any]) -> tuple[str, ...] | None:
    raw = payload.get("models") or payload.get("weatherModels")
    if isinstance(raw, str):
        models = tuple(item.strip() for item in raw.split(",") if item.strip())
    elif isinstance(raw, (list, tuple)):
        models = tuple(str(item).strip() for item in raw if str(item).strip())
    else:
        models = ()
    return models or None


def _model_from_payload(payload: dict[str, Any]) -> str:
    explicit = _optional_text(payload.get("ensembleModel") or payload.get("model"))
    if explicit:
        return explicit
    models = _models_from_payload(payload)
    return models[0] if models else "ecmwf_aifs025"


def _save_requested(payload: dict[str, Any]) -> bool:
    return _bool_value(payload.get("saveSqlite") or payload.get("save"), default=False)


def _payload_raw(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _payload_float(payload: dict[str, Any], *keys: str) -> float | None:
    raw = _payload_raw(payload, *keys)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{keys[0]} must be a number.") from exc


def _slug_token(value: str | None) -> str:
    text = str(value or "custom").strip().lower()
    chars: list[str] = []
    for char in text:
        if "a" <= char <= "z" or "0" <= char <= "9":
            chars.append(char)
        elif chars and chars[-1] != "-":
            chars.append("-")
    return "".join(chars).strip("-") or "custom"


def _coordinate_token(value: float) -> str:
    return f"{value:.4f}".replace("-", "m").replace(".", "p")


def _coordinate_city_id(prefix: str | None, latitude: float, longitude: float) -> str:
    return (
        f"{_slug_token(prefix)}-"
        f"{_coordinate_token(latitude)}-"
        f"{_coordinate_token(longitude)}"
    )


def _forecast_granularity_from_payload(
    payload: dict[str, Any],
    *,
    default: str = "city",
) -> str:
    text = _optional_text(
        payload.get("forecastGranularity") or payload.get("forecast_granularity")
    )
    if not text:
        return default if default in {"city", "station"} else "city"
    normalized = text.lower()
    if normalized not in {"city", "station"}:
        raise ValueError("forecastGranularity must be city or station.")
    return normalized


def _cell_selection_from_payload(
    payload: dict[str, Any],
    *,
    default: str | None = None,
) -> str | None:
    text = _optional_text(payload.get("cellSelection") or payload.get("cell_selection"))
    if not text:
        return default
    normalized = text.lower()
    if normalized not in {"land", "sea", "nearest"}:
        raise ValueError("cellSelection must be land, sea, or nearest.")
    return normalized


def _coordinate_payload_present(payload: dict[str, Any]) -> bool:
    return (
        _payload_raw(payload, "latitude", "lat") is not None
        or _payload_raw(payload, "longitude", "lon", "lng") is not None
    )


def _city_storage(payload: dict[str, Any]) -> WeatherStorage:
    return WeatherStorage(payload.get("dbPath") or None, initialize=True)


def _stored_city_requested(payload: dict[str, Any]) -> bool:
    return _bool_value(
        payload.get("useStoredCity") or payload.get("use_stored_city"),
        default=False,
    )


def _city_from_payload(payload: dict[str, Any]) -> CityConfig:
    city_text = _optional_text(payload.get("city") or payload.get("cityId"))
    if not _coordinate_payload_present(payload):
        if not city_text:
            raise ValueError("Provide city.")
        if _stored_city_requested(payload):
            stored_city = _city_storage(payload).get_city(city_text)
            if stored_city is not None:
                return stored_city
            raise KeyError(f"Unknown stored city config: {city_text}")
        try:
            return get_city(city_text)
        except KeyError:
            stored_city = _city_storage(payload).get_city(city_text)
            if stored_city is not None:
                return stored_city
            raise

    latitude = _payload_float(payload, "latitude", "lat")
    longitude = _payload_float(payload, "longitude", "lon", "lng")
    if latitude is None or longitude is None:
        raise ValueError("Provide both latitude and longitude.")
    if latitude < -90 or latitude > 90:
        raise ValueError("latitude must be between -90 and 90.")
    if longitude < -180 or longitude > 180:
        raise ValueError("longitude must be between -180 and 180.")

    base_city: CityConfig | None = None
    if city_text:
        try:
            base_city = get_city(city_text)
        except KeyError:
            base_city = None

    unit_text = _optional_text(
        payload.get("settlementUnit")
        or payload.get("settlement_unit")
        or payload.get("temperatureUnit")
        or payload.get("temperature_unit")
        or payload.get("unit")
    )
    settlement_unit = (
        normalize_unit(unit_text)
        if unit_text
        else (base_city.settlement_unit if base_city else "F")
    )
    models = _models_from_payload(payload) or (
        base_city.weather_models
        if base_city
        else (
            "ecmwf_ifs025",
            "icon_seamless",
            "meteofrance_seamless",
            "gfs_seamless",
            "ukmo_seamless",
        )
    )
    location_id = _optional_text(
        payload.get("locationId")
        or payload.get("location_id")
        or payload.get("customCityId")
        or payload.get("custom_city_id")
    )
    location_name = _optional_text(
        payload.get("locationName")
        or payload.get("location")
        or payload.get("cityName")
        or payload.get("name")
        or city_text
    )
    elevation = _payload_float(payload, "elevation")
    model_error_std = _payload_float(payload, "modelErrorStd", "model_error_std")
    min_distribution_std = _payload_float(
        payload,
        "minDistributionStd",
        "min_distribution_std",
    )

    return CityConfig(
        city_id=(
            location_id
            or _coordinate_city_id(
                base_city.city_id if base_city else city_text,
                latitude,
                longitude,
            )
        ),
        name=location_name or (base_city.name if base_city else "Custom location"),
        latitude=latitude,
        longitude=longitude,
        timezone=_optional_text(payload.get("timezone") or payload.get("timeZone"))
        or (base_city.timezone if base_city else "auto"),
        settlement_station=_optional_text(
            payload.get("settlementStation") or payload.get("settlement_station")
        ) or (base_city.settlement_station if base_city else None),
        station_id=_optional_text(payload.get("stationId") or payload.get("station_id"))
        or (base_city.station_id if base_city else None),
        metar_source=_optional_text(payload.get("metarSource") or payload.get("metar_source"))
        or (base_city.metar_source if base_city else None),
        forecast_granularity=_forecast_granularity_from_payload(
            payload,
            default=base_city.forecast_granularity if base_city else "city",
        ),  # type: ignore[arg-type]
        settlement_unit=settlement_unit,
        weather_models=models,
        model_weights=base_city.model_weights if base_city else {},
        model_error_std=(
            model_error_std
            if model_error_std is not None
            else (base_city.model_error_std if base_city else 2.5)
        ),
        min_distribution_std=(
            min_distribution_std
            if min_distribution_std is not None
            else (base_city.min_distribution_std if base_city else 1.0)
        ),
        elevation=(
            elevation
            if elevation is not None
            else (base_city.elevation if base_city else None)
        ),
        cell_selection=_cell_selection_from_payload(
            payload,
            default=base_city.cell_selection if base_city else None,
        ),  # type: ignore[arg-type]
    )


def _city_config_from_editor_payload(payload: dict[str, Any]) -> CityConfig:
    storage = _city_storage(payload)
    editing_city_id = _optional_text(
        payload.get("editingCityId") or payload.get("editing_city_id")
    )
    raw_city_id = _optional_text(
        editing_city_id
        or payload.get("cityId")
        or payload.get("city_id")
        or payload.get("locationId")
        or payload.get("location_id")
        or payload.get("city")
    )
    existing = storage.get_city(raw_city_id) if raw_city_id else None
    latitude = _payload_float(payload, "latitude", "lat")
    longitude = _payload_float(payload, "longitude", "lon", "lng")
    if latitude is None:
        latitude = existing.latitude if existing else None
    if longitude is None:
        longitude = existing.longitude if existing else None
    if latitude is None or longitude is None:
        raise ValueError("Provide both latitude and longitude.")
    if latitude < -90 or latitude > 90:
        raise ValueError("latitude must be between -90 and 90.")
    if longitude < -180 or longitude > 180:
        raise ValueError("longitude must be between -180 and 180.")

    name = _optional_text(payload.get("name") or payload.get("cityName") or payload.get("locationName"))
    city_id = _slug_token(raw_city_id or name) if (raw_city_id or name) else _coordinate_city_id(
        None,
        latitude,
        longitude,
    )
    unit_text = _optional_text(
        payload.get("settlementUnit")
        or payload.get("settlement_unit")
        or payload.get("unit")
        or payload.get("temperatureUnit")
        or payload.get("temperature_unit")
    )
    elevation = _payload_float(payload, "elevation")
    model_error_std = _payload_float(payload, "modelErrorStd", "model_error_std")
    min_distribution_std = _payload_float(
        payload,
        "minDistributionStd",
        "min_distribution_std",
    )

    return CityConfig(
        city_id=city_id,
        name=name or (existing.name if existing else city_id),
        latitude=latitude,
        longitude=longitude,
        timezone=_optional_text(payload.get("timezone") or payload.get("timeZone"))
        or (existing.timezone if existing else "auto"),
        settlement_station=_optional_text(
            payload.get("settlementStation") or payload.get("settlement_station")
        ) or (existing.settlement_station if existing else None),
        station_id=_optional_text(payload.get("stationId") or payload.get("station_id"))
        or (existing.station_id if existing else None),
        metar_source=_optional_text(payload.get("metarSource") or payload.get("metar_source"))
        or (existing.metar_source if existing else None),
        forecast_granularity=_forecast_granularity_from_payload(
            payload,
            default=existing.forecast_granularity if existing else "city",
        ),  # type: ignore[arg-type]
        settlement_unit=normalize_unit(
            unit_text or (existing.settlement_unit if existing else "F")
        ),
        weather_models=_models_from_payload(payload)
        or (existing.weather_models if existing else ("ecmwf_ifs025", "gfs_seamless", "ukmo_seamless")),
        model_weights=existing.model_weights if existing else {},
        model_error_std=(
            model_error_std
            if model_error_std is not None
            else (existing.model_error_std if existing else 2.5)
        ),
        min_distribution_std=(
            min_distribution_std
            if min_distribution_std is not None
            else (existing.min_distribution_std if existing else 1.0)
        ),
        elevation=(
            elevation
            if elevation is not None
            else (existing.elevation if existing else None)
        ),
        cell_selection=_cell_selection_from_payload(
            payload,
            default=existing.cell_selection if existing else None,
        ),  # type: ignore[arg-type]
    )


def city_list_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {"cities": _city_storage(payload).list_cities()}


def city_save_payload(payload: dict[str, Any]) -> dict[str, Any]:
    storage = _city_storage(payload)
    city = _city_config_from_editor_payload(payload)
    saved_city = storage.save_city(city)
    return {"city": saved_city, "cities": storage.list_cities()}


def station_lookup_payload(payload: dict[str, Any]) -> dict[str, Any]:
    matches = StationLookupClient().lookup(
        settlement_station=_optional_text(
            payload.get("settlementStation")
            or payload.get("settlement_station")
            or payload.get("query")
        ),
        station_id=_optional_text(payload.get("stationId") or payload.get("station_id")),
        country_code=_optional_text(payload.get("countryCode") or payload.get("country_code")),
        limit=int(payload.get("limit") or 5),
    )
    return {
        "station": matches[0],
        "matches": list(matches),
    }


def _market_buckets_for_payload(
    payload: dict[str, Any],
    *,
    positions,
    unit: str,
) -> tuple[tuple, str]:
    markets_text = str(payload.get("marketsCsv") or "")
    if markets_text.strip():
        return parse_inline_market_buckets(markets_text, default_unit=unit), "csv"  # type: ignore[arg-type]

    query, slug, condition_id = _selector_from_payload(payload)
    if query or slug or condition_id:
        buckets, _selector = _load_live_market_buckets(payload)
        return buckets, "polymarket"

    return market_buckets_from_positions(positions, default_unit=unit), "positions"  # type: ignore[arg-type]


def _optional_market_buckets_for_payload(
    payload: dict[str, Any],
    *,
    unit: str,
    allow_city_selector: bool = False,
) -> tuple:
    markets_text = str(payload.get("marketsCsv") or "")
    if markets_text.strip():
        return parse_inline_market_buckets(markets_text, default_unit=unit)  # type: ignore[arg-type]
    has_explicit_selector = (
        _optional_text(payload.get("marketQuery") or payload.get("query"))
        or _optional_text(payload.get("marketSlug") or payload.get("slug"))
        or _optional_text(payload.get("conditionId") or payload.get("condition_id"))
    )
    has_city_selector = allow_city_selector and _optional_text(payload.get("city") or payload.get("cityId"))
    if has_explicit_selector or has_city_selector:
        buckets, _selector = _load_live_market_buckets(payload)
        return buckets
    return ()


def _selector_from_payload(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    query = _optional_text(payload.get("marketQuery") or payload.get("query"))
    if query is None:
        query = _selector_city_from_payload(payload)
    slug = _optional_text(payload.get("marketSlug") or payload.get("slug"))
    condition_id = _optional_text(payload.get("conditionId") or payload.get("condition_id"))
    return query, slug, condition_id


def _selector_city_from_payload(payload: dict[str, Any]) -> str | None:
    return _optional_text(
        payload.get("locationName")
        or payload.get("cityName")
        or payload.get("location")
        or payload.get("name")
        or payload.get("city")
        or payload.get("cityId")
    )


def _load_live_market_buckets(payload: dict[str, Any]):
    unit = str(payload.get("unit") or "F")
    query, slug, condition_id = _selector_from_payload(payload)
    kind = _optional_text(payload.get("temperatureKind") or payload.get("kind"))
    target_date = _optional_text(payload.get("targetDate") or payload.get("date"))
    if not (query or slug or condition_id):
        raise ValueError("Provide a city/search term, marketSlug, or conditionId.")
    include_orderbooks = _bool_value(payload.get("includeOrderbooks"), default=True)
    refresh_midpoints = _bool_value(
        payload.get("refreshClobMidpoints"),
        default=not include_orderbooks,
    )
    client = GammaMarketClient()
    method = "events_keyset"
    buckets = ()
    if slug or condition_id:
        method = "markets"
        try:
            buckets = client.get_market_buckets(
                query=query,
                slug=slug,
                condition_id=condition_id,
                default_unit=unit,  # type: ignore[arg-type]
                refresh_clob_midpoints=refresh_midpoints,
                include_orderbooks=include_orderbooks,
            )
        except MarketDataError:
            buckets = ()
    if not buckets and query:
        method = "events_keyset"
        buckets = client.discover_weather_market_buckets(
            query=query,
            default_unit=unit,  # type: ignore[arg-type]
            kind=kind,
            target_date=target_date,
            refresh_clob_midpoints=refresh_midpoints,
            include_orderbooks=include_orderbooks,
            limit=int(payload.get("eventLimit") or 100),
            max_pages=int(payload.get("maxEventPages") or 3),
        )
    if not buckets:
        raise ValueError("No matching Polymarket weather event/market found.")
    return buckets, {
        "query": query,
        "slug": slug,
        "conditionId": condition_id,
        "unit": unit,
        "kind": kind,
        "targetDate": target_date,
        "includeOrderbooks": include_orderbooks,
        "method": method,
    }


def _timezone_from_payload(payload: dict[str, Any]) -> str | None:
    explicit = _optional_text(payload.get("timezone") or payload.get("timeZone"))
    if explicit:
        return explicit
    has_city = _optional_text(payload.get("city") or payload.get("cityId"))
    if not (has_city or _coordinate_payload_present(payload)):
        return None
    try:
        return _city_from_payload(payload).timezone
    except (KeyError, ValueError):
        return None


def _level_payload(levels, *, limit: int):  # noqa: ANN001
    return [
        {"price": level.price, "size": level.size}
        for level in tuple(levels)[: max(0, limit)]
    ]


def _market_bucket_payload(bucket, *, depth_limit: int = 10):  # noqa: ANN001
    return {
        "outcome": bucket.outcome,
        "question": bucket.question,
        "marketId": bucket.market_id,
        "slug": bucket.slug,
        "conditionId": bucket.condition_id,
        "tokenId": bucket.token_id,
        "price": bucket.price,
        "markPrice": market_mark_price(bucket),
        "bestBid": market_best_bid(bucket),
        "bestAsk": market_best_ask(bucket),
        "midpoint": bucket.orderbook.midpoint if bucket.orderbook else None,
        "spread": bucket.orderbook.spread if bucket.orderbook else None,
        "bucket": {
            "label": bucket.bucket.label,
            "lower": bucket.bucket.lower,
            "upper": bucket.bucket.upper,
            "unit": bucket.bucket.unit,
            "lowerInclusive": bucket.bucket.lower_inclusive,
            "upperInclusive": bucket.bucket.upper_inclusive,
        },
        "bucketLabel": bucket.bucket.label,
        "bucketKey": bucket.bucket.canonical_key,
        "orderbook": {
            "bids": _level_payload(bucket.orderbook.bids, limit=depth_limit),
            "asks": _level_payload(bucket.orderbook.asks, limit=depth_limit),
        } if bucket.orderbook else None,
    }


def market_payload(payload: dict[str, Any]) -> dict[str, Any]:
    buckets, selector = _load_live_market_buckets(payload)
    depth_limit = int(payload.get("depthLimit") or 10)
    overround = orderbook_overround(buckets)
    return {
        "summary": {
            "marketSource": "polymarket",
            "marketCount": len(buckets),
            "askSum": float(overround["ask_sum"]),
            "bidSum": float(overround["bid_sum"]),
            "midpointSum": float(overround["midpoint_sum"]),
            "isOverround": bool(overround["is_overround"]),
            "selector": selector,
            "timezone": _timezone_from_payload(payload),
        },
        "buckets": [_market_bucket_payload(bucket, depth_limit=depth_limit) for bucket in buckets],
    }


def forecast_payload(payload: dict[str, Any]) -> dict[str, Any]:
    city = _city_from_payload(payload)
    target_date = _target_date_from_payload(payload)
    kind = _kind_from_payload(payload)
    requested_models = _models_from_payload(payload)
    ensemble = WeatherEnsembleProvider().fetch_ensemble(
        city,
        target_date=target_date,
        kind=kind,
        models=requested_models,
    )
    values = [point.value for point in ensemble.points]
    return {
        "summary": {
            "cityId": city.city_id,
            "cityName": city.name,
            "latitude": city.latitude,
            "longitude": city.longitude,
            "timezone": city.timezone,
            "elevation": city.elevation,
            "cellSelection": city.cell_selection,
            "targetDate": target_date.isoformat(),
            "kind": kind,
            "unit": city.settlement_unit,
            "modelCount": len(ensemble.points),
            "models": list(ensemble.source_models),
            "warnings": list(ensemble.provider_warnings),
            "failedModelCount": len(ensemble.provider_warnings),
            "mean": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
            "fetchedAt": ensemble.fetched_at.isoformat(),
        },
        "points": [
            {
                "cityId": point.city_id,
                "targetDate": point.target_date.isoformat(),
                "kind": point.kind,
                "value": point.value,
                "unit": point.unit,
                "sourceModel": point.source_model,
                "settlementStation": point.settlement_station,
                "stationId": point.station_id,
                "metarSource": point.metar_source,
                "forecastGranularity": point.forecast_granularity,
                "generatedAt": point.generated_at.isoformat(),
                "provider": point.raw_payload.get("provider"),
            }
            for point in ensemble.points
        ],
    }


def _ensemble_payload(payload: dict[str, Any], *, include_signals: bool) -> dict[str, Any]:
    city = _city_from_payload(payload)
    target_date = _target_date_from_payload(payload)
    kind = _kind_from_payload(payload)
    model = _model_from_payload(payload)
    forecast_days = payload.get("forecastDays") or payload.get("forecast_days")
    forecast_days_int = int(forecast_days) if forecast_days not in (None, "") else None
    start_date = _date_from_payload_keys(payload, "startDate", "start_date")
    end_date = _date_from_payload_keys(payload, "endDate", "end_date")
    client = OpenMeteoEnsembleClient()
    run = client.fetch_run(
        city,
        target_date=target_date,
        kind=kind,
        model=model,
        forecast_days=forecast_days_int,
        start_date=start_date,
        end_date=end_date,
    )
    market_buckets = _optional_market_buckets_for_payload(
        payload,
        unit=city.settlement_unit,
        allow_city_selector=include_signals
        or _bool_value(payload.get("includeMarketBuckets"), default=False),
    )
    buckets = tuple(item.bucket for item in market_buckets) if market_buckets else default_buckets_for_run(run)
    distribution = build_bucket_distribution(run, buckets)
    signals = ensemble_signal_rows(
        distribution,
        market_buckets,
        fee_rate=float(payload.get("feeRate") or DEFAULT_TAKER_FEE_RATE),
        min_edge=float(payload.get("minEdge") or 0.03),
    ) if include_signals and market_buckets else ()

    market_snapshot_group = None
    saved = False
    if _save_requested(payload):
        storage = WeatherStorage(payload.get("dbPath") or None, initialize=True)
        storage.save_distribution(distribution)
        market_snapshot_group = storage.save_market_snapshots(market_buckets)
        storage.save_signal_snapshots(
            run_key=run.run_key,
            rows=signals,
            market_snapshot_group=market_snapshot_group,
        )
        saved = True

    return {
        "summary": {
            "runKey": run.run_key,
            "provider": run.provider,
            "model": run.model,
            "cityId": city.city_id,
            "cityName": city.name,
            "latitude": city.latitude,
            "longitude": city.longitude,
            "timezone": city.timezone,
            "elevation": city.elevation,
            "cellSelection": city.cell_selection,
            "targetDate": target_date.isoformat(),
            "kind": kind,
            "unit": distribution.unit,
            "memberCount": distribution.total_members,
            "unmatchedCount": distribution.unmatched_count,
            "empiricalMean": distribution.empirical_mean,
            "empiricalStd": distribution.empirical_std,
            "p10": distribution.p10,
            "p50": distribution.p50,
            "p90": distribution.p90,
            "settlementStation": city.settlement_station,
            "stationId": city.station_id,
            "forecastGranularity": city.forecast_granularity,
            "marketBucketCount": len(market_buckets),
            "saved": saved,
            "marketSnapshotGroup": market_snapshot_group,
        },
        "probabilities": [
            {
                "bucketLabel": item.bucket.label,
                "bucketKey": item.bucket.canonical_key,
                "hitCount": item.hit_count,
                "probability": item.probability,
                "totalMembers": item.total_members,
                "unmatchedCount": item.unmatched_count,
            }
            for item in distribution.probabilities
        ],
        "members": [
            {
                "memberId": item.member_id,
                "value": item.value,
                "unit": item.unit,
                "bucketLabel": item.bucket_label,
                "bucketKey": item.bucket_key,
            }
            for item in distribution.member_values
        ],
        "chart": ensemble_chart_data(distribution, market_buckets=market_buckets),
        "signals": list(signals),
        "marketBuckets": [
            _market_bucket_payload(bucket, depth_limit=int(payload.get("depthLimit") or 10))
            for bucket in market_buckets
        ],
    }


def ensemble_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _ensemble_payload(payload, include_signals=False)


def ensemble_signal_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _ensemble_payload(payload, include_signals=True)


def db_runs_payload(payload: dict[str, Any]) -> dict[str, Any]:
    storage = WeatherStorage(payload.get("dbPath") or None)
    return {"runs": storage.recent_runs(limit=int(payload.get("limit") or 20))}


def db_probabilities_payload(payload: dict[str, Any]) -> dict[str, Any]:
    storage = WeatherStorage(payload.get("dbPath") or None)
    return {"probabilities": storage.recent_probabilities(limit=int(payload.get("limit") or 50))}


def _settlement_buckets_for_payload(
    payload: dict[str, Any],
    *,
    city: CityConfig,
) -> tuple[tuple, str | None]:
    if not _bool_value(payload.get("includeMarketBuckets"), default=True):
        return (), None
    try:
        market_buckets = _optional_market_buckets_for_payload(
            payload,
            unit=city.settlement_unit,
            allow_city_selector=True,
        )
    except Exception as exc:  # noqa: BLE001
        return (), str(exc)
    return market_buckets, None


def settlement_import_payload(payload: dict[str, Any]) -> dict[str, Any]:
    city = _city_from_payload(payload)
    target_date = _target_date_from_payload(payload)
    kind = _kind_from_payload(payload)
    storage = WeatherStorage(payload.get("dbPath") or None, initialize=True)
    market_buckets, bucket_warning = _settlement_buckets_for_payload(payload, city=city)
    request_payload = {
        "cityId": city.city_id,
        "targetDate": target_date.isoformat(),
        "kind": kind,
        "stationId": city.station_id,
        "metarSource": city.metar_source,
        "marketBucketCount": len(market_buckets),
    }
    try:
        observation = SettlementImporter().import_observation(
            city,
            target_date=target_date,
            kind=kind,
            buckets=tuple(bucket.bucket for bucket in market_buckets),
        )
    except Exception as exc:  # noqa: BLE001
        import_run_key = storage.save_settlement_import_run(
            city=city,
            target_date=target_date.isoformat(),
            kind=kind,
            provider="settlement-interface",
            station_id=city.station_id,
            status="failed",
            error_message=str(exc),
            raw_request=request_payload,
        )
        return {
            "summary": {
                "status": "failed",
                "importRunKey": import_run_key,
                "cityId": city.city_id,
                "cityName": city.name,
                "targetDate": target_date.isoformat(),
                "kind": kind,
                "stationId": city.station_id,
                "errorMessage": str(exc),
            },
            "settlement": None,
            "outcomes": [],
        }

    import_run_key = storage.save_settlement_import_run(
        city=city,
        target_date=target_date.isoformat(),
        kind=kind,
        provider=observation.source_provider,
        station_id=observation.station_id,
        status="success",
        raw_request=request_payload,
        raw_payload=observation.raw_payload,
    )
    settlement_key = storage.save_settlement(
        observation,
        import_run_key=import_run_key,
    )
    outcomes = storage.reconcile_signal_outcomes(
        city_id=city.city_id,
        target_date=target_date.isoformat(),
        kind=kind,
    )
    return {
        "summary": {
            "status": "success",
            "importRunKey": import_run_key,
            "settlementKey": settlement_key,
            "cityId": city.city_id,
            "cityName": city.name,
            "targetDate": target_date.isoformat(),
            "kind": kind,
            "stationId": observation.station_id,
            "bucketWarning": bucket_warning,
            "outcomeCount": len(outcomes),
        },
        "settlement": {
            "settlementKey": settlement_key,
            "cityId": city.city_id,
            "cityName": city.name,
            "targetDate": target_date.isoformat(),
            "kind": kind,
            "stationId": observation.station_id,
            "settlementStation": observation.settlement_station,
            "sourceProvider": observation.source_provider,
            "sourceUrl": observation.source_url,
            "observedValue": observation.observed_value,
            "unit": observation.unit,
            "bucketLabel": observation.bucket_label,
            "bucketKey": observation.bucket_key,
            "observationCount": observation.observation_count,
            "observationStart": (
                observation.observation_start.isoformat()
                if observation.observation_start
                else None
            ),
            "observationEnd": (
                observation.observation_end.isoformat()
                if observation.observation_end
                else None
            ),
        },
        "outcomes": outcomes,
    }


def settlement_reconcile_payload(payload: dict[str, Any]) -> dict[str, Any]:
    storage = WeatherStorage(payload.get("dbPath") or None, initialize=True)
    city_id = _optional_text(payload.get("city") or payload.get("cityId"))
    target_date = _optional_text(payload.get("targetDate") or payload.get("date"))
    kind = _optional_text(payload.get("temperatureKind") or payload.get("kind"))
    outcomes = storage.reconcile_signal_outcomes(
        city_id=city_id,
        target_date=target_date,
        kind=kind,
    )
    return {"summary": {"outcomeCount": len(outcomes)}, "outcomes": outcomes}


def settlements_recent_payload(payload: dict[str, Any]) -> dict[str, Any]:
    storage = WeatherStorage(payload.get("dbPath") or None)
    return {"settlements": storage.recent_settlements(limit=int(payload.get("limit") or 20))}


def signal_outcomes_payload(payload: dict[str, Any]) -> dict[str, Any]:
    storage = WeatherStorage(payload.get("dbPath") or None)
    return {"outcomes": storage.recent_signal_outcomes(limit=int(payload.get("limit") or 50))}


def calibration_payload(payload: dict[str, Any]) -> dict[str, Any]:
    storage = WeatherStorage(payload.get("dbPath") or None)
    return storage.calibration_summary()


def portfolio_payload(payload: dict[str, Any]) -> dict[str, Any]:
    positions_text = str(payload.get("positionsCsv") or "")
    unit = str(payload.get("unit") or "F")
    fee_rate = float(payload.get("feeRate") or 0.05)
    min_cashout_ratio = float(payload.get("minCashoutRatio") or 0.50)
    target_profit = float(payload.get("targetProfit") or 0.0)
    tail_probability_cutoff = float(payload.get("tailProbabilityCutoff") or 0.05)
    max_tail_probability = float(payload.get("maxTailProbability") or 0.10)

    positions = parse_inline_positions(positions_text, default_unit=unit)  # type: ignore[arg-type]
    market_buckets, market_source = _market_buckets_for_payload(
        payload,
        positions=positions,
        unit=unit,
    )
    portfolio = Portfolio(positions=positions)
    valuations = value_portfolio(portfolio, market_buckets, fee_rate=fee_rate)
    market_by_key = {item.bucket.canonical_key: item for item in market_buckets}
    lock = calculate_hedge_lock(
        portfolio,
        market_buckets,
        probabilities=probabilities_from_market_buckets(market_buckets),
        target_profit=target_profit,
        tail_probability_cutoff=tail_probability_cutoff,
        max_tail_probability=max_tail_probability,
        fee_rate=fee_rate,
    )
    exits = []
    for valuation in valuations:
        plan = generate_passive_exit_plan(
            valuation.position,
            market_by_key.get(valuation.position.bucket.canonical_key),
            fee_rate=fee_rate,
            min_cashout_ratio=min_cashout_ratio,
        )
        exits.append(
            {
                "outcome": plan.outcome,
                "action": plan.action,
                "retainedShares": plan.retained_shares,
                "ladder": [
                    {
                        "fraction": leg.fraction,
                        "shares": leg.shares,
                        "limitPrice": leg.limit_price,
                        "netValue": leg.net_value,
                        "label": leg.label,
                    }
                    for leg in plan.ladder
                ],
                "warning": plan.warning,
            }
        )
    return {
        "summary": {
            "marketSource": market_source,
            "marketCount": len(market_buckets),
            "positions": len(portfolio.positions),
            "currentCost": portfolio.total_cost,
            "markValue": portfolio_mark_value(valuations),
            "liquidationValue": portfolio_liquidation_value(valuations),
            "cashoutRatio": portfolio_cashout_ratio(valuations),
            "coveredProbability": lock.covered_probability,
            "uncoveredTailProbability": lock.uncovered_tail_probability,
            "worstCasePnl": lock.worst_case_pnl,
            "coveredWorstCasePnl": lock.covered_worst_case_pnl,
            "hedgeCost": lock.hedge_cost,
            "lockProfit": lock.lock_profit,
            "isOverround": lock.is_overround,
            "askSum": lock.ask_sum,
            "bidSum": lock.bid_sum,
            "midpointSum": lock.midpoint_sum,
            "isTrueArbitrage": lock.is_true_arbitrage,
            "isTailRiskLock": lock.is_tail_risk_lock,
            "recommendation": lock.recommendation,
            "notes": list(lock.notes),
        },
        "valuations": [
            {
                "outcome": valuation.position.outcome,
                "shares": valuation.position.shares,
                "cost": valuation.position.total_cost,
                "markPrice": valuation.mark_price,
                "bestBid": valuation.best_bid,
                "bestAsk": valuation.best_ask,
                "markValue": valuation.mark_value,
                "liquidationValue": valuation.liquidation_value,
                "cashoutRatio": valuation.cashout_ratio,
                "unrealizedMarkPnl": valuation.unrealized_mark_pnl,
                "executablePnl": valuation.executable_pnl,
            }
            for valuation in valuations
        ],
        "exits": exits,
        "hedgeLegs": [
            {
                "outcome": leg.outcome,
                "shares": leg.shares,
                "price": leg.price,
                "cost": leg.total_cost,
                "action": leg.action,
            }
            for leg in lock.hedge_legs
        ],
        "scenarios": [
            {
                "outcome": scenario.outcome,
                "probability": scenario.probability,
                "payoff": scenario.payoff,
                "totalCost": scenario.total_cost,
                "netPnl": scenario.net_pnl,
                "isCovered": scenario.is_covered,
            }
            for scenario in lock.scenarios
        ],
    }


def _paper_account_key(payload: dict[str, Any]) -> str:
    return _optional_text(payload.get("accountKey") or payload.get("account_key")) or DEFAULT_ACCOUNT_KEY


def _paper_initial_cash(payload: dict[str, Any]) -> float:
    raw = _payload_raw(payload, "initialCash", "initial_cash")
    return float(DEFAULT_INITIAL_CASH if raw is None else raw)


def _paper_stake(payload: dict[str, Any]) -> float:
    raw = _payload_raw(payload, "stakeUsdc", "stake")
    return float(DEFAULT_STAKE_USDC if raw is None else raw)


def _paper_number(payload: dict[str, Any], default: float, *keys: str) -> float:
    raw = _payload_raw(payload, *keys)
    return float(default if raw is None else raw)


def _paper_city_optional(payload: dict[str, Any]) -> CityConfig | None:
    if not (
        _optional_text(payload.get("city") or payload.get("cityId"))
        or _coordinate_payload_present(payload)
    ):
        return None
    return _city_from_payload(payload)


def _paper_context_from_payload(
    payload: dict[str, Any],
    *,
    city: CityConfig | None,
) -> dict[str, Any]:
    raw_context = payload.get("context")
    context = dict(raw_context) if isinstance(raw_context, dict) else {}
    summary = payload.get("summary")
    summary_payload = summary if isinstance(summary, dict) else {}
    target_date = _optional_text(
        payload.get("targetDate")
        or payload.get("date")
        or context.get("targetDate")
        or summary_payload.get("targetDate")
    )
    kind = _optional_text(
        payload.get("temperatureKind")
        or payload.get("kind")
        or context.get("kind")
        or summary_payload.get("kind")
    )
    if city is not None:
        context.update(
            {
                "cityId": city.city_id,
                "cityName": city.name,
                "targetDate": target_date,
                "kind": kind,
                "settlementStation": city.settlement_station,
                "stationId": city.station_id,
                "metarSource": city.metar_source,
                "runKey": summary_payload.get("runKey"),
                "marketSnapshotGroup": summary_payload.get("marketSnapshotGroup"),
            }
        )
    else:
        context.setdefault("targetDate", target_date)
        context.setdefault("kind", kind)
        if summary_payload.get("cityId"):
            context.setdefault("cityId", summary_payload.get("cityId"))
        if summary_payload.get("cityName"):
            context.setdefault("cityName", summary_payload.get("cityName"))
        if summary_payload.get("settlementStation"):
            context.setdefault("settlementStation", summary_payload.get("settlementStation"))
        if summary_payload.get("stationId"):
            context.setdefault("stationId", summary_payload.get("stationId"))
        if summary_payload.get("marketSnapshotGroup"):
            context.setdefault("marketSnapshotGroup", summary_payload.get("marketSnapshotGroup"))
        if summary_payload.get("runKey"):
            context.setdefault("runKey", summary_payload.get("runKey"))
    return context


def _paper_signal_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_signal = payload.get("signal")
    if isinstance(raw_signal, dict):
        return raw_signal
    signals = payload.get("signals")
    if isinstance(signals, list):
        buy_signals = [
            signal
            for signal in signals
            if isinstance(signal, dict) and signal.get("recommendation") == "BUY_YES"
        ]
        if buy_signals:
            return max(
                buy_signals,
                key=lambda signal: float(signal.get("edge") or -99.0),
            )
    raise ValueError("Provide signal or signals with a BUY_YES row.")


def _paper_signals_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    signals = payload.get("signals")
    if isinstance(signals, list):
        return [signal for signal in signals if isinstance(signal, dict)]
    return [_paper_signal_from_payload(payload)]


def _paper_market_buckets_from_payload(
    payload: dict[str, Any],
    *,
    city: CityConfig | None,
) -> tuple:
    unit = city.settlement_unit if city is not None else str(payload.get("unit") or "F")
    raw_market_buckets = payload.get("marketBuckets") or payload.get("markets")
    if isinstance(raw_market_buckets, list):
        rows = [row for row in raw_market_buckets if isinstance(row, dict)]
        return market_buckets_from_payload(rows, default_unit=unit)
    if str(payload.get("marketsCsv") or "").strip():
        return parse_inline_market_buckets(str(payload.get("marketsCsv")), default_unit=unit)  # type: ignore[arg-type]
    has_selector = (
        _optional_text(payload.get("marketQuery") or payload.get("query"))
        or _optional_text(payload.get("marketSlug") or payload.get("slug"))
        or _optional_text(payload.get("conditionId") or payload.get("condition_id"))
        or _optional_text(payload.get("city") or payload.get("cityId"))
    )
    if has_selector:
        return _optional_market_buckets_for_payload(
            payload,
            unit=unit,
            allow_city_selector=True,
        )
    return ()


def _paper_storage(payload: dict[str, Any]) -> WeatherStorage:
    return WeatherStorage(payload.get("dbPath") or None, initialize=True)


def paper_preview_payload(payload: dict[str, Any]) -> dict[str, Any]:
    city = _paper_city_optional(payload)
    context = _paper_context_from_payload(payload, city=city)
    market_buckets = _paper_market_buckets_from_payload(payload, city=city)
    storage = _paper_storage(payload)
    account_key = _paper_account_key(payload)
    preview = storage.paper_buy_preview(
        signal=_paper_signal_from_payload(payload),
        market_buckets=market_buckets,
        context=context,
        account_key=account_key,
        initial_cash=_paper_initial_cash(payload),
        stake_usdc=_paper_stake(payload),
        min_edge=_paper_number(payload, DEFAULT_MIN_EDGE, "minEdge"),
        fee_rate=_paper_number(payload, DEFAULT_TAKER_FEE_RATE, "feeRate"),
        max_spread=_paper_number(payload, DEFAULT_MAX_SPREAD, "maxSpread"),
        min_ask_depth_shares=_paper_number(payload, DEFAULT_MIN_ASK_DEPTH_SHARES, "minAskDepthShares"),
        max_market_exposure=_paper_number(payload, DEFAULT_MAX_MARKET_EXPOSURE, "maxMarketExposure"),
        max_city_date_exposure=_paper_number(payload, DEFAULT_MAX_CITY_DATE_EXPOSURE, "maxCityDateExposure"),
    )
    return {
        "preview": preview,
        "portfolio": storage.paper_portfolio(
            account_key=account_key,
            initial_cash=_paper_initial_cash(payload),
            market_buckets=market_buckets,
            fee_rate=_paper_number(payload, DEFAULT_TAKER_FEE_RATE, "feeRate"),
        ),
    }


def paper_buy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    city = _paper_city_optional(payload)
    context = _paper_context_from_payload(payload, city=city)
    market_buckets = _paper_market_buckets_from_payload(payload, city=city)
    storage = _paper_storage(payload)
    account_key = _paper_account_key(payload)
    result = storage.execute_paper_buy(
        signal=_paper_signal_from_payload(payload),
        market_buckets=market_buckets,
        context=context,
        account_key=account_key,
        initial_cash=_paper_initial_cash(payload),
        stake_usdc=_paper_stake(payload),
        min_edge=_paper_number(payload, DEFAULT_MIN_EDGE, "minEdge"),
        fee_rate=_paper_number(payload, DEFAULT_TAKER_FEE_RATE, "feeRate"),
        max_spread=_paper_number(payload, DEFAULT_MAX_SPREAD, "maxSpread"),
        min_ask_depth_shares=_paper_number(payload, DEFAULT_MIN_ASK_DEPTH_SHARES, "minAskDepthShares"),
        max_market_exposure=_paper_number(payload, DEFAULT_MAX_MARKET_EXPOSURE, "maxMarketExposure"),
        max_city_date_exposure=_paper_number(payload, DEFAULT_MAX_CITY_DATE_EXPOSURE, "maxCityDateExposure"),
    )
    result["portfolio"] = storage.paper_portfolio(
        account_key=account_key,
        initial_cash=_paper_initial_cash(payload),
        market_buckets=market_buckets,
        fee_rate=_paper_number(payload, DEFAULT_TAKER_FEE_RATE, "feeRate"),
    )
    return result


def paper_portfolio_payload(payload: dict[str, Any]) -> dict[str, Any]:
    city = _paper_city_optional(payload)
    market_buckets = _paper_market_buckets_from_payload(payload, city=city)
    return _paper_storage(payload).paper_portfolio(
        account_key=_paper_account_key(payload),
        initial_cash=_paper_initial_cash(payload),
        market_buckets=market_buckets,
        limit=int(payload.get("limit") or 20),
        fee_rate=_paper_number(payload, DEFAULT_TAKER_FEE_RATE, "feeRate"),
    )


def _paper_live_position_market_buckets(
    payload: dict[str, Any],
    *,
    storage: WeatherStorage,
    city: CityConfig | None,
) -> tuple[MarketBucket, ...]:
    provided = _paper_market_buckets_from_payload(payload, city=city)
    if provided:
        return provided
    account_key = _paper_account_key(payload)
    positions = storage.open_paper_positions(account_key=account_key)
    if not positions:
        return ()
    client = GammaMarketClient(
        cache_max_age_seconds=int(_paper_number(payload, 5.0, "markCacheSeconds")),
    )
    buckets: list[MarketBucket] = []
    for position in positions:
        token_id = _optional_text(position.get("token_id"))
        if not token_id:
            continue
        try:
            orderbook = client.get_orderbook(token_id)
        except MarketDataError:
            continue
        bucket = bucket_from_key(
            bucket_label=str(position.get("bucket_label") or position.get("outcome") or ""),
            bucket_key=_optional_text(position.get("bucket_key")),
            default_unit=city.settlement_unit if city is not None else str(payload.get("unit") or "F"),
        )
        buckets.append(
            MarketBucket(
                market_id=str(position.get("condition_id") or "paper-position"),
                question="Paper position live orderbook",
                slug=_optional_text(position.get("market_slug")),
                condition_id=_optional_text(position.get("condition_id")),
                outcome=str(position.get("outcome") or bucket.label),
                price=orderbook.midpoint or 0.0,
                bucket=bucket,
                token_id=token_id,
                orderbook=orderbook,
                raw_payload=dict(position),
            )
        )
    return tuple(buckets)


def paper_mark_payload(payload: dict[str, Any]) -> dict[str, Any]:
    city = _paper_city_optional(payload)
    storage = _paper_storage(payload)
    account_key = _paper_account_key(payload)
    market_buckets = _paper_live_position_market_buckets(
        payload,
        storage=storage,
        city=city,
    )
    result = storage.paper_mark_positions(
        account_key=account_key,
        initial_cash=_paper_initial_cash(payload),
        market_buckets=market_buckets,
        fee_rate=_paper_number(payload, DEFAULT_TAKER_FEE_RATE, "feeRate"),
        target_profit=_paper_number(payload, 0.10, "targetProfit"),
        min_cashout_ratio=_paper_number(payload, 0.50, "minCashoutRatio"),
    )
    result["portfolio"] = storage.paper_portfolio(
        account_key=account_key,
        initial_cash=_paper_initial_cash(payload),
        market_buckets=market_buckets,
        limit=int(payload.get("limit") or 20),
        fee_rate=_paper_number(payload, DEFAULT_TAKER_FEE_RATE, "feeRate"),
    )
    return result


def paper_reconcile_payload(payload: dict[str, Any]) -> dict[str, Any]:
    storage = _paper_storage(payload)
    result = storage.reconcile_paper_positions(
        account_key=_paper_account_key(payload),
        city_id=_optional_text(payload.get("city") or payload.get("cityId")),
        target_date=_optional_text(payload.get("targetDate") or payload.get("date")),
        kind=_optional_text(payload.get("temperatureKind") or payload.get("kind")),
    )
    result["portfolio"] = storage.paper_portfolio(
        account_key=_paper_account_key(payload),
        initial_cash=_paper_initial_cash(payload),
    )
    return result


def paper_exit_preview_payload(payload: dict[str, Any]) -> dict[str, Any]:
    city = _paper_city_optional(payload)
    market_buckets = _paper_market_buckets_from_payload(payload, city=city)
    return _paper_storage(payload).paper_exit_preview(
        account_key=_paper_account_key(payload),
        position_key=_optional_text(payload.get("positionKey") or payload.get("position_key")),
        market_buckets=market_buckets,
        shares=_payload_float(payload, "shares"),
        fee_rate=_paper_number(payload, DEFAULT_TAKER_FEE_RATE, "feeRate"),
        target_profit=_paper_number(payload, 0.10, "targetProfit"),
        signal_edge=_payload_float(payload, "signalEdge", "edge"),
        min_cashout_ratio=_paper_number(payload, 0.50, "minCashoutRatio"),
    )


def paper_hedge_preview_payload(payload: dict[str, Any]) -> dict[str, Any]:
    city = _paper_city_optional(payload)
    context = _paper_context_from_payload(payload, city=city)
    market_buckets = _paper_market_buckets_from_payload(payload, city=city)
    return _paper_storage(payload).paper_hedge_preview(
        signals=_paper_signals_from_payload(payload),
        market_buckets=market_buckets,
        account_key=_paper_account_key(payload),
        initial_cash=_paper_initial_cash(payload),
        context=context,
        stake_usdc=_paper_stake(payload),
        fee_rate=_paper_number(payload, DEFAULT_TAKER_FEE_RATE, "feeRate"),
        target_profit=_paper_number(payload, 0.0, "targetProfit"),
        tail_probability_cutoff=_paper_number(payload, 0.05, "tailProbabilityCutoff"),
        max_tail_probability=_paper_number(payload, 0.05, "maxTailProbability"),
        min_adjacent_probability=_paper_number(payload, 0.10, "minAdjacentProbability"),
    )


def llm_summary_payload(payload: dict[str, Any]) -> dict[str, Any]:
    kind = str(payload.get("kind") or "").strip()
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("result must be an object.")
    return OpenAILlmSummaryClient().summarize(kind=kind, result=result)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    handlers = {
        "cities": city_list_payload,
        "city-save": city_save_payload,
        "station-lookup": station_lookup_payload,
        "portfolio": portfolio_payload,
        "markets": market_payload,
        "forecast": forecast_payload,
        "ensemble": ensemble_payload,
        "ensemble-signal": ensemble_signal_payload,
        "llm-summary": llm_summary_payload,
        "db-runs": db_runs_payload,
        "db-probabilities": db_probabilities_payload,
        "settlement-import": settlement_import_payload,
        "settlement-reconcile": settlement_reconcile_payload,
        "settlements-recent": settlements_recent_payload,
        "signal-outcomes": signal_outcomes_payload,
        "calibration": calibration_payload,
        "paper-preview": paper_preview_payload,
        "paper-buy": paper_buy_payload,
        "paper-portfolio": paper_portfolio_payload,
        "paper-mark": paper_mark_payload,
        "paper-reconcile": paper_reconcile_payload,
        "paper-exit-preview": paper_exit_preview_payload,
        "paper-hedge-preview": paper_hedge_preview_payload,
    }
    if not args or args[0] not in handlers:
        print(
            json.dumps(
                {"error": f"supported commands: {', '.join(sorted(handlers))}"},
                ensure_ascii=False,
            )
        )
        return 2
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = handlers[args[0]](payload)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
