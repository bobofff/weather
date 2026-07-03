"""Configuration helpers for city and market setup."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from weather_quant.buckets import build_regular_buckets
from weather_quant.models import CityConfig, TemperatureBucket
from weather_quant.units import normalize_unit


DEFAULT_CITY_CONFIGS: dict[str, CityConfig] = {
    "shanghai": CityConfig(
        city_id="shanghai",
        name="Shanghai",
        latitude=31.2304,
        longitude=121.4737,
        timezone="Asia/Shanghai",
        settlement_station="Configurable official Shanghai settlement station",
        forecast_granularity="city",
        settlement_unit="C",
        weather_models=("ecmwf_ifs025", "gfs_seamless", "ukmo_seamless"),
        model_error_std=1.4,
        min_distribution_std=0.8,
    ),
    "hong-kong": CityConfig(
        city_id="hong-kong",
        name="Hong Kong",
        latitude=22.3193,
        longitude=114.1694,
        timezone="Asia/Hong_Kong",
        settlement_station="Hong Kong Observatory or configured market station",
        forecast_granularity="city",
        settlement_unit="C",
        weather_models=("ecmwf_ifs025", "gfs_seamless", "ukmo_seamless"),
        model_error_std=1.3,
        min_distribution_std=0.8,
    ),
    "tokyo": CityConfig(
        city_id="tokyo",
        name="Tokyo",
        latitude=35.6762,
        longitude=139.6503,
        timezone="Asia/Tokyo",
        settlement_station="Tokyo official settlement station",
        forecast_granularity="city",
        settlement_unit="C",
        weather_models=("ecmwf_ifs025", "gfs_seamless", "ukmo_seamless"),
        model_error_std=1.3,
        min_distribution_std=0.8,
    ),
    "new-york": CityConfig(
        city_id="new-york",
        name="New York",
        latitude=40.7128,
        longitude=-74.0060,
        timezone="America/New_York",
        settlement_station="Central Park / configured market station",
        station_id="KNYC",
        metar_source="NOAA/METAR",
        forecast_granularity="station",
        settlement_unit="F",
        weather_models=("ecmwf_ifs025", "gfs_seamless", "ukmo_seamless", "nws"),
        model_weights={"nws": 0.8},
        model_error_std=2.5,
        min_distribution_std=1.0,
    ),
    "london": CityConfig(
        city_id="london",
        name="London",
        latitude=51.5072,
        longitude=-0.1276,
        timezone="Europe/London",
        settlement_station="London official settlement station",
        forecast_granularity="city",
        settlement_unit="C",
        weather_models=(
            "ecmwf_ifs025",
            "icon_seamless",
            "meteofrance_seamless",
            "gfs_seamless",
            "ukmo_seamless",
        ),
        model_error_std=1.4,
        min_distribution_std=0.8,
    ),
    "munich": CityConfig(
        city_id="munich",
        name="Munich",
        latitude=48.1372,
        longitude=11.5756,
        timezone="Europe/Berlin",
        settlement_station="Munich official settlement station",
        forecast_granularity="city",
        settlement_unit="C",
        weather_models=(
            "ecmwf_ifs025",
            "icon_seamless",
            "meteofrance_seamless",
            "gfs_seamless",
            "ukmo_seamless",
        ),
        model_error_std=1.4,
        min_distribution_std=0.8,
    ),
    "moscow": CityConfig(
        city_id="moscow",
        name="Moscow",
        latitude=55.7558,
        longitude=37.6173,
        timezone="Europe/Moscow",
        settlement_station="Moscow official settlement station",
        forecast_granularity="city",
        settlement_unit="C",
        weather_models=(
            "ecmwf_ifs025",
            "icon_seamless",
            "meteofrance_seamless",
            "gfs_seamless",
            "ukmo_seamless",
        ),
        model_error_std=1.6,
        min_distribution_std=0.9,
    ),
    "ankara": CityConfig(
        city_id="ankara",
        name="Ankara",
        latitude=39.9334,
        longitude=32.8597,
        timezone="Europe/Istanbul",
        settlement_station="Ankara official settlement station",
        forecast_granularity="city",
        settlement_unit="C",
        weather_models=(
            "ecmwf_ifs025",
            "icon_seamless",
            "meteofrance_seamless",
            "gfs_seamless",
            "ukmo_seamless",
        ),
        model_error_std=1.5,
        min_distribution_std=0.8,
    ),
    "las-vegas": CityConfig(
        city_id="las-vegas",
        name="Las Vegas",
        latitude=36.1699,
        longitude=-115.1398,
        timezone="America/Los_Angeles",
        settlement_station="Las Vegas official settlement station",
        station_id="KLAS",
        metar_source="NOAA/METAR",
        forecast_granularity="city",
        settlement_unit="F",
        weather_models=("ecmwf_ifs025", "gfs_seamless", "ukmo_seamless"),
        model_error_std=2.6,
        min_distribution_std=1.0,
    ),
}


@dataclass(frozen=True)
class WeatherMarketConfig:
    city_id: str
    target_date: str | None = None
    kind: str = "high"
    market_slug: str | None = None
    market_query: str | None = None
    condition_id: str | None = None
    fallback_buckets: tuple[TemperatureBucket, ...] = ()


@dataclass(frozen=True)
class WeatherTradingConfig:
    cities: Mapping[str, CityConfig] = field(default_factory=lambda: DEFAULT_CITY_CONFIGS)
    markets: tuple[WeatherMarketConfig, ...] = ()
    cache_max_age_seconds: int = 900
    open_meteo_base_url: str = "https://api.open-meteo.com"
    open_meteo_ensemble_base_url: str = "https://ensemble-api.open-meteo.com"
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"
    request_timeout_seconds: float = 30.0


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "Loading YAML config requires PyYAML. Install pyyaml or use JSON config."
        ) from exc
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _read_mapping(path: Path) -> Mapping[str, Any]:
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        data = _load_yaml(path)
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError("Config root must be a mapping.")
    return data


def city_from_mapping(city_id: str, payload: Mapping[str, Any]) -> CityConfig:
    unit = normalize_unit(str(payload.get("settlement_unit", "F")))
    models = payload.get("weather_models") or payload.get("models") or ()
    if isinstance(models, str):
        model_tuple = tuple(item.strip() for item in models.split(",") if item.strip())
    else:
        model_tuple = tuple(str(item).strip() for item in models if str(item).strip())
    if not model_tuple:
        model_tuple = (
            "ecmwf_ifs025",
            "icon_seamless",
            "meteofrance_seamless",
            "gfs_seamless",
            "ukmo_seamless",
        )

    weights_payload = payload.get("model_weights") or {}
    weights = {
        str(key): float(value)
        for key, value in weights_payload.items()
    } if isinstance(weights_payload, Mapping) else {}

    return CityConfig(
        city_id=city_id,
        name=str(payload.get("name") or city_id),
        latitude=float(payload["latitude"]),
        longitude=float(payload["longitude"]),
        timezone=str(payload.get("timezone") or "auto"),
        settlement_station=(
            str(payload["settlement_station"]) if payload.get("settlement_station") else None
        ),
        station_id=str(payload["station_id"]) if payload.get("station_id") else None,
        metar_source=str(payload["metar_source"]) if payload.get("metar_source") else None,
        forecast_granularity=(
            "station"
            if str(payload.get("forecast_granularity") or "").strip().lower() == "station"
            else "city"
        ),
        settlement_unit=unit,
        weather_models=model_tuple,
        model_weights=weights,
        model_error_std=float(payload.get("model_error_std", 2.5)),
        min_distribution_std=float(payload.get("min_distribution_std", 1.0)),
    )


def _fallback_buckets_from_mapping(
    payload: Mapping[str, Any],
    *,
    unit: str,
) -> tuple[TemperatureBucket, ...]:
    raw = payload.get("fallback_buckets")
    if not isinstance(raw, Mapping):
        return ()
    return build_regular_buckets(
        start=int(raw["start"]),
        end=int(raw["end"]),
        step=int(raw.get("step", 1)),
        unit=normalize_unit(str(raw.get("unit", unit))),
        include_tails=bool(raw.get("include_tails", True)),
    )


def market_from_mapping(payload: Mapping[str, Any]) -> WeatherMarketConfig:
    city_id = str(payload["city_id"])
    fallback_unit = str(payload.get("unit") or "F")
    return WeatherMarketConfig(
        city_id=city_id,
        target_date=str(payload["target_date"]) if payload.get("target_date") else None,
        kind=str(payload.get("kind") or "high"),
        market_slug=str(payload["market_slug"]) if payload.get("market_slug") else None,
        market_query=str(payload["market_query"]) if payload.get("market_query") else None,
        condition_id=str(payload["condition_id"]) if payload.get("condition_id") else None,
        fallback_buckets=_fallback_buckets_from_mapping(payload, unit=fallback_unit),
    )


def load_trading_config(path: Path | None = None) -> WeatherTradingConfig:
    if path is None:
        return WeatherTradingConfig()

    payload = _read_mapping(path)
    cities = dict(DEFAULT_CITY_CONFIGS)
    raw_cities = payload.get("cities") or {}
    if isinstance(raw_cities, Mapping):
        for city_id, city_payload in raw_cities.items():
            if not isinstance(city_payload, Mapping):
                continue
            cities[str(city_id)] = city_from_mapping(str(city_id), city_payload)

    markets: list[WeatherMarketConfig] = []
    raw_markets = payload.get("markets") or []
    if isinstance(raw_markets, list):
        for market_payload in raw_markets:
            if isinstance(market_payload, Mapping):
                markets.append(market_from_mapping(market_payload))

    return WeatherTradingConfig(
        cities=cities,
        markets=tuple(markets),
        cache_max_age_seconds=int(payload.get("cache_max_age_seconds", 900)),
        open_meteo_base_url=str(
            payload.get("open_meteo_base_url") or "https://api.open-meteo.com"
        ).rstrip("/"),
        open_meteo_ensemble_base_url=str(
            payload.get("open_meteo_ensemble_base_url")
            or "https://ensemble-api.open-meteo.com"
        ).rstrip("/"),
        gamma_base_url=str(
            payload.get("gamma_base_url") or "https://gamma-api.polymarket.com"
        ).rstrip("/"),
        clob_base_url=str(
            payload.get("clob_base_url") or "https://clob.polymarket.com"
        ).rstrip("/"),
        request_timeout_seconds=float(payload.get("request_timeout_seconds", 30.0)),
    )


def get_city(city_id: str, config: WeatherTradingConfig | None = None) -> CityConfig:
    trading_config = config or WeatherTradingConfig()
    normalized = city_id.strip().lower()
    aliases = {
        "nyc": "new-york",
        "new york": "new-york",
        "hk": "hong-kong",
        "hong kong": "hong-kong",
        "上海": "shanghai",
        "香港": "hong-kong",
        "东京": "tokyo",
        "纽约": "new-york",
        "伦敦": "london",
        "las vegas": "las-vegas",
        "lv": "las-vegas",
        "慕尼黑": "munich",
        "莫斯科": "moscow",
        "安卡拉": "ankara",
        "拉斯维加斯": "las-vegas",
    }
    key = aliases.get(normalized, normalized)
    if key not in trading_config.cities:
        raise KeyError(f"Unknown city config: {city_id}")
    return trading_config.cities[key]
