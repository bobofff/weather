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
from weather_quant.market import GammaMarketClient, MarketDataError
from weather_quant.models import DEFAULT_TAKER_FEE_RATE, Portfolio, TemperatureKind
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
    return models[0] if models else "gfs_seamless"


def _save_requested(payload: dict[str, Any]) -> bool:
    return _bool_value(payload.get("saveSqlite") or payload.get("save"), default=False)


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


def _optional_market_buckets_for_payload(payload: dict[str, Any], *, unit: str) -> tuple:
    markets_text = str(payload.get("marketsCsv") or "")
    if markets_text.strip():
        return parse_inline_market_buckets(markets_text, default_unit=unit)  # type: ignore[arg-type]
    if _optional_text(payload.get("marketQuery") or payload.get("query")) or _optional_text(
        payload.get("marketSlug") or payload.get("slug")
    ) or _optional_text(payload.get("conditionId") or payload.get("condition_id")):
        buckets, _selector = _load_live_market_buckets(payload)
        return buckets
    return ()


def _selector_from_payload(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    query = _optional_text(payload.get("marketQuery") or payload.get("query"))
    if query is None:
        city = _optional_text(payload.get("city") or payload.get("cityId"))
        kind = _optional_text(payload.get("temperatureKind") or payload.get("kind"))
        if city and kind:
            query = f"{city} {kind} temperature"
        elif city:
            query = f"{city} temperature"
    slug = _optional_text(payload.get("marketSlug") or payload.get("slug"))
    condition_id = _optional_text(payload.get("conditionId") or payload.get("condition_id"))
    return query, slug, condition_id


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


def _level_payload(levels, *, limit: int):  # noqa: ANN001
    return [
        {"price": level.price, "size": level.size}
        for level in tuple(levels)[: max(0, limit)]
    ]


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
        },
        "buckets": [
            {
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
                "orderbook": {
                    "bids": _level_payload(bucket.orderbook.bids, limit=depth_limit),
                    "asks": _level_payload(bucket.orderbook.asks, limit=depth_limit),
                } if bucket.orderbook else None,
            }
            for bucket in buckets
        ],
    }


def forecast_payload(payload: dict[str, Any]) -> dict[str, Any]:
    city_text = _optional_text(payload.get("city") or payload.get("cityId"))
    if not city_text:
        raise ValueError("Provide city.")
    city = get_city(city_text)
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
            "targetDate": target_date.isoformat(),
            "kind": kind,
            "unit": city.settlement_unit,
            "modelCount": len(ensemble.points),
            "models": list(ensemble.source_models),
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
    city_text = _optional_text(payload.get("city") or payload.get("cityId"))
    if not city_text:
        raise ValueError("Provide city.")
    city = get_city(city_text)
    target_date = _target_date_from_payload(payload)
    kind = _kind_from_payload(payload)
    model = _model_from_payload(payload)
    forecast_days = payload.get("forecastDays") or payload.get("forecast_days")
    forecast_days_int = int(forecast_days) if forecast_days not in (None, "") else None
    client = OpenMeteoEnsembleClient()
    run = client.fetch_run(
        city,
        target_date=target_date,
        kind=kind,
        model=model,
        forecast_days=forecast_days_int,
    )
    market_buckets = _optional_market_buckets_for_payload(payload, unit=city.settlement_unit)
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


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    handlers = {
        "portfolio": portfolio_payload,
        "markets": market_payload,
        "forecast": forecast_payload,
        "ensemble": ensemble_payload,
        "ensemble-signal": ensemble_signal_payload,
        "db-runs": db_runs_payload,
        "db-probabilities": db_probabilities_payload,
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
