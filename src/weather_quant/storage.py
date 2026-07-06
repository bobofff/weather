"""Persistence helpers for weather ensemble research snapshots."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from weather_quant.config import DEFAULT_CITY_CONFIGS
from weather_quant.db import connect_database, init_database
from weather_quant.ensemble import stable_payload_hash
from weather_quant.models import CityConfig, EnsembleDistribution, MarketBucket
from weather_quant.portfolio import (
    market_best_ask,
    market_best_bid,
    market_mark_price,
    orderbook_overround,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text_time(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _city_to_payload(city: CityConfig, *, is_builtin: bool, sort_order: int) -> dict[str, Any]:
    return {
        "cityId": city.city_id,
        "name": city.name,
        "latitude": city.latitude,
        "longitude": city.longitude,
        "timezone": city.timezone,
        "settlementStation": city.settlement_station,
        "stationId": city.station_id,
        "metarSource": city.metar_source,
        "forecastGranularity": city.forecast_granularity,
        "settlementUnit": city.settlement_unit,
        "weatherModels": list(city.weather_models),
        "modelWeights": dict(city.model_weights),
        "modelErrorStd": city.model_error_std,
        "minDistributionStd": city.min_distribution_std,
        "elevation": city.elevation,
        "cellSelection": city.cell_selection,
        "isBuiltin": is_builtin,
        "sortOrder": sort_order,
    }


def _city_from_row(row: Any) -> CityConfig:
    return CityConfig(
        city_id=str(row["city_id"]),
        name=str(row["name"]),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        timezone=str(row["timezone"]),
        settlement_station=row["settlement_station"],
        station_id=row["station_id"],
        metar_source=row["metar_source"],
        forecast_granularity=row["forecast_granularity"],
        settlement_unit=row["settlement_unit"],
        weather_models=tuple(json.loads(row["weather_models_json"] or "[]")),
        model_weights=json.loads(row["model_weights_json"] or "{}"),
        model_error_std=float(row["model_error_std"]),
        min_distribution_std=float(row["min_distribution_std"]),
        elevation=row["elevation"],
        cell_selection=row["cell_selection"],
    )


def _city_payload_from_row(row: Any) -> dict[str, Any]:
    return _city_to_payload(
        _city_from_row(row),
        is_builtin=bool(row["is_builtin"]),
        sort_order=int(row["sort_order"]),
    )


class WeatherStorage:
    """SQLite-backed storage for ensemble runs and trading snapshots."""

    def __init__(self, path: Path | str | None = None, *, initialize: bool = False) -> None:
        self.path = Path(path).expanduser() if path else None
        if initialize:
            init_database(self.path)

    def _ensure_schema(self) -> None:
        init_database(self.path)

    def _seed_default_cities(self) -> None:
        self._ensure_schema()
        timestamp = _now()
        with connect_database(self.path) as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO weather_cities (
                  city_id, name, latitude, longitude, timezone, settlement_station,
                  station_id, metar_source, forecast_granularity, settlement_unit,
                  weather_models_json, model_weights_json, model_error_std,
                  min_distribution_std, elevation, cell_selection, is_builtin,
                  sort_order, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        city.city_id,
                        city.name,
                        city.latitude,
                        city.longitude,
                        city.timezone,
                        city.settlement_station,
                        city.station_id,
                        city.metar_source,
                        city.forecast_granularity,
                        city.settlement_unit,
                        _json(list(city.weather_models)),
                        _json(dict(city.model_weights)),
                        city.model_error_std,
                        city.min_distribution_std,
                        city.elevation,
                        city.cell_selection,
                        1,
                        index,
                        timestamp,
                        timestamp,
                    )
                    for index, city in enumerate(DEFAULT_CITY_CONFIGS.values())
                ],
            )
            connection.commit()

    def list_cities(self) -> list[dict[str, Any]]:
        self._seed_default_cities()
        with connect_database(self.path) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM weather_cities
                ORDER BY sort_order ASC, name COLLATE NOCASE ASC
                """
            ).fetchall()
        return [_city_payload_from_row(row) for row in rows]

    def get_city(self, city_id_or_name: str) -> CityConfig | None:
        self._seed_default_cities()
        text = city_id_or_name.strip()
        with connect_database(self.path) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM weather_cities
                WHERE lower(city_id) = lower(?) OR lower(name) = lower(?)
                LIMIT 1
                """,
                (text, text),
            ).fetchone()
        return _city_from_row(row) if row else None

    def save_city(self, city: CityConfig, *, is_builtin: bool | None = None) -> dict[str, Any]:
        self._seed_default_cities()
        timestamp = _now()
        with connect_database(self.path) as connection:
            existing = connection.execute(
                "SELECT is_builtin, sort_order, created_at FROM weather_cities WHERE city_id = ?",
                (city.city_id,),
            ).fetchone()
            builtin_value = int(is_builtin) if is_builtin is not None else (
                int(existing["is_builtin"]) if existing else 0
            )
            sort_order = int(existing["sort_order"]) if existing else 10_000
            created_at = str(existing["created_at"]) if existing else timestamp
            connection.execute(
                """
                INSERT INTO weather_cities (
                  city_id, name, latitude, longitude, timezone, settlement_station,
                  station_id, metar_source, forecast_granularity, settlement_unit,
                  weather_models_json, model_weights_json, model_error_std,
                  min_distribution_std, elevation, cell_selection, is_builtin,
                  sort_order, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(city_id) DO UPDATE SET
                  name = excluded.name,
                  latitude = excluded.latitude,
                  longitude = excluded.longitude,
                  timezone = excluded.timezone,
                  settlement_station = excluded.settlement_station,
                  station_id = excluded.station_id,
                  metar_source = excluded.metar_source,
                  forecast_granularity = excluded.forecast_granularity,
                  settlement_unit = excluded.settlement_unit,
                  weather_models_json = excluded.weather_models_json,
                  model_weights_json = excluded.model_weights_json,
                  model_error_std = excluded.model_error_std,
                  min_distribution_std = excluded.min_distribution_std,
                  elevation = excluded.elevation,
                  cell_selection = excluded.cell_selection,
                  is_builtin = excluded.is_builtin,
                  sort_order = excluded.sort_order,
                  updated_at = excluded.updated_at
                """,
                (
                    city.city_id,
                    city.name,
                    city.latitude,
                    city.longitude,
                    city.timezone,
                    city.settlement_station,
                    city.station_id,
                    city.metar_source,
                    city.forecast_granularity,
                    city.settlement_unit,
                    _json(list(city.weather_models)),
                    _json(dict(city.model_weights)),
                    city.model_error_std,
                    city.min_distribution_std,
                    city.elevation,
                    city.cell_selection,
                    builtin_value,
                    sort_order,
                    created_at,
                    timestamp,
                ),
            )
            connection.commit()
        row = self.get_city(city.city_id)
        if row is None:
            raise RuntimeError(f"Saved city not found: {city.city_id}")
        return _city_to_payload(row, is_builtin=bool(builtin_value), sort_order=sort_order)

    def save_distribution(self, distribution: EnsembleDistribution) -> str:
        self._ensure_schema()
        run = distribution.run
        created_at = _now()
        with connect_database(self.path) as connection:
            connection.execute(
                """
                INSERT INTO weather_ensemble_runs (
                  run_key, provider, model, run_time, initialization_time, city_id,
                  target_date, kind, latitude, longitude, timezone, settlement_station,
                  station_id, metar_source, forecast_granularity, member_count,
                  fetched_at, raw_payload_json, payload_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_key,
                    run.provider,
                    run.model,
                    _text_time(run.run_time),
                    _text_time(run.initialization_time),
                    run.city.city_id,
                    run.target_date.isoformat(),
                    run.kind,
                    run.city.latitude,
                    run.city.longitude,
                    run.city.timezone,
                    run.city.settlement_station,
                    run.city.station_id,
                    run.city.metar_source,
                    run.city.forecast_granularity,
                    run.member_count,
                    run.fetched_at.isoformat(),
                    _json(run.raw_payload),
                    run.payload_hash,
                ),
            )
            connection.executemany(
                """
                INSERT INTO weather_ensemble_members (
                  run_key, member_id, target_date, kind, daily_value, unit,
                  bucket_label, bucket_key, raw_hourly_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run.run_key,
                        value.member_id,
                        value.target_date.isoformat(),
                        value.kind,
                        value.value,
                        value.unit,
                        value.bucket_label,
                        value.bucket_key,
                        _json(
                            {
                                "time": value.hourly_times,
                                "temperature_2m": value.hourly_values,
                            }
                        ),
                        created_at,
                    )
                    for value in distribution.member_values
                ],
            )
            connection.executemany(
                """
                INSERT INTO weather_bucket_probabilities (
                  run_key, bucket_label, bucket_key, hit_count, probability,
                  total_members, unmatched_count, empirical_mean, empirical_std,
                  p10, p50, p90, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run.run_key,
                        item.bucket.label,
                        item.bucket.canonical_key,
                        item.hit_count,
                        item.probability,
                        item.total_members,
                        item.unmatched_count,
                        item.empirical_mean,
                        item.empirical_std,
                        item.p10,
                        item.p50,
                        item.p90,
                        created_at,
                    )
                    for item in distribution.probabilities
                ],
            )
            connection.commit()
        return run.run_key

    def save_market_snapshots(self, market_buckets: Sequence[MarketBucket]) -> str | None:
        self._ensure_schema()
        if not market_buckets:
            return None
        fetched_at = _now()
        overround = orderbook_overround(market_buckets)
        group_payload = [
            {
                "slug": bucket.slug,
                "condition_id": bucket.condition_id,
                "outcome": bucket.outcome,
                "token_id": bucket.token_id,
                "price": bucket.price,
                "bucket_key": bucket.bucket.canonical_key,
            }
            for bucket in market_buckets
        ]
        group_key = f"market:{stable_payload_hash({'fetched_at': fetched_at, 'buckets': group_payload})[:20]}"
        with connect_database(self.path) as connection:
            connection.executemany(
                """
                INSERT INTO weather_market_snapshots (
                  market_snapshot_group, market_slug, condition_id, outcome,
                  token_id, bucket_label, bucket_key, price, best_bid, best_ask,
                  midpoint, spread, ask_sum, bid_sum, midpoint_sum, is_overround,
                  fetched_at, raw_payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        group_key,
                        bucket.slug,
                        bucket.condition_id,
                        bucket.outcome,
                        bucket.token_id,
                        bucket.bucket.label,
                        bucket.bucket.canonical_key,
                        bucket.price,
                        market_best_bid(bucket),
                        market_best_ask(bucket),
                        market_mark_price(bucket),
                        bucket.orderbook.spread if bucket.orderbook else None,
                        float(overround["ask_sum"]),
                        float(overround["bid_sum"]),
                        float(overround["midpoint_sum"]),
                        1 if overround["is_overround"] else 0,
                        fetched_at,
                        _json(bucket.raw_payload),
                    )
                    for bucket in market_buckets
                ],
            )
            connection.commit()
        return group_key

    def save_signal_snapshots(
        self,
        *,
        run_key: str,
        rows: Sequence[dict[str, Any]],
        market_snapshot_group: str | None = None,
    ) -> None:
        self._ensure_schema()
        if not rows:
            return
        created_at = _now()
        with connect_database(self.path) as connection:
            connection.executemany(
                """
                INSERT INTO weather_signal_snapshots (
                  run_key, market_snapshot_group, outcome, bucket_key,
                  ensemble_probability, market_midpoint, best_bid, best_ask,
                  executable_entry_cost, fee, expected_exit_cost, edge,
                  recommendation, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_key,
                        market_snapshot_group,
                        row["outcome"],
                        row["bucketKey"],
                        row["ensembleProbability"],
                        row.get("marketMidpoint"),
                        row.get("bestBid"),
                        row.get("bestAsk"),
                        row.get("executableEntryCost"),
                        row.get("fee"),
                        row.get("expectedExitCost"),
                        row["edge"],
                        row["recommendation"],
                        created_at,
                    )
                    for row in rows
                ],
            )
            connection.commit()

    def recent_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        self._ensure_schema()
        with connect_database(self.path) as connection:
            rows = connection.execute(
                """
                SELECT run_key, provider, model, city_id, target_date, kind,
                       member_count, fetched_at, settlement_station, station_id
                FROM weather_ensemble_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_probabilities(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self._ensure_schema()
        with connect_database(self.path) as connection:
            rows = connection.execute(
                """
                SELECT run_key, bucket_label, bucket_key, hit_count, probability,
                       total_members, unmatched_count, empirical_mean,
                       empirical_std, p10, p50, p90, created_at
                FROM weather_bucket_probabilities
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
