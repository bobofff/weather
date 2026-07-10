"""Persistence helpers for weather ensemble research snapshots."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from weather_quant.config import DEFAULT_CITY_CONFIGS
from weather_quant.db import connect_database, init_database
from weather_quant.ensemble import stable_payload_hash
from weather_quant.models import CityConfig, EnsembleDistribution, MarketBucket
from weather_quant.paper_trading import (
    DEFAULT_ACCOUNT_KEY,
    DEFAULT_INITIAL_CASH,
    DEFAULT_MAX_CITY_DATE_EXPOSURE,
    DEFAULT_MAX_MARKET_EXPOSURE,
    DEFAULT_MAX_SPREAD,
    DEFAULT_MIN_ASK_DEPTH_SHARES,
    DEFAULT_MIN_EDGE,
    DEFAULT_STAKE_USDC,
    exit_preview,
    find_market_bucket,
    hedge_preview,
    optional_text,
    optional_float,
    paper_buy_preview,
    paper_position_key,
    position_from_mapping,
    safe_float,
)
from weather_quant.portfolio import (
    market_best_ask,
    market_best_bid,
    market_mark_price,
    orderbook_overround,
    value_position,
)
from weather_quant.settlement import (
    SettlementObservation,
    bucket_key_contains_value,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text_time(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _average(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


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

    def save_settlement_import_run(
        self,
        *,
        city: CityConfig,
        target_date: str,
        kind: str,
        provider: str,
        station_id: str | None,
        status: str,
        error_message: str | None = None,
        raw_request: Mapping[str, Any] | None = None,
        raw_payload: Mapping[str, Any] | None = None,
    ) -> str:
        self._ensure_schema()
        timestamp = _now()
        import_run_key = (
            "settlement-import:"
            + stable_payload_hash(
                {
                    "city_id": city.city_id,
                    "target_date": target_date,
                    "kind": kind,
                    "provider": provider,
                    "station_id": station_id,
                    "timestamp": timestamp,
                }
            )[:20]
        )
        with connect_database(self.path) as connection:
            connection.execute(
                """
                INSERT INTO weather_settlement_import_runs (
                  import_run_key, city_id, target_date, kind, provider, station_id,
                  status, started_at, finished_at, error_message,
                  raw_request_json, raw_payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    import_run_key,
                    city.city_id,
                    target_date,
                    kind,
                    provider,
                    station_id,
                    status,
                    timestamp,
                    timestamp,
                    error_message,
                    _json(raw_request or {}),
                    _json(raw_payload or {}),
                ),
            )
            connection.commit()
        return import_run_key

    def save_settlement(
        self,
        observation: SettlementObservation,
        *,
        import_run_key: str | None,
    ) -> str:
        self._ensure_schema()
        settlement_key = ":".join(
            [
                "settlement",
                observation.source_provider,
                observation.city.city_id,
                observation.target_date.isoformat(),
                observation.kind,
                observation.station_id or "point",
            ]
        )
        imported_at = _now()
        with connect_database(self.path) as connection:
            connection.execute(
                """
                INSERT INTO weather_settlements (
                  settlement_key, import_run_key, city_id, city_name, target_date,
                  kind, station_id, settlement_station, source_provider, source_url,
                  observed_value, unit, bucket_label, bucket_key, observation_count,
                  observation_start, observation_end, status, imported_at,
                  raw_payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(settlement_key) DO UPDATE SET
                  import_run_key = excluded.import_run_key,
                  city_name = excluded.city_name,
                  station_id = excluded.station_id,
                  settlement_station = excluded.settlement_station,
                  source_provider = excluded.source_provider,
                  source_url = excluded.source_url,
                  observed_value = excluded.observed_value,
                  unit = excluded.unit,
                  bucket_label = excluded.bucket_label,
                  bucket_key = excluded.bucket_key,
                  observation_count = excluded.observation_count,
                  observation_start = excluded.observation_start,
                  observation_end = excluded.observation_end,
                  status = excluded.status,
                  imported_at = excluded.imported_at,
                  raw_payload_json = excluded.raw_payload_json
                """,
                (
                    settlement_key,
                    import_run_key,
                    observation.city.city_id,
                    observation.city.name,
                    observation.target_date.isoformat(),
                    observation.kind,
                    observation.station_id,
                    observation.settlement_station,
                    observation.source_provider,
                    observation.source_url,
                    observation.observed_value,
                    observation.unit,
                    observation.bucket_label,
                    observation.bucket_key,
                    observation.observation_count,
                    _text_time(observation.observation_start),
                    _text_time(observation.observation_end),
                    "settled",
                    imported_at,
                    _json(observation.raw_payload),
                ),
            )
            connection.commit()
        return settlement_key

    def reconcile_signal_outcomes(
        self,
        *,
        city_id: str | None = None,
        target_date: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        filters = []
        params: list[Any] = []
        if city_id:
            filters.append("runs.city_id = ?")
            params.append(city_id)
        if target_date:
            filters.append("runs.target_date = ?")
            params.append(target_date)
        if kind:
            filters.append("runs.kind = ?")
            params.append(kind)
        where = " AND " + " AND ".join(filters) if filters else ""
        created_at = _now()
        outcomes: list[dict[str, Any]] = []
        with connect_database(self.path) as connection:
            rows = connection.execute(
                f"""
                SELECT
                  signals.id AS signal_snapshot_id,
                  signals.run_key,
                  signals.market_snapshot_group,
                  signals.outcome,
                  signals.bucket_key,
                  signals.ensemble_probability,
                  signals.market_midpoint,
                  signals.edge,
                  signals.recommendation,
                  runs.city_id,
                  runs.target_date,
                  runs.kind,
                  settlements.settlement_key,
                  settlements.observed_value,
                  settlements.unit,
                  settlements.bucket_key AS settlement_bucket_key
                FROM weather_signal_snapshots signals
                JOIN (
                  SELECT DISTINCT run_key, city_id, target_date, kind
                  FROM weather_ensemble_runs
                ) runs ON runs.run_key = signals.run_key
                JOIN weather_settlements settlements
                  ON settlements.city_id = runs.city_id
                 AND settlements.target_date = runs.target_date
                 AND settlements.kind = runs.kind
                 AND settlements.status = 'settled'
                WHERE 1 = 1
                {where}
                ORDER BY signals.id ASC
                """,
                params,
            ).fetchall()
            for row in rows:
                actual_bucket_key = row["settlement_bucket_key"] or self._actual_bucket_key(
                    connection,
                    market_snapshot_group=row["market_snapshot_group"],
                    observed_value=float(row["observed_value"]),
                    observed_unit=row["unit"],
                )
                won = bucket_key_contains_value(
                    bucket_key=row["bucket_key"],
                    bucket_label=row["outcome"],
                    observed_value=float(row["observed_value"]),
                    observed_unit=row["unit"],
                )
                observed_probability = 1.0 if won else 0.0
                predicted_probability = float(row["ensemble_probability"])
                probability_error = observed_probability - predicted_probability
                brier_score = probability_error * probability_error
                outcome = {
                    "signalSnapshotId": int(row["signal_snapshot_id"]),
                    "runKey": row["run_key"],
                    "settlementKey": row["settlement_key"],
                    "cityId": row["city_id"],
                    "targetDate": row["target_date"],
                    "kind": row["kind"],
                    "outcome": row["outcome"],
                    "bucketKey": row["bucket_key"],
                    "actualBucketKey": actual_bucket_key,
                    "ensembleProbability": predicted_probability,
                    "marketMidpoint": row["market_midpoint"],
                    "edge": float(row["edge"]),
                    "recommendation": row["recommendation"],
                    "won": won,
                    "brierScore": brier_score,
                    "probabilityError": probability_error,
                    "createdAt": created_at,
                }
                outcomes.append(outcome)
                connection.execute(
                    """
                    INSERT INTO weather_signal_outcomes (
                      signal_snapshot_id, run_key, settlement_key, city_id,
                      target_date, kind, outcome, bucket_key, actual_bucket_key,
                      ensemble_probability, market_midpoint, edge,
                      recommendation, won, brier_score, probability_error,
                      created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(signal_snapshot_id, settlement_key) DO UPDATE SET
                      actual_bucket_key = excluded.actual_bucket_key,
                      ensemble_probability = excluded.ensemble_probability,
                      market_midpoint = excluded.market_midpoint,
                      edge = excluded.edge,
                      recommendation = excluded.recommendation,
                      won = excluded.won,
                      brier_score = excluded.brier_score,
                      probability_error = excluded.probability_error,
                      created_at = excluded.created_at
                    """,
                    (
                        outcome["signalSnapshotId"],
                        outcome["runKey"],
                        outcome["settlementKey"],
                        outcome["cityId"],
                        outcome["targetDate"],
                        outcome["kind"],
                        outcome["outcome"],
                        outcome["bucketKey"],
                        outcome["actualBucketKey"],
                        outcome["ensembleProbability"],
                        outcome["marketMidpoint"],
                        outcome["edge"],
                        outcome["recommendation"],
                        1 if outcome["won"] else 0,
                        outcome["brierScore"],
                        outcome["probabilityError"],
                        outcome["createdAt"],
                    ),
                )
            connection.commit()
        return outcomes

    def _actual_bucket_key(
        self,
        connection,  # noqa: ANN001
        *,
        market_snapshot_group: str | None,
        observed_value: float,
        observed_unit: str,
    ) -> str | None:
        if not market_snapshot_group:
            return None
        rows = connection.execute(
            """
            SELECT bucket_label, bucket_key
            FROM weather_market_snapshots
            WHERE market_snapshot_group = ?
            ORDER BY id ASC
            """,
            (market_snapshot_group,),
        ).fetchall()
        for row in rows:
            if bucket_key_contains_value(
                bucket_key=row["bucket_key"],
                bucket_label=row["bucket_label"],
                observed_value=observed_value,
                observed_unit=observed_unit,
            ):
                return row["bucket_key"]
        return None

    def recent_settlements(self, *, limit: int = 20) -> list[dict[str, Any]]:
        self._ensure_schema()
        with connect_database(self.path) as connection:
            rows = connection.execute(
                """
                SELECT settlement_key, import_run_key, city_id, city_name,
                       target_date, kind, station_id, settlement_station,
                       source_provider, observed_value, unit, bucket_label,
                       bucket_key, observation_count, status, imported_at
                FROM weather_settlements
                ORDER BY imported_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_signal_outcomes(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self._ensure_schema()
        with connect_database(self.path) as connection:
            rows = connection.execute(
                """
                SELECT signal_snapshot_id, run_key, settlement_key, city_id,
                       target_date, kind, outcome, bucket_key, actual_bucket_key,
                       ensemble_probability, market_midpoint, edge,
                       recommendation, won, brier_score, probability_error,
                       created_at
                FROM weather_signal_outcomes
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def calibration_summary(self) -> dict[str, Any]:
        self._ensure_schema()
        with connect_database(self.path) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT ensemble_probability, edge, recommendation, won,
                           brier_score, probability_error
                    FROM weather_signal_outcomes
                    ORDER BY id ASC
                    """
                ).fetchall()
            ]
        total = len(rows)
        wins = sum(1 for row in rows if row["won"])
        buy_rows = [row for row in rows if row["recommendation"] == "BUY_YES"]
        return {
            "summary": {
                "outcomeCount": total,
                "winCount": wins,
                "hitRate": wins / total if total else 0.0,
                "averageBrierScore": _average([float(row["brier_score"]) for row in rows]),
                "averageProbabilityError": _average(
                    [float(row["probability_error"]) for row in rows]
                ),
                "buySignalCount": len(buy_rows),
                "buySignalHitRate": (
                    sum(1 for row in buy_rows if row["won"]) / len(buy_rows)
                    if buy_rows
                    else 0.0
                ),
            },
            "probabilityBins": self._probability_bins(rows),
            "edgeBuckets": self._edge_buckets(rows),
        }

    @staticmethod
    def _probability_bins(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        bins: list[dict[str, Any]] = []
        for index in range(10):
            lower = index / 10.0
            upper = (index + 1) / 10.0
            bucket_rows = [
                row
                for row in rows
                if lower <= float(row["ensemble_probability"]) < upper
                or (index == 9 and float(row["ensemble_probability"]) == 1.0)
            ]
            count = len(bucket_rows)
            wins = sum(1 for row in bucket_rows if row["won"])
            bins.append(
                {
                    "label": f"{int(lower * 100)}-{int(upper * 100)}%",
                    "lower": lower,
                    "upper": upper,
                    "count": count,
                    "averageProbability": _average(
                        [float(row["ensemble_probability"]) for row in bucket_rows]
                    ),
                    "observedRate": wins / count if count else None,
                    "averageBrierScore": _average(
                        [float(row["brier_score"]) for row in bucket_rows]
                    ),
                }
            )
        return bins

    @staticmethod
    def _edge_buckets(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        groups = [
            ("<0%", lambda edge: edge < 0.0),
            ("0-3%", lambda edge: 0.0 <= edge < 0.03),
            ("3-5%", lambda edge: 0.03 <= edge < 0.05),
            (">=5%", lambda edge: edge >= 0.05),
        ]
        result: list[dict[str, Any]] = []
        for label, predicate in groups:
            bucket_rows = [row for row in rows if predicate(float(row["edge"]))]
            count = len(bucket_rows)
            wins = sum(1 for row in bucket_rows if row["won"])
            result.append(
                {
                    "label": label,
                    "count": count,
                    "averageEdge": _average([float(row["edge"]) for row in bucket_rows]),
                    "hitRate": wins / count if count else None,
                    "averageBrierScore": _average(
                        [float(row["brier_score"]) for row in bucket_rows]
                    ),
                }
            )
        return result

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

    def get_or_create_paper_account(
        self,
        *,
        account_key: str = DEFAULT_ACCOUNT_KEY,
        initial_cash: float = DEFAULT_INITIAL_CASH,
        name: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        timestamp = _now()
        with connect_database(self.path) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO weather_paper_accounts (
                  account_key, name, initial_cash, cash_balance, status,
                  created_at, updated_at, raw_config_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_key,
                    name or account_key,
                    float(initial_cash),
                    float(initial_cash),
                    "ACTIVE",
                    timestamp,
                    timestamp,
                    _json({"initialCash": float(initial_cash)}),
                ),
            )
            row = connection.execute(
                """
                SELECT *
                FROM weather_paper_accounts
                WHERE account_key = ?
                """,
                (account_key,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError(f"Paper account not found: {account_key}")
        return self._paper_account_payload(dict(row))

    def paper_buy_preview(
        self,
        *,
        signal: Mapping[str, Any],
        market_buckets: Sequence[MarketBucket],
        context: Mapping[str, Any] | None = None,
        account_key: str = DEFAULT_ACCOUNT_KEY,
        initial_cash: float = DEFAULT_INITIAL_CASH,
        stake_usdc: float | None = None,
        min_edge: float = DEFAULT_MIN_EDGE,
        fee_rate: float = 0.05,
        max_spread: float = DEFAULT_MAX_SPREAD,
        min_ask_depth_shares: float = DEFAULT_MIN_ASK_DEPTH_SHARES,
        max_market_exposure: float = DEFAULT_MAX_MARKET_EXPOSURE,
        max_city_date_exposure: float = DEFAULT_MAX_CITY_DATE_EXPOSURE,
    ) -> dict[str, Any]:
        account = self.get_or_create_paper_account(
            account_key=account_key,
            initial_cash=initial_cash,
        )
        positions = self._open_paper_position_rows(account_key=account_key)
        market_bucket = find_market_bucket(market_buckets, signal)
        return paper_buy_preview(
            signal=signal,
            market_bucket=market_bucket,
            account=account,
            open_positions=positions,
            context=context or {},
            stake_usdc=stake_usdc,
            min_edge=min_edge,
            fee_rate=fee_rate,
            max_spread=max_spread,
            min_ask_depth_shares=min_ask_depth_shares,
            max_market_exposure=max_market_exposure,
            max_city_date_exposure=max_city_date_exposure,
        )

    def execute_paper_buy(
        self,
        *,
        signal: Mapping[str, Any],
        market_buckets: Sequence[MarketBucket],
        context: Mapping[str, Any] | None = None,
        account_key: str = DEFAULT_ACCOUNT_KEY,
        initial_cash: float = DEFAULT_INITIAL_CASH,
        stake_usdc: float | None = None,
        min_edge: float = DEFAULT_MIN_EDGE,
        fee_rate: float = 0.05,
        max_spread: float = DEFAULT_MAX_SPREAD,
        min_ask_depth_shares: float = DEFAULT_MIN_ASK_DEPTH_SHARES,
        max_market_exposure: float = DEFAULT_MAX_MARKET_EXPOSURE,
        max_city_date_exposure: float = DEFAULT_MAX_CITY_DATE_EXPOSURE,
    ) -> dict[str, Any]:
        preview = self.paper_buy_preview(
            signal=signal,
            market_buckets=market_buckets,
            context=context,
            account_key=account_key,
            initial_cash=initial_cash,
            stake_usdc=stake_usdc,
            min_edge=min_edge,
            fee_rate=fee_rate,
            max_spread=max_spread,
            min_ask_depth_shares=min_ask_depth_shares,
            max_market_exposure=max_market_exposure,
            max_city_date_exposure=max_city_date_exposure,
        )
        timestamp = _now()
        with connect_database(self.path) as connection:
            order = self._insert_paper_order(
                connection,
                preview=preview,
                signal=signal,
                timestamp=timestamp,
            )
            trade = None
            position = None
            if preview.get("accepted"):
                position = self._upsert_paper_position(
                    connection,
                    preview=preview,
                    timestamp=timestamp,
                )
                trade = self._insert_paper_trade(
                    connection,
                    preview=preview,
                    order_key=order["orderKey"],
                    position_key=position["positionKey"],
                    timestamp=timestamp,
                )
                connection.execute(
                    """
                    UPDATE weather_paper_accounts
                    SET cash_balance = cash_balance - ?,
                        updated_at = ?
                    WHERE account_key = ?
                    """,
                    (safe_float(preview.get("netCost")), timestamp, account_key),
                )
            connection.commit()
        return {
            "summary": {
                "accepted": bool(preview.get("accepted")),
                "rejectReason": preview.get("rejectReason"),
                "orderStatus": order["status"],
                "noRealOrder": True,
            },
            "preview": preview,
            "order": order,
            "trade": trade,
            "position": position,
            "account": self.get_or_create_paper_account(account_key=account_key),
        }

    def save_paper_order(
        self,
        *,
        preview: Mapping[str, Any],
        signal: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        with connect_database(self.path) as connection:
            order = self._insert_paper_order(
                connection,
                preview=preview,
                signal=signal or {},
                timestamp=_now(),
            )
            connection.commit()
        return order

    def save_paper_trade(
        self,
        *,
        preview: Mapping[str, Any],
        order_key: str,
        position_key: str,
    ) -> dict[str, Any]:
        self._ensure_schema()
        with connect_database(self.path) as connection:
            trade = self._insert_paper_trade(
                connection,
                preview=preview,
                order_key=order_key,
                position_key=position_key,
                timestamp=_now(),
            )
            connection.commit()
        return trade

    def update_paper_position(
        self,
        *,
        preview: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._ensure_schema()
        with connect_database(self.path) as connection:
            position = self._upsert_paper_position(
                connection,
                preview=preview,
                timestamp=_now(),
            )
            connection.commit()
        return position

    def paper_portfolio(
        self,
        *,
        account_key: str = DEFAULT_ACCOUNT_KEY,
        initial_cash: float = DEFAULT_INITIAL_CASH,
        market_buckets: Sequence[MarketBucket] = (),
        limit: int = 20,
        fee_rate: float = 0.05,
    ) -> dict[str, Any]:
        account = self.get_or_create_paper_account(
            account_key=account_key,
            initial_cash=initial_cash,
        )
        market_by_key = {bucket.bucket.canonical_key: bucket for bucket in market_buckets}
        with connect_database(self.path) as connection:
            position_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM weather_paper_positions
                    WHERE account_key = ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    (account_key, limit),
                ).fetchall()
            ]
            order_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM weather_paper_orders
                    WHERE account_key = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (account_key, limit),
                ).fetchall()
            ]
            settlement_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM weather_paper_position_settlements
                    WHERE account_key = ?
                    ORDER BY settled_at DESC, id DESC
                    LIMIT ?
                    """,
                    (account_key, limit),
                ).fetchall()
            ]
            latest_marks = self._latest_paper_mark_rows(
                connection,
                account_key=account_key,
            )
            recent_mark_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM weather_paper_position_marks
                    WHERE account_key = ?
                    ORDER BY fetched_at DESC, id DESC
                    LIMIT ?
                    """,
                    (account_key, limit),
                ).fetchall()
            ]
        positions = [
            self._paper_position_payload(
                row,
                market_bucket=market_by_key.get(str(row["bucket_key"])),
                fee_rate=fee_rate,
                latest_mark=latest_marks.get(str(row["position_key"])),
            )
            for row in position_rows
        ]
        open_positions = [row for row in positions if row["status"] in {"OPEN", "PARTIALLY_CLOSED"}]
        open_cost = sum(safe_float(row.get("totalCost")) for row in open_positions)
        mark_value = sum(safe_float(row.get("markValue")) for row in open_positions)
        liquidation_value = sum(safe_float(row.get("liquidationValue")) for row in open_positions)
        realized_pnl = sum(safe_float(row.get("realizedPnl")) for row in positions)
        return {
            "summary": {
                "accountKey": account["accountKey"],
                "cash": account["cashBalance"],
                "initialCash": account["initialCash"],
                "openPositionCost": open_cost,
                "markValue": mark_value,
                "liquidationValue": liquidation_value,
                "realizedPnl": realized_pnl,
                "unrealizedPnl": mark_value - open_cost,
                "totalEquity": safe_float(account["cashBalance"]) + mark_value,
                "openPositionCount": len(open_positions),
                "orderCount": len(order_rows),
                "noRealOrder": True,
            },
            "account": account,
            "positions": positions,
            "orders": [self._paper_order_payload(row) for row in order_rows],
            "settlements": [self._paper_settlement_payload(row) for row in settlement_rows],
            "marks": [self._paper_mark_payload(row) for row in recent_mark_rows],
        }

    def paper_mark_positions(
        self,
        *,
        account_key: str = DEFAULT_ACCOUNT_KEY,
        initial_cash: float = DEFAULT_INITIAL_CASH,
        market_buckets: Sequence[MarketBucket] = (),
        fee_rate: float = 0.05,
        target_profit: float = 0.10,
        min_cashout_ratio: float = 0.50,
        signal_edges: Mapping[str, float] | None = None,
    ) -> dict[str, Any]:
        self.get_or_create_paper_account(
            account_key=account_key,
            initial_cash=initial_cash,
        )
        positions = self._open_paper_position_rows(account_key=account_key)
        fetched_at = _now()
        marks: list[dict[str, Any]] = []
        edge_map = dict(signal_edges or {})
        with connect_database(self.path) as connection:
            for position in positions:
                market_bucket = find_market_bucket(
                    market_buckets,
                    {
                        "bucketKey": position["bucket_key"],
                        "outcome": position["outcome"],
                        "tokenId": position["token_id"],
                    },
                )
                mark = self._insert_paper_position_mark(
                    connection,
                    position=position,
                    market_bucket=market_bucket,
                    fee_rate=fee_rate,
                    target_profit=target_profit,
                    min_cashout_ratio=min_cashout_ratio,
                    signal_edge=edge_map.get(str(position["bucket_key"])),
                    fetched_at=fetched_at,
                )
                marks.append(mark)
            connection.commit()
        warning_count = sum(1 for mark in marks if mark.get("warning"))
        return {
            "summary": {
                "openPositionCount": len(positions),
                "markCount": len(marks),
                "warningCount": warning_count,
                "fetchedAt": fetched_at,
                "noRealOrder": True,
            },
            "marks": marks,
        }

    def reconcile_paper_positions(
        self,
        *,
        account_key: str = DEFAULT_ACCOUNT_KEY,
        city_id: str | None = None,
        target_date: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        filters = ["positions.account_key = ?", "positions.status IN ('OPEN', 'PARTIALLY_CLOSED')"]
        params: list[Any] = [account_key]
        if city_id:
            filters.append("positions.city_id = ?")
            params.append(city_id)
        if target_date:
            filters.append("positions.target_date = ?")
            params.append(target_date)
        if kind:
            filters.append("positions.kind = ?")
            params.append(kind)
        where = " AND ".join(filters)
        settled_at = _now()
        settlements: list[dict[str, Any]] = []
        with connect_database(self.path) as connection:
            rows = connection.execute(
                f"""
                SELECT
                  positions.*,
                  settlements.settlement_key,
                  settlements.bucket_key AS actual_bucket_key,
                  settlements.bucket_label AS actual_bucket_label,
                  settlements.observed_value,
                  settlements.unit,
                  settlements.raw_payload_json AS settlement_raw_payload_json
                FROM weather_paper_positions positions
                JOIN weather_settlements settlements
                  ON settlements.city_id = positions.city_id
                 AND settlements.target_date = positions.target_date
                 AND settlements.kind = positions.kind
                 AND settlements.status = 'settled'
                WHERE {where}
                ORDER BY positions.id ASC
                """,
                params,
            ).fetchall()
            total_payout = 0.0
            total_realized = 0.0
            for raw_row in rows:
                row = dict(raw_row)
                shares = safe_float(row["open_shares"])
                total_cost = safe_float(row["total_cost"])
                won = row["actual_bucket_key"] == row["bucket_key"] or bucket_key_contains_value(
                    bucket_key=row["bucket_key"],
                    bucket_label=row["bucket_label"],
                    observed_value=float(row["observed_value"]),
                    observed_unit=row["unit"],
                )
                payout = shares if won else 0.0
                realized = payout - total_cost
                total_payout += payout
                total_realized += realized
                settlement_record_key = (
                    "paper-settlement:"
                    + stable_payload_hash(
                        {
                            "account_key": account_key,
                            "position_key": row["position_key"],
                            "settlement_key": row["settlement_key"],
                        }
                    )[:20]
                )
                payload = {
                    "won": won,
                    "actualBucketLabel": row["actual_bucket_label"],
                }
                connection.execute(
                    """
                    INSERT INTO weather_paper_position_settlements (
                      settlement_record_key, account_key, position_key,
                      run_key, model, settlement_key, city_id, target_date, kind, outcome,
                      bucket_label, bucket_key, actual_bucket_key, observed_value,
                      unit, shares, total_cost, payout, realized_pnl, settled_at,
                      raw_payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(settlement_record_key) DO UPDATE SET
                      actual_bucket_key = excluded.actual_bucket_key,
                      observed_value = excluded.observed_value,
                      unit = excluded.unit,
                      shares = excluded.shares,
                      total_cost = excluded.total_cost,
                      payout = excluded.payout,
                      realized_pnl = excluded.realized_pnl,
                      settled_at = excluded.settled_at,
                      raw_payload_json = excluded.raw_payload_json
                    """,
                    (
                        settlement_record_key,
                        account_key,
                        row["position_key"],
                        row.get("run_key"),
                        row.get("model"),
                        row["settlement_key"],
                        row["city_id"],
                        row["target_date"],
                        row["kind"],
                        row["outcome"],
                        row["bucket_label"],
                        row["bucket_key"],
                        row["actual_bucket_key"],
                        row["observed_value"],
                        row["unit"],
                        shares,
                        total_cost,
                        payout,
                        realized,
                        settled_at,
                        _json(payload),
                    ),
                )
                connection.execute(
                    """
                    UPDATE weather_paper_positions
                    SET open_shares = 0,
                        realized_pnl = realized_pnl + ?,
                        status = 'SETTLED',
                        updated_at = ?,
                        closed_at = ?
                    WHERE position_key = ?
                    """,
                    (realized, settled_at, settled_at, row["position_key"]),
                )
                connection.execute(
                    """
                    UPDATE weather_paper_accounts
                    SET cash_balance = cash_balance + ?,
                        updated_at = ?
                    WHERE account_key = ?
                    """,
                    (payout, settled_at, account_key),
                )
                settlements.append(
                    {
                        "settlementRecordKey": settlement_record_key,
                        "positionKey": row["position_key"],
                        "runKey": row.get("run_key"),
                        "model": row.get("model"),
                        "settlementKey": row["settlement_key"],
                        "outcome": row["outcome"],
                        "won": won,
                        "shares": shares,
                        "totalCost": total_cost,
                        "payout": payout,
                        "realizedPnl": realized,
                    }
                )
            connection.commit()
        return {
            "summary": {
                "settledPositionCount": len(settlements),
                "payout": total_payout if settlements else 0.0,
                "realizedPnl": total_realized if settlements else 0.0,
            },
            "settlements": settlements,
            "account": self.get_or_create_paper_account(account_key=account_key),
        }

    def model_competition_stats(self) -> dict[str, list[dict[str, Any]]]:
        """返回模型竞赛已成交订单的模型、城市和温度类型统计。"""
        self._ensure_schema()
        with connect_database(self.path) as connection:
            by_model = self._model_competition_stats_rows(connection, "orders.model")
            by_city_model = self._model_competition_stats_rows(
                connection,
                "orders.city_id, orders.city_name, orders.model",
            )
            by_kind_model = self._model_competition_stats_rows(
                connection,
                "orders.kind, orders.model",
            )
        return {
            "byModel": by_model,
            "byCityModel": by_city_model,
            "byKindModel": by_kind_model,
        }

    def model_competition_account_keys(self) -> list[str]:
        """返回存在模型竞赛持仓的隔离虚拟账户。"""
        self._ensure_schema()
        with connect_database(self.path) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT account_key
                FROM weather_paper_positions
                WHERE model IS NOT NULL AND account_key LIKE 'model-competition:%'
                ORDER BY account_key ASC
                """
            ).fetchall()
        return [str(row["account_key"]) for row in rows]

    @staticmethod
    def _model_competition_stats_rows(connection, group_by: str) -> list[dict[str, Any]]:  # noqa: ANN001
        rows = connection.execute(
            f"""
            SELECT
              orders.model AS model,
              orders.city_id AS city_id,
              MAX(orders.city_name) AS city_name,
              orders.kind AS kind,
              COUNT(*) AS order_count,
              SUM(CASE WHEN settlements.settlement_record_key IS NOT NULL THEN 1 ELSE 0 END) AS settled_order_count,
              SUM(CASE WHEN settlements.payout > 0 THEN 1 ELSE 0 END) AS hit_count,
              SUM(orders.stake_usdc) AS total_stake,
              COALESCE(SUM(settlements.payout), 0) AS total_payout,
              COALESCE(SUM(settlements.realized_pnl), 0) AS realized_pnl,
              AVG(orders.edge) AS average_edge
            FROM weather_paper_orders orders
            LEFT JOIN weather_paper_position_settlements settlements
              ON settlements.position_key = (
                SELECT trades.position_key
                FROM weather_paper_trades trades
                WHERE trades.order_key = orders.order_key
                ORDER BY trades.id DESC
                LIMIT 1
              )
            WHERE orders.model IS NOT NULL AND orders.status = 'FILLED'
            GROUP BY {group_by}
            ORDER BY realized_pnl DESC, order_count DESC, model ASC
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            settled = int(item["settled_order_count"] or 0)
            hits = int(item["hit_count"] or 0)
            stake = safe_float(item["total_stake"])
            item.update(
                {
                    "model": item["model"],
                    "cityId": item.pop("city_id"),
                    "cityName": item.pop("city_name"),
                    "kind": item["kind"],
                    "orderCount": int(item.pop("order_count") or 0),
                    "settledOrderCount": settled,
                    "hitCount": hits,
                    "winRate": hits / settled if settled else None,
                    "totalStake": stake,
                    "totalPayout": safe_float(item.pop("total_payout")),
                    "realizedPnl": safe_float(item.pop("realized_pnl")),
                    "averageEdge": item.pop("average_edge"),
                }
            )
            item["roi"] = item["realizedPnl"] / stake if stake else None
            result.append(item)
        return result

    def paper_exit_preview(
        self,
        *,
        account_key: str = DEFAULT_ACCOUNT_KEY,
        position_key: str | None = None,
        market_buckets: Sequence[MarketBucket] = (),
        shares: float | None = None,
        fee_rate: float = 0.05,
        target_profit: float = 0.10,
        signal_edge: float | None = None,
        min_cashout_ratio: float = 0.50,
    ) -> dict[str, Any]:
        self._ensure_schema()
        with connect_database(self.path) as connection:
            if position_key:
                row = connection.execute(
                    """
                    SELECT *
                    FROM weather_paper_positions
                    WHERE account_key = ? AND position_key = ?
                    """,
                    (account_key, position_key),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT *
                    FROM weather_paper_positions
                    WHERE account_key = ? AND status IN ('OPEN', 'PARTIALLY_CLOSED')
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (account_key,),
                ).fetchone()
        if row is None:
            raise ValueError("No paper position found for exit preview.")
        position = dict(row)
        market_bucket = find_market_bucket(
            market_buckets,
            {
                "bucketKey": position["bucket_key"],
                "outcome": position["outcome"],
                "tokenId": position["token_id"],
            },
        )
        return exit_preview(
            position=position,
            market_bucket=market_bucket,
            shares=shares,
            fee_rate=fee_rate,
            target_profit=target_profit,
            signal_edge=signal_edge,
            min_cashout_ratio=min_cashout_ratio,
        )

    def paper_hedge_preview(
        self,
        *,
        signals: Sequence[Mapping[str, Any]],
        market_buckets: Sequence[MarketBucket],
        account_key: str = DEFAULT_ACCOUNT_KEY,
        initial_cash: float = DEFAULT_INITIAL_CASH,
        context: Mapping[str, Any] | None = None,
        stake_usdc: float = DEFAULT_STAKE_USDC,
        fee_rate: float = 0.05,
        target_profit: float = 0.0,
        tail_probability_cutoff: float = 0.05,
        max_tail_probability: float = 0.05,
        min_adjacent_probability: float = 0.10,
    ) -> dict[str, Any]:
        account = self.get_or_create_paper_account(
            account_key=account_key,
            initial_cash=initial_cash,
        )
        positions = self._open_paper_position_rows(account_key=account_key)
        return hedge_preview(
            signals=signals,
            market_buckets=market_buckets,
            open_positions=positions,
            account=account,
            context=context or {},
            stake_usdc=stake_usdc,
            fee_rate=fee_rate,
            target_profit=target_profit,
            tail_probability_cutoff=tail_probability_cutoff,
            max_tail_probability=max_tail_probability,
            min_adjacent_probability=min_adjacent_probability,
        )

    def open_paper_positions(
        self,
        *,
        account_key: str = DEFAULT_ACCOUNT_KEY,
    ) -> list[dict[str, Any]]:
        return self._open_paper_position_rows(account_key=account_key)

    def _open_paper_position_rows(self, *, account_key: str) -> list[dict[str, Any]]:
        self._ensure_schema()
        with connect_database(self.path) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM weather_paper_positions
                    WHERE account_key = ? AND status IN ('OPEN', 'PARTIALLY_CLOSED')
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (account_key,),
                ).fetchall()
            ]

    def _insert_paper_order(
        self,
        connection,  # noqa: ANN001
        *,
        preview: Mapping[str, Any],
        signal: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        order_key = (
            "paper-order:"
            + stable_payload_hash(
                {"timestamp": timestamp, "preview": dict(preview), "signal": dict(signal)}
            )[:20]
        )
        status = "FILLED" if preview.get("accepted") else "REJECTED"
        connection.execute(
            """
            INSERT INTO weather_paper_orders (
              order_key, account_key, signal_snapshot_id, run_key, model,
              market_snapshot_group, city_id, city_name, target_date, kind,
              settlement_station, station_id, metar_source, market_slug,
              condition_id, outcome, bucket_label, bucket_key, token_id,
              side, order_type, stake_usdc, filled_shares, vwap,
              average_price, fee, net_cost, edge, ensemble_probability,
              executable_entry_cost, expected_exit_cost, best_bid, best_ask,
              spread, ask_depth, status, reject_reason, created_at,
              raw_signal_json, raw_preview_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_key,
                preview.get("accountKey"),
                preview.get("signalSnapshotId"),
                preview.get("runKey"),
                preview.get("model"),
                preview.get("marketSnapshotGroup"),
                preview.get("cityId"),
                preview.get("cityName"),
                preview.get("targetDate"),
                preview.get("kind"),
                preview.get("settlementStation"),
                preview.get("stationId"),
                preview.get("metarSource"),
                preview.get("marketSlug"),
                preview.get("conditionId"),
                preview.get("outcome") or "-",
                preview.get("bucketLabel") or "-",
                preview.get("bucketKey") or "-",
                preview.get("tokenId"),
                preview.get("side") or "BUY_YES",
                preview.get("orderType") or "MARKET_BUY",
                safe_float(preview.get("stakeUsdc")),
                safe_float(preview.get("filledShares")),
                preview.get("vwap"),
                preview.get("averagePrice"),
                safe_float(preview.get("fee")),
                safe_float(preview.get("netCost")),
                preview.get("edge"),
                preview.get("ensembleProbability"),
                preview.get("executableEntryCost"),
                preview.get("expectedExitCost"),
                preview.get("bestBid"),
                preview.get("bestAsk"),
                preview.get("spread"),
                preview.get("askDepth"),
                status,
                preview.get("rejectReason"),
                timestamp,
                _json(signal),
                _json(preview),
            ),
        )
        row = connection.execute(
            "SELECT * FROM weather_paper_orders WHERE order_key = ?",
            (order_key,),
        ).fetchone()
        return self._paper_order_payload(dict(row))

    def _upsert_paper_position(
        self,
        connection,  # noqa: ANN001
        *,
        preview: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        position_key = paper_position_key(
            account_key=str(preview.get("accountKey") or DEFAULT_ACCOUNT_KEY),
            city_id=preview.get("cityId"),
            target_date=preview.get("targetDate"),
            kind=preview.get("kind"),
            bucket_key=str(preview.get("bucketKey") or "-"),
            token_id=preview.get("tokenId"),
            model=optional_text(preview.get("model")),
            run_key=optional_text(preview.get("runKey")),
        )
        connection.execute(
            """
            INSERT INTO weather_paper_positions (
              position_key, account_key, run_key, model, city_id, city_name, target_date,
              kind, market_snapshot_group, market_slug, condition_id, outcome,
              bucket_label, bucket_key, token_id, settlement_station, station_id,
              metar_source, open_shares, total_cost, average_entry_price,
              realized_pnl, status, opened_at, updated_at, closed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(position_key) DO UPDATE SET
              open_shares = weather_paper_positions.open_shares + excluded.open_shares,
              total_cost = weather_paper_positions.total_cost + excluded.total_cost,
              average_entry_price = (
                weather_paper_positions.total_cost + excluded.total_cost
              ) / NULLIF(
                weather_paper_positions.open_shares + excluded.open_shares,
                0
              ),
              status = 'OPEN',
              updated_at = excluded.updated_at,
              closed_at = NULL
            """,
            (
                position_key,
                preview.get("accountKey"),
                preview.get("runKey"),
                preview.get("model"),
                preview.get("cityId"),
                preview.get("cityName"),
                preview.get("targetDate"),
                preview.get("kind"),
                preview.get("marketSnapshotGroup"),
                preview.get("marketSlug"),
                preview.get("conditionId"),
                preview.get("outcome"),
                preview.get("bucketLabel"),
                preview.get("bucketKey"),
                preview.get("tokenId"),
                preview.get("settlementStation"),
                preview.get("stationId"),
                preview.get("metarSource"),
                safe_float(preview.get("filledShares")),
                safe_float(preview.get("netCost")),
                preview.get("averagePrice"),
                0.0,
                "OPEN",
                timestamp,
                timestamp,
                None,
            ),
        )
        row = connection.execute(
            "SELECT * FROM weather_paper_positions WHERE position_key = ?",
            (position_key,),
        ).fetchone()
        return self._paper_position_payload(dict(row), market_bucket=None)

    def _insert_paper_trade(
        self,
        connection,  # noqa: ANN001
        *,
        preview: Mapping[str, Any],
        order_key: str,
        position_key: str,
        timestamp: str,
    ) -> dict[str, Any]:
        trade_key = (
            "paper-trade:"
            + stable_payload_hash(
                {"timestamp": timestamp, "order_key": order_key, "position_key": position_key}
            )[:20]
        )
        connection.execute(
            """
            INSERT INTO weather_paper_trades (
              trade_key, account_key, order_key, position_key, run_key, model, side, outcome,
              bucket_label, bucket_key, token_id, shares, price, notional,
              fee, net_cost, traded_at, raw_fill_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_key,
                preview.get("accountKey"),
                order_key,
                position_key,
                preview.get("runKey"),
                preview.get("model"),
                preview.get("side") or "BUY_YES",
                preview.get("outcome"),
                preview.get("bucketLabel"),
                preview.get("bucketKey"),
                preview.get("tokenId"),
                safe_float(preview.get("filledShares")),
                safe_float(preview.get("vwap") or preview.get("averagePrice")),
                safe_float(preview.get("notional")),
                safe_float(preview.get("fee")),
                safe_float(preview.get("netCost")),
                timestamp,
                _json(preview.get("fill") or {}),
            ),
        )
        row = connection.execute(
            "SELECT * FROM weather_paper_trades WHERE trade_key = ?",
            (trade_key,),
        ).fetchone()
        return {
            "tradeKey": row["trade_key"],
            "orderKey": row["order_key"],
            "positionKey": row["position_key"],
            "runKey": row["run_key"],
            "model": row["model"],
            "side": row["side"],
            "outcome": row["outcome"],
            "shares": row["shares"],
            "price": row["price"],
            "notional": row["notional"],
            "fee": row["fee"],
            "netCost": row["net_cost"],
            "tradedAt": row["traded_at"],
        }

    def _insert_paper_position_mark(
        self,
        connection,  # noqa: ANN001
        *,
        position: Mapping[str, Any],
        market_bucket: MarketBucket | None,
        fee_rate: float,
        target_profit: float,
        min_cashout_ratio: float,
        signal_edge: float | None,
        fetched_at: str,
    ) -> dict[str, Any]:
        position_key = str(position["position_key"])
        shares = safe_float(position["open_shares"])
        total_cost = safe_float(position["total_cost"])
        average_entry = optional_float(position.get("average_entry_price")) or (
            total_cost / shares if shares > 0 else 0.0
        )
        best_bid = None
        best_ask = None
        midpoint = None
        spread = None
        bid_depth = None
        ask_depth = None
        mark_value = shares * average_entry
        liquidation_value = 0.0
        cashout_ratio = None
        unrealized_pnl = mark_value - total_cost
        executable_pnl = -total_cost
        exit_signal = "NO_ORDERBOOK"
        warning = "NO_ORDERBOOK"
        raw_orderbook: dict[str, Any] = {}

        if market_bucket is not None and market_bucket.orderbook is not None:
            orderbook = market_bucket.orderbook
            best_bid = orderbook.best_bid
            best_ask = orderbook.best_ask
            midpoint = orderbook.midpoint
            spread = orderbook.spread
            bid_depth = sum(level.size for level in orderbook.bids[:3])
            ask_depth = sum(level.size for level in orderbook.asks[:3])
            valuation = value_position(
                position_from_mapping(position),
                market_bucket,
                fee_rate=fee_rate,
            )
            mark_value = valuation.mark_value
            liquidation_value = valuation.liquidation_value
            cashout_ratio = valuation.cashout_ratio
            unrealized_pnl = valuation.unrealized_mark_pnl
            executable_pnl = valuation.executable_pnl
            exit_result = exit_preview(
                position=position,
                market_bucket=market_bucket,
                fee_rate=fee_rate,
                target_profit=target_profit,
                signal_edge=signal_edge,
                min_cashout_ratio=min_cashout_ratio,
            )
            exit_signal = self._exit_signal_from_preview(exit_result)
            warning = exit_result.get("rejectReason") if not exit_result.get("accepted") else None
            raw_orderbook = {
                "tokenId": orderbook.token_id,
                "bids": [
                    {"price": level.price, "size": level.size}
                    for level in orderbook.bids
                ],
                "asks": [
                    {"price": level.price, "size": level.size}
                    for level in orderbook.asks
                ],
                "rawPayload": dict(orderbook.raw_payload),
            }
        elif not position.get("token_id"):
            warning = "MISSING_TOKEN_ID"
            exit_signal = "MISSING_TOKEN_ID"

        mark_key = (
            "paper-mark:"
            + stable_payload_hash(
                {
                    "position_key": position_key,
                    "token_id": position.get("token_id"),
                    "fetched_at": fetched_at,
                }
            )[:20]
        )
        connection.execute(
            """
            INSERT INTO weather_paper_position_marks (
              mark_key, account_key, position_key, token_id, city_id,
              target_date, kind, outcome, bucket_label, bucket_key,
              open_shares, total_cost, average_entry_price, best_bid,
              best_ask, midpoint, spread, bid_depth, ask_depth, mark_value,
              liquidation_value, cashout_ratio, unrealized_pnl, executable_pnl,
              exit_signal, warning, fetched_at, raw_orderbook_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mark_key,
                position["account_key"],
                position_key,
                position.get("token_id"),
                position.get("city_id"),
                position.get("target_date"),
                position.get("kind"),
                position["outcome"],
                position["bucket_label"],
                position["bucket_key"],
                shares,
                total_cost,
                average_entry,
                best_bid,
                best_ask,
                midpoint,
                spread,
                bid_depth,
                ask_depth,
                mark_value,
                liquidation_value,
                cashout_ratio,
                unrealized_pnl,
                executable_pnl,
                exit_signal,
                warning,
                fetched_at,
                _json(raw_orderbook),
            ),
        )
        row = connection.execute(
            "SELECT * FROM weather_paper_position_marks WHERE mark_key = ?",
            (mark_key,),
        ).fetchone()
        return self._paper_mark_payload(dict(row))

    @staticmethod
    def _exit_signal_from_preview(preview: Mapping[str, Any]) -> str:
        if not preview.get("accepted"):
            return str(preview.get("rejectReason") or "NO_EXIT")
        triggers = preview.get("triggers")
        if isinstance(triggers, (list, tuple)) and triggers:
            return "EXIT_" + "+".join(str(item) for item in triggers)
        return "HOLD"

    @staticmethod
    def _latest_paper_mark_rows(
        connection,  # noqa: ANN001
        *,
        account_key: str,
    ) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT marks.*
            FROM weather_paper_position_marks marks
            JOIN (
              SELECT position_key, MAX(id) AS latest_id
              FROM weather_paper_position_marks
              WHERE account_key = ?
              GROUP BY position_key
            ) latest ON latest.latest_id = marks.id
            """,
            (account_key,),
        ).fetchall()
        return {str(row["position_key"]): dict(row) for row in rows}

    @staticmethod
    def _paper_account_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "accountKey": row["account_key"],
            "name": row["name"],
            "initialCash": row["initial_cash"],
            "cashBalance": row["cash_balance"],
            "status": row["status"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _paper_order_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "orderKey": row["order_key"],
            "accountKey": row["account_key"],
            "signalSnapshotId": row["signal_snapshot_id"],
            "runKey": row["run_key"],
            "model": row["model"],
            "marketSnapshotGroup": row["market_snapshot_group"],
            "cityId": row["city_id"],
            "cityName": row["city_name"],
            "targetDate": row["target_date"],
            "kind": row["kind"],
            "outcome": row["outcome"],
            "bucketLabel": row["bucket_label"],
            "bucketKey": row["bucket_key"],
            "tokenId": row["token_id"],
            "side": row["side"],
            "orderType": row["order_type"],
            "stakeUsdc": row["stake_usdc"],
            "filledShares": row["filled_shares"],
            "vwap": row["vwap"],
            "averagePrice": row["average_price"],
            "fee": row["fee"],
            "netCost": row["net_cost"],
            "edge": row["edge"],
            "ensembleProbability": row["ensemble_probability"],
            "bestBid": row["best_bid"],
            "bestAsk": row["best_ask"],
            "spread": row["spread"],
            "askDepth": row["ask_depth"],
            "status": row["status"],
            "rejectReason": row["reject_reason"],
            "createdAt": row["created_at"],
        }

    @staticmethod
    def _paper_position_payload(
        row: Mapping[str, Any],
        *,
        market_bucket: MarketBucket | None,
        fee_rate: float = 0.05,
        latest_mark: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        position = position_from_mapping(row)
        shares = safe_float(row["open_shares"])
        if market_bucket is not None and shares > 0:
            valuation = value_position(position, market_bucket, fee_rate=fee_rate)
            mark_price = valuation.mark_price
            best_bid = valuation.best_bid
            best_ask = valuation.best_ask
            mark_value = valuation.mark_value
            liquidation_value = valuation.liquidation_value
            cashout_ratio = valuation.cashout_ratio
            unrealized_pnl = valuation.unrealized_mark_pnl
            executable_pnl = valuation.executable_pnl
            latest_mark_payload = None
        elif latest_mark is not None:
            mark_price = latest_mark.get("midpoint")
            best_bid = latest_mark.get("best_bid")
            best_ask = latest_mark.get("best_ask")
            mark_value = safe_float(latest_mark.get("mark_value"))
            liquidation_value = safe_float(latest_mark.get("liquidation_value"))
            cashout_ratio = latest_mark.get("cashout_ratio")
            unrealized_pnl = safe_float(latest_mark.get("unrealized_pnl"))
            executable_pnl = safe_float(latest_mark.get("executable_pnl"))
            latest_mark_payload = WeatherStorage._paper_mark_payload(latest_mark)
        else:
            mark_price = optional_float(row.get("average_entry_price")) or 0.0
            best_bid = None
            best_ask = None
            mark_value = shares * mark_price
            liquidation_value = 0.0
            cashout_ratio = None
            unrealized_pnl = mark_value - safe_float(row["total_cost"])
            executable_pnl = -safe_float(row["total_cost"])
            latest_mark_payload = None
        return {
            "positionKey": row["position_key"],
            "accountKey": row["account_key"],
            "runKey": row["run_key"],
            "model": row["model"],
            "cityId": row["city_id"],
            "cityName": row["city_name"],
            "targetDate": row["target_date"],
            "kind": row["kind"],
            "outcome": row["outcome"],
            "bucketLabel": row["bucket_label"],
            "bucketKey": row["bucket_key"],
            "tokenId": row["token_id"],
            "openShares": shares,
            "totalCost": row["total_cost"],
            "averageEntryPrice": row["average_entry_price"],
            "realizedPnl": row["realized_pnl"],
            "status": row["status"],
            "markPrice": mark_price,
            "bestBid": best_bid,
            "bestAsk": best_ask,
            "markValue": mark_value,
            "liquidationValue": liquidation_value,
            "cashoutRatio": cashout_ratio,
            "unrealizedPnl": unrealized_pnl,
            "executablePnl": executable_pnl,
            "latestMark": latest_mark_payload,
            "openedAt": row["opened_at"],
            "updatedAt": row["updated_at"],
            "closedAt": row["closed_at"],
        }

    @staticmethod
    def _paper_mark_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "markKey": row["mark_key"],
            "accountKey": row["account_key"],
            "positionKey": row["position_key"],
            "tokenId": row["token_id"],
            "cityId": row["city_id"],
            "targetDate": row["target_date"],
            "kind": row["kind"],
            "outcome": row["outcome"],
            "bucketLabel": row["bucket_label"],
            "bucketKey": row["bucket_key"],
            "openShares": row["open_shares"],
            "totalCost": row["total_cost"],
            "averageEntryPrice": row["average_entry_price"],
            "bestBid": row["best_bid"],
            "bestAsk": row["best_ask"],
            "midpoint": row["midpoint"],
            "spread": row["spread"],
            "bidDepth": row["bid_depth"],
            "askDepth": row["ask_depth"],
            "markValue": row["mark_value"],
            "liquidationValue": row["liquidation_value"],
            "cashoutRatio": row["cashout_ratio"],
            "unrealizedPnl": row["unrealized_pnl"],
            "executablePnl": row["executable_pnl"],
            "exitSignal": row["exit_signal"],
            "warning": row["warning"],
            "fetchedAt": row["fetched_at"],
        }

    @staticmethod
    def _paper_settlement_payload(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "settlementRecordKey": row["settlement_record_key"],
            "accountKey": row["account_key"],
            "positionKey": row["position_key"],
            "runKey": row["run_key"],
            "model": row["model"],
            "settlementKey": row["settlement_key"],
            "cityId": row["city_id"],
            "targetDate": row["target_date"],
            "kind": row["kind"],
            "outcome": row["outcome"],
            "bucketLabel": row["bucket_label"],
            "bucketKey": row["bucket_key"],
            "actualBucketKey": row["actual_bucket_key"],
            "observedValue": row["observed_value"],
            "unit": row["unit"],
            "shares": row["shares"],
            "totalCost": row["total_cost"],
            "payout": row["payout"],
            "realizedPnl": row["realized_pnl"],
            "settledAt": row["settled_at"],
        }
