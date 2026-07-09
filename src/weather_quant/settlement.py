"""Settlement observation import helpers for weather markets."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from weather_quant.cache import FileCache
from weather_quant.http import JsonHttpClient
from weather_quant.models import CityConfig, TemperatureBucket, TemperatureKind, TemperatureUnit
from weather_quant.runtime_logs import log_external_api_failure, log_sync_event
from weather_quant.units import convert_temperature


class SettlementImportError(RuntimeError):
    """Raised when a settlement observation cannot be imported."""


@dataclass(frozen=True)
class SettlementObservation:
    """One imported high/low settlement observation."""

    city: CityConfig
    target_date: date
    kind: TemperatureKind
    observed_value: float
    unit: TemperatureUnit
    source_provider: str
    source_url: str | None
    station_id: str | None
    settlement_station: str | None
    observation_count: int
    observation_start: datetime | None = None
    observation_end: datetime | None = None
    bucket_label: str | None = None
    bucket_key: str | None = None
    raw_payload: Mapping[str, Any] = field(default_factory=dict)


def bucket_from_canonical_key(
    *,
    label: str,
    key: str,
) -> TemperatureBucket | None:
    """Rebuild a temperature bucket from its canonical storage key."""

    parts = str(key or "").split(":")
    if len(parts) != 3 or parts[0] not in {"C", "F"}:
        return None
    lower = None if parts[1] == "-inf" else float(parts[1])
    upper = None if parts[2] == "inf" else float(parts[2])
    return TemperatureBucket(
        label=label,
        lower=lower,
        upper=upper,
        unit=parts[0],  # type: ignore[arg-type]
    )


def bucket_key_contains_value(
    *,
    bucket_key: str,
    bucket_label: str,
    observed_value: float,
    observed_unit: TemperatureUnit,
) -> bool:
    bucket = bucket_from_canonical_key(label=bucket_label, key=bucket_key)
    if bucket is None:
        return False
    converted = convert_temperature(
        observed_value,
        from_unit=observed_unit,
        to_unit=bucket.unit,
    )
    return bucket.contains(converted)


def settlement_bucket_for_value(
    *,
    observed_value: float,
    observed_unit: TemperatureUnit,
    buckets: Sequence[TemperatureBucket],
) -> TemperatureBucket | None:
    for bucket in buckets:
        converted = convert_temperature(
            observed_value,
            from_unit=observed_unit,
            to_unit=bucket.unit,
        )
        if bucket.contains(converted):
            return bucket
    return None


def _timezone_for_city(city: CityConfig) -> ZoneInfo | timezone:
    if city.timezone and city.timezone.lower() not in {"auto", "gmt", "utc"}:
        try:
            return ZoneInfo(city.timezone)
        except ZoneInfoNotFoundError:
            return timezone.utc
    return timezone.utc


def _parse_observation_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _float_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _temperature_from_metar(item: Mapping[str, Any]) -> tuple[float, TemperatureUnit] | None:
    for key in ("temp", "tempC", "temp_c", "temperatureC", "temperature_c"):
        value = _float_value(item.get(key))
        if value is not None:
            return value, "C"
    for key in ("tmpf", "tempF", "temp_f", "temperatureF", "temperature_f"):
        value = _float_value(item.get(key))
        if value is not None:
            return value, "F"
    return None


def _metar_items(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping) and isinstance(payload.get("data"), list):
        return [item for item in payload["data"] if isinstance(item, Mapping)]
    if isinstance(payload, Mapping) and isinstance(payload.get("features"), list):
        items: list[Mapping[str, Any]] = []
        for feature in payload["features"]:
            if isinstance(feature, Mapping) and isinstance(feature.get("properties"), Mapping):
                items.append(feature["properties"])
        return items
    if isinstance(payload, Mapping):
        return [payload]
    return []


def _metar_observations_for_date(
    payloads: Sequence[Any],
    *,
    tzinfo: ZoneInfo | timezone,
    target_date: date,
    settlement_unit: TemperatureUnit,
) -> tuple[list[tuple[datetime, float]], list[TemperatureUnit]]:
    observations: list[tuple[datetime, float]] = []
    source_units: list[TemperatureUnit] = []
    seen: set[tuple[str, float]] = set()
    for payload in payloads:
        for item in _metar_items(payload):
            observed_at = _parse_observation_time(
                item.get("obsTime")
                or item.get("reportTime")
                or item.get("time")
                or item.get("valid_time")
                or item.get("receiptTime")
            )
            temperature = _temperature_from_metar(item)
            if observed_at is None or temperature is None:
                continue
            if observed_at.astimezone(tzinfo).date() != target_date:
                continue
            value, unit = temperature
            converted = convert_temperature(
                value,
                from_unit=unit,
                to_unit=settlement_unit,
            )
            dedupe_key = (observed_at.isoformat(), converted)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            observations.append((observed_at, converted))
            source_units.append(unit)
    observations.sort(key=lambda item: item[0])
    return observations, source_units


class AviationWeatherMetarSettlementClient:
    """Import daily high/low observations from a METAR JSON endpoint."""

    def __init__(
        self,
        *,
        base_url: str = "https://aviationweather.gov",
        http_client: JsonHttpClient | None = None,
        cache: FileCache | None = None,
        cache_max_age_seconds: int = 900,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http_client or JsonHttpClient(base_url=self.base_url)
        self.cache = cache or FileCache()
        self.cache_max_age_seconds = cache_max_age_seconds

    def fetch_observation(
        self,
        city: CityConfig,
        *,
        target_date: date,
        kind: TemperatureKind,
        buckets: Sequence[TemperatureBucket] = (),
    ) -> SettlementObservation:
        station_id = str(city.station_id or "").strip().upper()
        if not station_id:
            raise SettlementImportError(
                f"{city.name} 缺少 stationId，不能自动接口导入结算观测。"
            )

        tzinfo = _timezone_for_city(city)
        start_local = datetime.combine(target_date, time.min, tzinfo=tzinfo)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)
        params = {
            "ids": station_id,
            "format": "json",
            "start": start_utc.strftime("%Y%m%d_%H%M"),
            "end": end_utc.strftime("%Y%m%d_%H%M"),
        }
        path = "/api/data/metar"
        url = self._source_url(path, params)
        cache_key = {"provider": "aviationweather-metar", "path": path, "params": params}
        payload = self.cache.get(cache_key, max_age_seconds=self.cache_max_age_seconds)
        if payload is None:
            try:
                payload = self.http.get_json(path, params=params)
            except Exception as exc:
                log_external_api_failure(
                    provider="aviationweather-metar",
                    action="fetch_settlement_observation",
                    endpoint=path,
                    details={
                        "city": city.city_id,
                        "date": target_date.isoformat(),
                        "kind": kind,
                        "stationId": station_id,
                    },
                    error=exc,
                )
                raise SettlementImportError(
                    f"结算观测接口请求失败: {exc}"
                ) from exc
            self.cache.set(cache_key, payload)

        payloads: list[Any] = [payload]
        observations, source_units = _metar_observations_for_date(
            payloads,
            tzinfo=tzinfo,
            target_date=target_date,
            settlement_unit=city.settlement_unit,
        )
        fallback_requests: list[dict[str, str]] = []
        if not observations:
            cursor = start_utc
            while cursor < end_utc:
                fallback_params = {
                    "ids": station_id,
                    "format": "json",
                    "date": cursor.strftime("%Y%m%d_%H%M"),
                }
                fallback_requests.append(dict(fallback_params))
                fallback_cache_key = {
                    "provider": "aviationweather-metar",
                    "path": path,
                    "params": fallback_params,
                }
                fallback_payload = self.cache.get(
                    fallback_cache_key,
                    max_age_seconds=self.cache_max_age_seconds,
                )
                if fallback_payload is None:
                    try:
                        fallback_payload = self.http.get_json(path, params=fallback_params)
                    except Exception as exc:
                        log_external_api_failure(
                            provider="aviationweather-metar",
                            action="fetch_settlement_observation",
                            endpoint=path,
                            details={
                                "city": city.city_id,
                                "date": target_date.isoformat(),
                                "kind": kind,
                                "stationId": station_id,
                                "requestDate": fallback_params["date"],
                            },
                            error=exc,
                        )
                        cursor += timedelta(minutes=30)
                        continue
                    self.cache.set(fallback_cache_key, fallback_payload)
                payloads.append(fallback_payload)
                cursor += timedelta(minutes=30)
            observations, source_units = _metar_observations_for_date(
                payloads,
                tzinfo=tzinfo,
                target_date=target_date,
                settlement_unit=city.settlement_unit,
            )

        if not observations:
            log_sync_event(
                source="polymarket_weather",
                action="fetch_settlement_observation",
                status="fail",
                details={
                    "city": city.city_id,
                    "date": target_date.isoformat(),
                    "kind": kind,
                    "stationId": station_id,
                    "provider": "aviationweather-metar",
                    "endpoint": path,
                    "requestStart": params["start"],
                    "requestEnd": params["end"],
                },
                result={
                    "itemCount": sum(len(_metar_items(item)) for item in payloads),
                    "usableObservationCount": 0,
                    "fallbackRequestCount": len(fallback_requests),
                },
            )
            raise SettlementImportError(
                f"{station_id} 在 {target_date.isoformat()} 没有可用温度观测。"
            )

        values = [value for _observed_at, value in observations]
        observed_value = max(values) if kind == "high" else min(values)
        bucket = settlement_bucket_for_value(
            observed_value=observed_value,
            observed_unit=city.settlement_unit,
            buckets=buckets,
        )
        observation_times = [observed_at for observed_at, _value in observations]
        return SettlementObservation(
            city=city,
            target_date=target_date,
            kind=kind,
            observed_value=observed_value,
            unit=city.settlement_unit,
            source_provider="aviationweather-metar",
            source_url=url,
            station_id=station_id,
            settlement_station=city.settlement_station,
            observation_count=len(observations),
            observation_start=min(observation_times),
            observation_end=max(observation_times),
            bucket_label=bucket.label if bucket else None,
            bucket_key=bucket.canonical_key if bucket else None,
            raw_payload={
                "request": params,
                "fallbackRequests": fallback_requests,
                "sourceUnits": source_units,
                "payload": payloads,
            },
        )

    def _source_url(self, path: str, params: Mapping[str, Any]) -> str:
        query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
        return f"{self.base_url}{path}?{query}"


class SettlementImporter:
    """High-level settlement import facade."""

    def __init__(
        self,
        *,
        metar_client: AviationWeatherMetarSettlementClient | None = None,
    ) -> None:
        self.metar_client = metar_client or AviationWeatherMetarSettlementClient()

    def import_observation(
        self,
        city: CityConfig,
        *,
        target_date: date,
        kind: TemperatureKind,
        buckets: Sequence[TemperatureBucket] = (),
    ) -> SettlementObservation:
        source = str(city.metar_source or "").upper()
        if city.station_id and (not source or "METAR" in source or "NOAA" in source):
            return self.metar_client.fetch_observation(
                city,
                target_date=target_date,
                kind=kind,
                buckets=buckets,
            )
        raise SettlementImportError(
            f"{city.name} 缺少可用结算观测接口配置，请先配置 stationId/metarSource。"
        )
