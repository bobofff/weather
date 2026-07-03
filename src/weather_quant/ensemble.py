"""Empirical ensemble-member probability calculations."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Sequence

from weather_quant.buckets import build_regular_buckets
from weather_quant.models import (
    CityConfig,
    EnsembleBucketProbability,
    EnsembleDistribution,
    EnsembleMemberDailyValue,
    EnsembleMemberForecast,
    EnsembleRun,
    MarketBucket,
    TemperatureBucket,
    TemperatureKind,
    TemperatureUnit,
    binary_contract_fee,
)
from weather_quant.portfolio import market_best_ask, market_best_bid, market_mark_price
from weather_quant.units import convert_temperature


class EnsembleError(RuntimeError):
    """Raised when ensemble data cannot be reduced to probabilities."""


def stable_payload_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _date_in_timezone(value: str, timezone_name: str) -> date:
    text = str(value)
    normalized = text.replace("Z", "+00:00")
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError:
        return date.fromisoformat(text[:10])

    if timestamp.tzinfo is None:
        return timestamp.date()
    if timezone_name and timezone_name.lower() not in {"auto", "gmt", "utc"}:
        try:
            return timestamp.astimezone(ZoneInfo(timezone_name)).date()
        except ZoneInfoNotFoundError:
            return timestamp.date()
    return timestamp.astimezone(timezone.utc).date()


def aggregate_member_daily_value(
    member: EnsembleMemberForecast,
    *,
    target_date: date,
    kind: TemperatureKind,
    timezone_name: str,
    unit: TemperatureUnit,
) -> EnsembleMemberDailyValue | None:
    paired_values: list[tuple[str, float | None]] = []
    for timestamp, value in zip(member.hourly_times, member.hourly_values, strict=False):
        if _date_in_timezone(timestamp, timezone_name) == target_date:
            paired_values.append((timestamp, value))
    numeric_values = [float(value) for _timestamp, value in paired_values if value is not None]
    if not numeric_values:
        return None
    daily_value = max(numeric_values) if kind == "high" else min(numeric_values)
    return EnsembleMemberDailyValue(
        member_id=member.member_id,
        target_date=target_date,
        kind=kind,
        value=convert_temperature(daily_value, from_unit=member.unit, to_unit=unit),
        unit=unit,
        hourly_times=tuple(timestamp for timestamp, _value in paired_values),
        hourly_values=tuple(value for _timestamp, value in paired_values),
    )


def build_ensemble_run(
    *,
    provider: str,
    model: str,
    city: CityConfig,
    target_date: date,
    kind: TemperatureKind,
    members: Sequence[EnsembleMemberForecast],
    raw_payload: Any | None = None,
    run_time: datetime | None = None,
    initialization_time: datetime | None = None,
) -> EnsembleRun:
    daily_values = tuple(
        value
        for member in members
        if (
            value := aggregate_member_daily_value(
                member,
                target_date=target_date,
                kind=kind,
                timezone_name=city.timezone,
                unit=city.settlement_unit,
            )
        )
        is not None
    )
    payload = raw_payload or {
        "provider": provider,
        "model": model,
        "city_id": city.city_id,
        "target_date": target_date.isoformat(),
        "kind": kind,
        "members": [
            {
                "member_id": member.member_id,
                "hourly_times": member.hourly_times,
                "hourly_values": member.hourly_values,
            }
            for member in members
        ],
    }
    payload_hash = stable_payload_hash(payload)
    run_key = ":".join(
        [
            provider,
            model,
            city.city_id,
            target_date.isoformat(),
            kind,
            payload_hash[:16],
        ]
    )
    return EnsembleRun(
        run_key=run_key,
        provider=provider,
        model=model,
        city=city,
        target_date=target_date,
        kind=kind,
        members=tuple(members),
        daily_values=daily_values,
        run_time=run_time,
        initialization_time=initialization_time,
        raw_payload=payload if isinstance(payload, dict) else {"payload": payload},
        payload_hash=payload_hash,
    )


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _std(values: Sequence[float], mean: float | None) -> float | None:
    if not values or mean is None:
        return None
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    lower_weight = upper - rank
    upper_weight = rank - lower
    return sorted_values[lower] * lower_weight + sorted_values[upper] * upper_weight


def _bucket_for_value(value: float, *, unit: TemperatureUnit, buckets: Sequence[TemperatureBucket]) -> TemperatureBucket | None:
    for bucket in buckets:
        converted = convert_temperature(value, from_unit=unit, to_unit=bucket.unit)
        if bucket.contains(converted):
            return bucket
    return None


def build_bucket_distribution(
    run: EnsembleRun,
    buckets: Sequence[TemperatureBucket],
    *,
    unit: TemperatureUnit | None = None,
) -> EnsembleDistribution:
    if not buckets:
        raise EnsembleError("No temperature buckets provided.")
    distribution_unit = unit or buckets[0].unit
    values = [
        convert_temperature(value.value, from_unit=value.unit, to_unit=distribution_unit)
        for value in run.daily_values
    ]
    total_members = len(run.daily_values)
    if total_members <= 0:
        raise EnsembleError("No ensemble member daily values available.")

    hit_counts = {bucket.canonical_key: 0 for bucket in buckets}
    matched_values: list[EnsembleMemberDailyValue] = []
    unmatched_count = 0
    for daily_value in run.daily_values:
        bucket = _bucket_for_value(daily_value.value, unit=daily_value.unit, buckets=buckets)
        if bucket is None:
            unmatched_count += 1
            matched_values.append(daily_value)
            continue
        hit_counts[bucket.canonical_key] += 1
        matched_values.append(
            EnsembleMemberDailyValue(
                member_id=daily_value.member_id,
                target_date=daily_value.target_date,
                kind=daily_value.kind,
                value=convert_temperature(
                    daily_value.value,
                    from_unit=daily_value.unit,
                    to_unit=distribution_unit,
                ),
                unit=distribution_unit,
                hourly_times=daily_value.hourly_times,
                hourly_values=daily_value.hourly_values,
                bucket_label=bucket.label,
                bucket_key=bucket.canonical_key,
            )
        )

    empirical_mean = _mean(values)
    empirical_std = _std(values, empirical_mean)
    p10 = _percentile(values, 0.10)
    p50 = _percentile(values, 0.50)
    p90 = _percentile(values, 0.90)
    probabilities = tuple(
        EnsembleBucketProbability(
            bucket=bucket,
            hit_count=hit_counts[bucket.canonical_key],
            probability=hit_counts[bucket.canonical_key] / total_members,
            total_members=total_members,
            unmatched_count=unmatched_count,
            empirical_mean=empirical_mean,
            empirical_std=empirical_std,
            p10=p10,
            p50=p50,
            p90=p90,
        )
        for bucket in buckets
    )
    return EnsembleDistribution(
        run=run,
        probabilities=probabilities,
        member_values=tuple(matched_values),
        unit=distribution_unit,
        total_members=total_members,
        unmatched_count=unmatched_count,
        empirical_mean=empirical_mean,
        empirical_std=empirical_std,
        p10=p10,
        p50=p50,
        p90=p90,
    )


def default_buckets_for_run(run: EnsembleRun, *, padding: int = 1) -> tuple[TemperatureBucket, ...]:
    if not run.daily_values:
        raise EnsembleError("No ensemble member daily values available.")
    unit = run.city.settlement_unit
    values = [
        convert_temperature(value.value, from_unit=value.unit, to_unit=unit)
        for value in run.daily_values
    ]
    start = math.floor(min(values)) - padding
    end = math.ceil(max(values)) + padding
    return build_regular_buckets(start=start, end=end, unit=unit, include_tails=True)


def ensemble_chart_data(
    distribution: EnsembleDistribution,
    *,
    market_buckets: Sequence[MarketBucket] = (),
) -> dict[str, Any]:
    market_by_key = {bucket.bucket.canonical_key: bucket for bucket in market_buckets}
    sorted_values = sorted(value.value for value in distribution.member_values)
    total = len(sorted_values)
    return {
        "bucketLabels": [item.bucket.label for item in distribution.probabilities],
        "bucketKeys": [item.bucket.canonical_key for item in distribution.probabilities],
        "bucketProbabilities": [item.probability for item in distribution.probabilities],
        "hitCounts": [item.hit_count for item in distribution.probabilities],
        "memberValues": [
            {
                "memberId": value.member_id,
                "value": value.value,
                "unit": value.unit,
                "bucketLabel": value.bucket_label,
                "bucketKey": value.bucket_key,
            }
            for value in distribution.member_values
        ],
        "cdfPoints": [
            {"value": value, "probability": (index + 1) / total}
            for index, value in enumerate(sorted_values)
        ],
        "marketImpliedProbabilities": [
            market_mark_price(market_by_key[item.bucket.canonical_key])
            if item.bucket.canonical_key in market_by_key
            else None
            for item in distribution.probabilities
        ],
    }


def ensemble_signal_rows(
    distribution: EnsembleDistribution,
    market_buckets: Sequence[MarketBucket],
    *,
    fee_rate: float,
    min_edge: float = 0.03,
) -> tuple[dict[str, Any], ...]:
    probability_by_key = {
        item.bucket.canonical_key: item
        for item in distribution.probabilities
    }
    rows: list[dict[str, Any]] = []
    for market_bucket in market_buckets:
        probability_item = probability_by_key.get(market_bucket.bucket.canonical_key)
        probability = probability_item.probability if probability_item else 0.0
        best_bid = market_best_bid(market_bucket)
        best_ask = market_best_ask(market_bucket)
        midpoint = market_mark_price(market_bucket)
        executable_entry_cost = best_ask if best_ask is not None else midpoint
        fee = binary_contract_fee(
            shares=1.0,
            price=executable_entry_cost,
            fee_rate=fee_rate,
        )
        expected_exit_cost = (
            max(0.0, midpoint - best_bid)
            if best_bid is not None and midpoint is not None
            else 0.0
        )
        edge = probability - executable_entry_cost - fee - expected_exit_cost
        rows.append(
            {
                "outcome": market_bucket.outcome,
                "bucketLabel": market_bucket.bucket.label,
                "bucketKey": market_bucket.bucket.canonical_key,
                "ensembleProbability": probability,
                "hitCount": probability_item.hit_count if probability_item else 0,
                "totalMembers": distribution.total_members,
                "marketMidpoint": midpoint,
                "bestBid": best_bid,
                "bestAsk": best_ask,
                "executableEntryCost": executable_entry_cost,
                "fee": fee,
                "expectedExitCost": expected_exit_cost,
                "edge": edge,
                "recommendation": "BUY_YES" if edge >= min_edge else "SKIP_NO_EDGE",
            }
        )
    return tuple(rows)
