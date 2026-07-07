"""Paper-trading helpers for Polymarket weather YES buckets."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from weather_quant.buckets import parse_temperature_bucket
from weather_quant.ensemble import stable_payload_hash
from weather_quant.models import (
    DEFAULT_TAKER_FEE_RATE,
    FillEstimate,
    HedgeLeg,
    MarketBucket,
    OrderBookLevel,
    OrderBookSnapshot,
    Portfolio,
    Position,
    TemperatureBucket,
    binary_contract_fee,
)
from weather_quant.portfolio import (
    calculate_hedge_lock,
    calculate_portfolio_scenarios,
    market_best_ask,
    market_best_bid,
    market_mark_price,
)


DEFAULT_ACCOUNT_KEY = "default"
DEFAULT_INITIAL_CASH = 1_000.0
DEFAULT_STAKE_USDC = 25.0
DEFAULT_MIN_EDGE = 0.03
DEFAULT_MAX_SPREAD = 0.12
DEFAULT_MIN_ASK_DEPTH_SHARES = 1.0
DEFAULT_MAX_MARKET_EXPOSURE = 100.0
DEFAULT_MAX_CITY_DATE_EXPOSURE = 200.0


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_float(value: Any, default: float = 0.0) -> float:
    parsed = optional_float(value)
    return default if parsed is None else parsed


def optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def clamp_probability(value: float | None) -> float:
    return max(0.0, min(1.0, float(value or 0.0)))


def bucket_from_key(
    *,
    bucket_label: str,
    bucket_key: str | None,
    default_unit: str = "F",
) -> TemperatureBucket:
    if bucket_key:
        parts = bucket_key.split(":")
        if len(parts) == 3 and parts[0] in {"F", "C"}:
            lower = None if parts[1] == "-inf" else float(parts[1])
            upper = None if parts[2] == "inf" else float(parts[2])
            return TemperatureBucket(
                label=bucket_label,
                lower=lower,
                upper=upper,
                unit=parts[0],  # type: ignore[arg-type]
            )
    return parse_temperature_bucket(bucket_label, default_unit=default_unit)  # type: ignore[arg-type]


def market_bucket_from_payload(
    row: Mapping[str, Any],
    *,
    default_unit: str = "F",
) -> MarketBucket:
    bucket_payload = row.get("bucket")
    if isinstance(bucket_payload, Mapping):
        label = str(
            bucket_payload.get("label")
            or row.get("bucketLabel")
            or row.get("outcome")
            or ""
        )
        bucket = TemperatureBucket(
            label=label,
            lower=optional_float(bucket_payload.get("lower")),
            upper=optional_float(bucket_payload.get("upper")),
            unit=str(bucket_payload.get("unit") or default_unit),  # type: ignore[arg-type]
            lower_inclusive=bool(bucket_payload.get("lowerInclusive", True)),
            upper_inclusive=bool(bucket_payload.get("upperInclusive", False)),
        )
    else:
        label = str(row.get("bucketLabel") or row.get("outcome") or row.get("label") or "")
        bucket = bucket_from_key(
            bucket_label=label,
            bucket_key=optional_text(row.get("bucketKey") or row.get("bucket_key")),
            default_unit=default_unit,
        )

    outcome = str(row.get("outcome") or bucket.label)
    orderbook = orderbook_from_payload(row, token_id=optional_text(row.get("tokenId") or row.get("token_id")))
    price = safe_float(row.get("price") or row.get("marketPrice") or row.get("markPrice"))
    if price <= 0 and orderbook is not None and orderbook.midpoint is not None:
        price = orderbook.midpoint
    return MarketBucket(
        market_id=str(row.get("marketId") or row.get("market_id") or row.get("slug") or "paper"),
        question=str(row.get("question") or "Paper weather market"),
        slug=optional_text(row.get("slug") or row.get("marketSlug") or row.get("market_slug")),
        condition_id=optional_text(row.get("conditionId") or row.get("condition_id")),
        outcome=outcome,
        price=price,
        bucket=bucket,
        token_id=optional_text(row.get("tokenId") or row.get("token_id")),
        orderbook=orderbook,
        raw_payload=dict(row),
    )


def orderbook_from_payload(
    row: Mapping[str, Any],
    *,
    token_id: str | None,
) -> OrderBookSnapshot | None:
    raw_orderbook = row.get("orderbook")
    bids: list[OrderBookLevel] = []
    asks: list[OrderBookLevel] = []
    if isinstance(raw_orderbook, Mapping):
        bids.extend(levels_from_payload(raw_orderbook.get("bids")))
        asks.extend(levels_from_payload(raw_orderbook.get("asks")))
    bids.extend(levels_from_payload(row.get("bids")))
    asks.extend(levels_from_payload(row.get("asks")))

    best_bid = optional_float(row.get("bestBid") or row.get("best_bid") or row.get("bid"))
    best_ask = optional_float(row.get("bestAsk") or row.get("best_ask") or row.get("ask"))
    if best_bid is not None and not bids:
        bids.append(OrderBookLevel(price=best_bid, size=safe_float(row.get("bidSize") or row.get("bid_size"), 10_000.0)))
    if best_ask is not None and not asks:
        asks.append(OrderBookLevel(price=best_ask, size=safe_float(row.get("askSize") or row.get("ask_size"), 10_000.0)))
    if not bids and not asks:
        return None
    return OrderBookSnapshot(
        token_id=token_id or str(row.get("outcome") or "paper"),
        bids=tuple(bids),
        asks=tuple(asks),
    )


def levels_from_payload(raw_levels: Any) -> list[OrderBookLevel]:
    if not isinstance(raw_levels, Sequence) or isinstance(raw_levels, (str, bytes)):
        return []
    levels: list[OrderBookLevel] = []
    for item in raw_levels:
        if not isinstance(item, Mapping):
            continue
        price = optional_float(item.get("price"))
        size = optional_float(item.get("size"))
        if price is None or size is None:
            continue
        levels.append(OrderBookLevel(price=price, size=size))
    return levels


def market_buckets_from_payload(
    rows: Sequence[Mapping[str, Any]],
    *,
    default_unit: str = "F",
) -> tuple[MarketBucket, ...]:
    return tuple(market_bucket_from_payload(row, default_unit=default_unit) for row in rows)


def find_market_bucket(
    market_buckets: Sequence[MarketBucket],
    signal: Mapping[str, Any],
) -> MarketBucket | None:
    bucket_key = optional_text(signal.get("bucketKey") or signal.get("bucket_key"))
    outcome = optional_text(signal.get("outcome"))
    token_id = optional_text(signal.get("tokenId") or signal.get("token_id"))
    for market_bucket in market_buckets:
        if bucket_key and market_bucket.bucket.canonical_key == bucket_key:
            return market_bucket
        if token_id and market_bucket.token_id == token_id:
            return market_bucket
        if outcome and market_bucket.outcome.strip().lower() == outcome.lower():
            return market_bucket
    return None


def paper_position_key(
    *,
    account_key: str,
    city_id: str | None,
    target_date: str | None,
    kind: str | None,
    bucket_key: str,
    token_id: str | None,
) -> str:
    digest = stable_payload_hash(
        {
            "account_key": account_key,
            "city_id": city_id,
            "target_date": target_date,
            "kind": kind,
            "bucket_key": bucket_key,
            "token_id": token_id,
        }
    )[:20]
    return f"paper-position:{digest}"


def paper_buy_preview(
    *,
    signal: Mapping[str, Any],
    market_bucket: MarketBucket | None,
    account: Mapping[str, Any],
    open_positions: Sequence[Mapping[str, Any]] = (),
    context: Mapping[str, Any] | None = None,
    stake_usdc: float = DEFAULT_STAKE_USDC,
    min_edge: float = DEFAULT_MIN_EDGE,
    fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    max_spread: float = DEFAULT_MAX_SPREAD,
    min_ask_depth_shares: float = DEFAULT_MIN_ASK_DEPTH_SHARES,
    max_market_exposure: float = DEFAULT_MAX_MARKET_EXPOSURE,
    max_city_date_exposure: float = DEFAULT_MAX_CITY_DATE_EXPOSURE,
) -> dict[str, Any]:
    ctx = dict(context or {})
    account_key = str(account.get("account_key") or account.get("accountKey") or DEFAULT_ACCOUNT_KEY)
    cash_balance = safe_float(first_present(account.get("cash_balance"), account.get("cashBalance")), DEFAULT_INITIAL_CASH)
    stake = max(0.0, float(stake_usdc))
    recommendation = str(signal.get("recommendation") or "")
    probability = clamp_probability(optional_float(first_present(signal.get("ensembleProbability"), signal.get("ensemble_probability"))))
    expected_exit_cost = safe_float(
        first_present(signal.get("expectedExitCost"), signal.get("expected_exit_cost")),
        0.0,
    )

    if market_bucket is None:
        return _rejected_preview(
            signal=signal,
            account_key=account_key,
            cash_balance=cash_balance,
            stake=stake,
            reason="NO_ASK" if recommendation == "BUY_YES" else "NO_BUY_SIGNAL",
            context=ctx,
        )

    base = _preview_base(
        signal=signal,
        market_bucket=market_bucket,
        account_key=account_key,
        cash_balance=cash_balance,
        stake=stake,
        context=ctx,
    )
    if recommendation != "BUY_YES":
        return {**base, "accepted": False, "status": "REJECTED", "rejectReason": "NO_BUY_SIGNAL"}

    orderbook = market_bucket.orderbook
    if orderbook is None or orderbook.best_ask is None or not orderbook.asks:
        return {**base, "accepted": False, "status": "REJECTED", "rejectReason": "NO_ASK"}

    fill = orderbook.estimate_market_buy(stake, fee_rate=fee_rate)
    fee_per_share = fill.fee / fill.filled_shares if fill.filled_shares > 0 else 0.0
    entry_cost = fill.vwap
    edge = (
        probability - entry_cost - fee_per_share - expected_exit_cost
        if entry_cost is not None
        else -1.0
    )
    ask_depth = sum(level.size for level in orderbook.asks)
    spread = orderbook.spread
    enriched = {
        **base,
        "ensembleProbability": probability,
        "executableEntryCost": entry_cost,
        "expectedExitCost": expected_exit_cost,
        "fee": fill.fee,
        "feePerShare": fee_per_share,
        "edge": edge,
        "filledShares": fill.filled_shares,
        "vwap": fill.vwap,
        "averagePrice": fill.effective_price,
        "netCost": fill.net_value,
        "notional": fill.notional,
        "askDepth": ask_depth,
        "fill": fill_to_payload(fill),
    }

    if edge < min_edge:
        return {**enriched, "accepted": False, "status": "REJECTED", "rejectReason": "EDGE_TOO_LOW"}
    if stake > cash_balance or fill.net_value > cash_balance:
        return {**enriched, "accepted": False, "status": "REJECTED", "rejectReason": "INSUFFICIENT_BALANCE"}
    if spread is not None and spread > max_spread:
        return {**enriched, "accepted": False, "status": "REJECTED", "rejectReason": "SPREAD_TOO_WIDE"}
    if not fill.is_complete or fill.filled_shares < min_ask_depth_shares:
        return {**enriched, "accepted": False, "status": "REJECTED", "rejectReason": "INSUFFICIENT_DEPTH"}

    exposure = exposure_after_buy(
        open_positions=open_positions,
        preview=enriched,
    )
    enriched["exposure"] = exposure
    if exposure["sameMarketCostAfter"] > max_market_exposure or exposure["cityDateCostAfter"] > max_city_date_exposure:
        return {**enriched, "accepted": False, "status": "REJECTED", "rejectReason": "EXPOSURE_LIMIT"}
    if not settlement_source_present(signal=signal, context=ctx):
        return {**enriched, "accepted": False, "status": "REJECTED", "rejectReason": "MISSING_SETTLEMENT_SOURCE"}

    return {
        **enriched,
        "accepted": True,
        "status": "ACCEPTED",
        "rejectReason": None,
    }


def _preview_base(
    *,
    signal: Mapping[str, Any],
    market_bucket: MarketBucket,
    account_key: str,
    cash_balance: float,
    stake: float,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    best_bid = market_best_bid(market_bucket)
    best_ask = market_best_ask(market_bucket)
    spread = (
        max(0.0, best_ask - best_bid)
        if best_bid is not None and best_ask is not None
        else None
    )
    city_id = optional_text(context.get("cityId") or context.get("city_id") or signal.get("cityId") or signal.get("city_id"))
    target_date = optional_text(context.get("targetDate") or context.get("target_date") or signal.get("targetDate") or signal.get("target_date"))
    kind = optional_text(context.get("kind") or context.get("temperatureKind") or signal.get("kind"))
    bucket_label = str(signal.get("bucketLabel") or market_bucket.bucket.label)
    bucket_key = str(signal.get("bucketKey") or market_bucket.bucket.canonical_key)
    return {
        "accepted": False,
        "status": "REJECTED",
        "rejectReason": None,
        "accountKey": account_key,
        "cashBalanceBefore": cash_balance,
        "stakeUsdc": stake,
        "side": "BUY_YES",
        "orderType": "MARKET_BUY",
        "outcome": market_bucket.outcome,
        "bucketLabel": bucket_label,
        "bucketKey": bucket_key,
        "tokenId": market_bucket.token_id,
        "marketSlug": market_bucket.slug,
        "conditionId": market_bucket.condition_id,
        "cityId": city_id,
        "cityName": optional_text(context.get("cityName") or context.get("city_name")),
        "targetDate": target_date,
        "kind": kind,
        "settlementStation": optional_text(context.get("settlementStation") or context.get("settlement_station") or signal.get("settlementStation")),
        "stationId": optional_text(context.get("stationId") or context.get("station_id") or signal.get("stationId")),
        "metarSource": optional_text(context.get("metarSource") or context.get("metar_source") or signal.get("metarSource")),
        "signalSnapshotId": signal.get("signalSnapshotId") or signal.get("signal_snapshot_id"),
        "runKey": signal.get("runKey") or signal.get("run_key") or context.get("runKey"),
        "marketSnapshotGroup": signal.get("marketSnapshotGroup") or signal.get("market_snapshot_group") or context.get("marketSnapshotGroup"),
        "ensembleProbability": optional_float(signal.get("ensembleProbability") or signal.get("ensemble_probability")),
        "executableEntryCost": optional_float(signal.get("executableEntryCost") or signal.get("executable_entry_cost")),
        "expectedExitCost": optional_float(signal.get("expectedExitCost") or signal.get("expected_exit_cost")),
        "fee": optional_float(signal.get("fee")) or 0.0,
        "edge": optional_float(signal.get("edge")),
        "filledShares": 0.0,
        "vwap": None,
        "averagePrice": None,
        "netCost": 0.0,
        "notional": 0.0,
        "bestBid": best_bid,
        "bestAsk": best_ask,
        "spread": spread,
        "askDepth": sum(level.size for level in market_bucket.orderbook.asks) if market_bucket.orderbook else optional_float(signal.get("askDepth") or signal.get("ask_depth")),
        "noRealOrder": True,
    }


def _rejected_preview(
    *,
    signal: Mapping[str, Any],
    account_key: str,
    cash_balance: float,
    stake: float,
    reason: str,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "accepted": False,
        "status": "REJECTED",
        "rejectReason": reason,
        "accountKey": account_key,
        "cashBalanceBefore": cash_balance,
        "stakeUsdc": stake,
        "side": "BUY_YES",
        "orderType": "MARKET_BUY",
        "outcome": signal.get("outcome"),
        "bucketLabel": signal.get("bucketLabel"),
        "bucketKey": signal.get("bucketKey"),
        "tokenId": signal.get("tokenId"),
        "cityId": context.get("cityId"),
        "targetDate": context.get("targetDate"),
        "kind": context.get("kind"),
        "ensembleProbability": optional_float(signal.get("ensembleProbability")),
        "edge": optional_float(signal.get("edge")),
        "filledShares": 0.0,
        "fee": 0.0,
        "netCost": 0.0,
        "noRealOrder": True,
    }


def settlement_source_present(
    *,
    signal: Mapping[str, Any],
    context: Mapping[str, Any],
) -> bool:
    return bool(
        optional_text(context.get("stationId") or context.get("station_id") or signal.get("stationId"))
        or optional_text(context.get("settlementStation") or context.get("settlement_station") or signal.get("settlementStation"))
    )


def exposure_after_buy(
    *,
    open_positions: Sequence[Mapping[str, Any]],
    preview: Mapping[str, Any],
) -> dict[str, float]:
    add_cost = safe_float(preview.get("netCost"))
    condition_id = optional_text(preview.get("conditionId"))
    city_id = optional_text(preview.get("cityId"))
    target_date = optional_text(preview.get("targetDate"))
    kind = optional_text(preview.get("kind"))
    same_market = 0.0
    city_date = 0.0
    for position in open_positions:
        cost = safe_float(position.get("total_cost") or position.get("totalCost"))
        if condition_id and optional_text(position.get("condition_id") or position.get("conditionId")) == condition_id:
            same_market += cost
        if (
            city_id
            and target_date
            and kind
            and optional_text(position.get("city_id") or position.get("cityId")) == city_id
            and optional_text(position.get("target_date") or position.get("targetDate")) == target_date
            and optional_text(position.get("kind")) == kind
        ):
            city_date += cost
    return {
        "sameMarketCostBefore": same_market,
        "sameMarketCostAfter": same_market + add_cost,
        "cityDateCostBefore": city_date,
        "cityDateCostAfter": city_date + add_cost,
    }


def fill_to_payload(fill: FillEstimate) -> dict[str, Any]:
    return {
        "side": fill.side,
        "requestedUsdc": fill.requested_usdc,
        "requestedShares": fill.requested_shares,
        "filledShares": fill.filled_shares,
        "notional": fill.notional,
        "fee": fill.fee,
        "netValue": fill.net_value,
        "vwap": fill.vwap,
        "effectivePrice": fill.effective_price,
        "slippage": fill.slippage,
        "isComplete": fill.is_complete,
        "remainingUsdc": fill.remaining_usdc,
        "remainingShares": fill.remaining_shares,
        "bestPrice": fill.best_price,
        "worstPrice": fill.worst_price,
        "feeRate": fill.fee_rate,
    }


def position_from_mapping(row: Mapping[str, Any]) -> Position:
    bucket_label = str(row.get("bucket_label") or row.get("bucketLabel") or row.get("outcome") or "")
    bucket_key = optional_text(row.get("bucket_key") or row.get("bucketKey"))
    bucket = bucket_from_key(bucket_label=bucket_label, bucket_key=bucket_key)
    shares = safe_float(row.get("open_shares") or row.get("openShares") or row.get("shares"))
    total_cost = safe_float(row.get("total_cost") or row.get("totalCost"))
    average_price = optional_float(row.get("average_entry_price") or row.get("averageEntryPrice"))
    return Position(
        outcome=str(row.get("outcome") or bucket_label),
        bucket=bucket,
        shares=shares,
        total_cost=total_cost,
        token_id=optional_text(row.get("token_id") or row.get("tokenId")),
        market_id=optional_text(row.get("condition_id") or row.get("conditionId")),
        slug=optional_text(row.get("market_slug") or row.get("marketSlug")),
        average_entry_price=average_price,
        settlement_station=optional_text(row.get("settlement_station") or row.get("settlementStation")),
        station_id=optional_text(row.get("station_id") or row.get("stationId")),
        metar_source=optional_text(row.get("metar_source") or row.get("metarSource")),
        raw_payload=dict(row),
    )


def exit_preview(
    *,
    position: Mapping[str, Any],
    market_bucket: MarketBucket | None,
    shares: float | None = None,
    fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    target_profit: float = 0.10,
    signal_edge: float | None = None,
    min_cashout_ratio: float = 0.50,
) -> dict[str, Any]:
    open_shares = safe_float(position.get("open_shares") or position.get("openShares") or position.get("shares"))
    sell_shares = min(open_shares, max(0.0, shares if shares is not None else open_shares))
    total_cost = safe_float(position.get("total_cost") or position.get("totalCost"))
    average_entry = safe_float(position.get("average_entry_price") or position.get("averageEntryPrice"), total_cost / open_shares if open_shares > 0 else 0.0)
    if market_bucket is None or market_bucket.orderbook is None or market_bucket.orderbook.best_bid is None:
        return {
            "accepted": False,
            "rejectReason": "NO_BID",
            "shares": sell_shares,
            "allocatedCost": 0.0,
            "sellValue": 0.0,
            "realizedPnl": 0.0,
        }
    estimate = market_bucket.orderbook.estimate_market_sell(sell_shares, fee_rate=fee_rate)
    allocated_cost = total_cost * (sell_shares / open_shares) if open_shares > 0 else 0.0
    realized_pnl = estimate.net_value - allocated_cost
    mark_price = market_mark_price(market_bucket)
    cashout_ratio = market_bucket.orderbook.cashout_ratio(
        shares=sell_shares,
        mark_price=mark_price,
        fee_rate=fee_rate,
    )
    triggers = []
    if market_bucket.orderbook.best_bid >= average_entry + target_profit:
        triggers.append("TARGET_PROFIT")
    if signal_edge is not None and signal_edge < 0:
        triggers.append("EDGE_TURNED_NEGATIVE")
    if cashout_ratio is not None and cashout_ratio >= min_cashout_ratio:
        triggers.append("CASHOUT_RATIO")
    if not estimate.is_complete:
        return {
            "accepted": False,
            "rejectReason": "INSUFFICIENT_DEPTH",
            "shares": sell_shares,
            "allocatedCost": allocated_cost,
            "sellValue": estimate.net_value,
            "realizedPnl": realized_pnl,
            "fill": fill_to_payload(estimate),
            "triggers": triggers,
        }
    return {
        "accepted": True,
        "rejectReason": None,
        "outcome": position.get("outcome") or market_bucket.outcome,
        "shares": sell_shares,
        "allocatedCost": allocated_cost,
        "sellValue": estimate.net_value,
        "realizedPnl": realized_pnl,
        "averageEntryPrice": average_entry,
        "bestBid": market_bucket.orderbook.best_bid,
        "cashoutRatio": cashout_ratio,
        "fill": fill_to_payload(estimate),
        "triggers": triggers,
        "autoExecute": False,
    }


def hedge_preview(
    *,
    signals: Sequence[Mapping[str, Any]],
    market_buckets: Sequence[MarketBucket],
    open_positions: Sequence[Mapping[str, Any]] = (),
    account: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    stake_usdc: float = DEFAULT_STAKE_USDC,
    fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    target_profit: float = 0.0,
    tail_probability_cutoff: float = 0.05,
    max_tail_probability: float = 0.05,
    min_adjacent_probability: float = 0.10,
) -> dict[str, Any]:
    probability_map = probability_map_from_signals(signals, market_buckets)
    positions = [position_from_mapping(row) for row in open_positions if safe_float(row.get("open_shares") or row.get("openShares") or row.get("shares")) > 0]
    main_signal = best_buy_signal(signals)
    main_bucket = find_market_bucket(market_buckets, main_signal) if main_signal else None
    hypothetical = None
    if not positions and main_signal and main_bucket is not None and account is not None:
        preview = paper_buy_preview(
            signal=main_signal,
            market_bucket=main_bucket,
            account=account,
            open_positions=open_positions,
            context=context or {},
            stake_usdc=stake_usdc,
            min_edge=0.0,
            fee_rate=fee_rate,
        )
        if preview.get("accepted"):
            hypothetical = preview
            positions.append(
                Position(
                    outcome=str(preview["outcome"]),
                    bucket=main_bucket.bucket,
                    shares=safe_float(preview.get("filledShares")),
                    total_cost=safe_float(preview.get("netCost")),
                    token_id=main_bucket.token_id,
                    market_id=main_bucket.condition_id,
                    slug=main_bucket.slug,
                    average_entry_price=optional_float(preview.get("averagePrice")),
                )
            )
    portfolio = Portfolio(positions=tuple(positions))
    adjacent = adjacent_hedge_preview(
        portfolio=portfolio,
        main_bucket=main_bucket or main_bucket_from_positions(positions, market_buckets),
        market_buckets=market_buckets,
        probability_map=probability_map,
        stake_usdc=stake_usdc,
        fee_rate=fee_rate,
        min_adjacent_probability=min_adjacent_probability,
    )
    lock = calculate_hedge_lock(
        portfolio,
        market_buckets,
        probabilities=probability_map,
        target_profit=target_profit,
        tail_probability_cutoff=tail_probability_cutoff,
        max_tail_probability=max_tail_probability,
        fee_rate=fee_rate,
    )
    return {
        "summary": {
            "positionCount": len(portfolio.positions),
            "hypotheticalMainBuy": hypothetical,
            "recommendation": adjacent.get("recommendation") or lock.recommendation,
            "coveredProbability": lock.covered_probability,
            "uncoveredTailProbability": lock.uncovered_tail_probability,
            "coveredWorstCasePnl": lock.covered_worst_case_pnl,
            "globalWorstCasePnl": lock.worst_case_pnl,
            "isTailRiskLock": lock.is_tail_risk_lock,
            "isTrueArbitrage": lock.is_true_arbitrage,
            "tailRiskLockDisclaimer": "This is not risk-free arbitrage; uncovered tail buckets can still lose.",
        },
        "adjacent": adjacent,
        "tailRiskLock": {
            "recommendation": lock.recommendation,
            "coveredProbability": lock.covered_probability,
            "uncoveredTailProbability": lock.uncovered_tail_probability,
            "coveredWorstCasePnl": lock.covered_worst_case_pnl,
            "globalWorstCasePnl": lock.worst_case_pnl,
            "hedgeCost": lock.hedge_cost,
            "lockProfit": lock.lock_profit,
            "isTailRiskLock": lock.is_tail_risk_lock,
            "isTrueArbitrage": lock.is_true_arbitrage,
            "notes": list(lock.notes),
            "hedgeLegs": [
                {
                    "outcome": leg.outcome,
                    "shares": leg.shares,
                    "price": leg.price,
                    "cost": leg.cost,
                    "fee": leg.fee,
                    "totalCost": leg.total_cost,
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
        },
    }


def probability_map_from_signals(
    signals: Sequence[Mapping[str, Any]],
    market_buckets: Sequence[MarketBucket],
) -> dict[str, float]:
    result: dict[str, float] = {}
    for signal in signals:
        key = optional_text(signal.get("bucketKey") or signal.get("bucket_key"))
        if key:
            result[key] = clamp_probability(optional_float(signal.get("ensembleProbability") or signal.get("ensemble_probability")))
    for bucket in market_buckets:
        result.setdefault(bucket.bucket.canonical_key, clamp_probability(market_mark_price(bucket)))
    return result


def best_buy_signal(signals: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    buy_signals = [signal for signal in signals if str(signal.get("recommendation") or "") == "BUY_YES"]
    if not buy_signals:
        return None
    return max(
        buy_signals,
        key=lambda signal: (
            safe_float(signal.get("edge"), -99.0),
            safe_float(signal.get("ensembleProbability") or signal.get("ensemble_probability")),
        ),
    )


def main_bucket_from_positions(
    positions: Sequence[Position],
    market_buckets: Sequence[MarketBucket],
) -> MarketBucket | None:
    if not positions:
        return None
    main = max(positions, key=lambda position: position.total_cost)
    for bucket in market_buckets:
        if bucket.bucket.canonical_key == main.bucket.canonical_key:
            return bucket
    return None


def adjacent_hedge_preview(
    *,
    portfolio: Portfolio,
    main_bucket: MarketBucket | None,
    market_buckets: Sequence[MarketBucket],
    probability_map: Mapping[str, float],
    stake_usdc: float,
    fee_rate: float,
    min_adjacent_probability: float,
) -> dict[str, Any]:
    if main_bucket is None or not portfolio.positions:
        return {"recommendation": "NO_MAIN_BUCKET", "reason": "No main bucket is available."}
    candidates = [
        bucket
        for bucket in market_buckets
        if bucket.bucket.canonical_key != main_bucket.bucket.canonical_key
        and buckets_are_adjacent(main_bucket.bucket, bucket.bucket)
        and probability_map.get(bucket.bucket.canonical_key, 0.0) >= min_adjacent_probability
    ]
    if not candidates:
        return {
            "recommendation": "NO_ADJACENT_HEDGE",
            "mainOutcome": main_bucket.outcome,
            "reason": "No adjacent bucket has enough model probability.",
        }
    adjacent = max(candidates, key=lambda bucket: probability_map.get(bucket.bucket.canonical_key, 0.0))
    if adjacent.orderbook is None or adjacent.orderbook.best_ask is None:
        return {
            "recommendation": "NO_ASK",
            "mainOutcome": main_bucket.outcome,
            "adjacentOutcome": adjacent.outcome,
            "reason": "The adjacent bucket has no executable ask.",
        }
    fill = adjacent.orderbook.estimate_market_buy(stake_usdc, fee_rate=fee_rate)
    if not fill.is_complete or fill.filled_shares <= 0 or fill.vwap is None:
        return {
            "recommendation": "INSUFFICIENT_DEPTH",
            "mainOutcome": main_bucket.outcome,
            "adjacentOutcome": adjacent.outcome,
            "fill": fill_to_payload(fill),
        }
    before = calculate_portfolio_scenarios(
        portfolio,
        market_buckets,
        probabilities=probability_map,
    )
    hedge_leg = HedgeLeg(
        outcome=adjacent.outcome,
        bucket=adjacent.bucket,
        shares=fill.filled_shares,
        price=fill.vwap,
        cost=fill.notional,
        fee=fill.fee,
        token_id=adjacent.token_id,
        reason="Adjacent bucket boundary-risk preview.",
    )
    after = calculate_portfolio_scenarios(
        portfolio,
        market_buckets,
        probabilities=probability_map,
        hedge_legs=(hedge_leg,),
    )
    before_worst = min((scenario.net_pnl for scenario in before), default=-portfolio.total_cost)
    after_worst = min((scenario.net_pnl for scenario in after), default=before_worst)
    risk_reduction = after_worst - before_worst
    recommendation = "HEDGE_ADJACENT" if risk_reduction > 0.01 else "NO_ADJACENT_HEDGE"
    return {
        "recommendation": recommendation,
        "mainOutcome": main_bucket.outcome,
        "mainProbability": probability_map.get(main_bucket.bucket.canonical_key, 0.0),
        "adjacentOutcome": adjacent.outcome,
        "adjacentProbability": probability_map.get(adjacent.bucket.canonical_key, 0.0),
        "hedgeShares": fill.filled_shares,
        "hedgeCost": fill.net_value,
        "vwap": fill.vwap,
        "beforeWorstCasePnl": before_worst,
        "afterWorstCasePnl": after_worst,
        "riskReduction": risk_reduction,
        "fill": fill_to_payload(fill),
        "reason": "Adjacent bucket improves the worst boundary-risk scenario." if recommendation == "HEDGE_ADJACENT" else "Adjacent bucket does not materially improve the worst scenario.",
    }


def buckets_are_adjacent(left: TemperatureBucket, right: TemperatureBucket) -> bool:
    if left.unit != right.unit:
        return False
    tolerance = 1e-9
    return (
        left.upper is not None
        and right.lower is not None
        and abs(left.upper - right.lower) <= tolerance
    ) or (
        right.upper is not None
        and left.lower is not None
        and abs(right.upper - left.lower) <= tolerance
    )
