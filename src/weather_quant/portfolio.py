"""Portfolio, passive execution, and hedge-lock helpers for weather buckets."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from weather_quant.buckets import parse_temperature_bucket
from weather_quant.models import (
    DEFAULT_MAKER_FEE_RATE,
    DEFAULT_TAKER_FEE_RATE,
    ExitLadderLeg,
    HedgeLeg,
    MarketBucket,
    OrderBookLevel,
    OrderBookSnapshot,
    PassiveEntryRecommendation,
    PassiveExitPlan,
    Portfolio,
    PortfolioLockResult,
    PortfolioScenario,
    Position,
    PositionValuation,
    TemperatureBucket,
    TemperatureUnit,
    binary_contract_fee,
)


DEFAULT_EXIT_FRACTIONS = (0.20, 0.30, 0.30)
DEFAULT_RETAIN_FRACTION = 0.20


def clamp_price(value: float) -> float:
    return max(0.0, min(1.0, value))


def bucket_key(bucket: TemperatureBucket) -> str:
    return bucket.canonical_key


def market_bucket_key(market_bucket: MarketBucket) -> str:
    return bucket_key(market_bucket.bucket)


def position_key(position: Position) -> str:
    return bucket_key(position.bucket)


def _outcome_key(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _market_index(market_buckets: Sequence[MarketBucket]) -> dict[str, MarketBucket]:
    index: dict[str, MarketBucket] = {}
    for market_bucket in market_buckets:
        index[market_bucket_key(market_bucket)] = market_bucket
        index[_outcome_key(market_bucket.outcome)] = market_bucket
    return index


def _find_market_bucket(
    position: Position,
    market_buckets: Sequence[MarketBucket],
) -> MarketBucket | None:
    index = _market_index(market_buckets)
    return index.get(position_key(position)) or index.get(_outcome_key(position.outcome))


def market_mark_price(market_bucket: MarketBucket) -> float | None:
    orderbook = market_bucket.orderbook
    if orderbook is not None and orderbook.midpoint is not None:
        return orderbook.midpoint
    if market_bucket.price > 0:
        return clamp_price(market_bucket.price)
    if orderbook is not None:
        if orderbook.best_bid is not None and orderbook.best_ask is not None:
            return (orderbook.best_bid + orderbook.best_ask) / 2.0
        return orderbook.best_bid or orderbook.best_ask
    return None


def market_best_bid(market_bucket: MarketBucket) -> float | None:
    if market_bucket.orderbook is not None:
        return market_bucket.orderbook.best_bid
    return clamp_price(market_bucket.price) if market_bucket.price > 0 else None


def market_best_ask(market_bucket: MarketBucket) -> float | None:
    if market_bucket.orderbook is not None:
        return market_bucket.orderbook.best_ask
    return clamp_price(market_bucket.price) if market_bucket.price > 0 else None


def market_entry_cost_per_share(
    market_bucket: MarketBucket,
    *,
    fee_rate: float = DEFAULT_TAKER_FEE_RATE,
) -> float | None:
    ask = market_best_ask(market_bucket)
    if ask is None:
        return None
    return ask + binary_contract_fee(shares=1.0, price=ask, fee_rate=fee_rate)


def value_position(
    position: Position,
    market_bucket: MarketBucket | None,
    *,
    fee_rate: float = DEFAULT_TAKER_FEE_RATE,
) -> PositionValuation:
    shares = max(0.0, position.shares)
    mark_price = market_mark_price(market_bucket) if market_bucket else None
    best_bid = market_best_bid(market_bucket) if market_bucket else None
    best_ask = market_best_ask(market_bucket) if market_bucket else None
    mark_value = shares * mark_price if mark_price is not None else 0.0

    liquidation_value = 0.0
    if shares > 0 and market_bucket is not None:
        if market_bucket.orderbook is not None:
            liquidation_value = market_bucket.orderbook.estimate_market_sell(
                shares,
                fee_rate=fee_rate,
            ).net_value
        elif best_bid is not None:
            gross = shares * best_bid
            fee = binary_contract_fee(shares=shares, price=best_bid, fee_rate=fee_rate)
            liquidation_value = max(0.0, gross - fee)

    cashout_ratio = liquidation_value / mark_value if mark_value > 0 else None
    return PositionValuation(
        position=position,
        mark_price=mark_price,
        best_bid=best_bid,
        best_ask=best_ask,
        mark_value=mark_value,
        liquidation_value=liquidation_value,
        cashout_ratio=cashout_ratio,
    )


def value_portfolio(
    portfolio: Portfolio,
    market_buckets: Sequence[MarketBucket],
    *,
    fee_rate: float = DEFAULT_TAKER_FEE_RATE,
) -> tuple[PositionValuation, ...]:
    return tuple(
        value_position(
            position,
            _find_market_bucket(position, market_buckets),
            fee_rate=fee_rate,
        )
        for position in portfolio.positions
    )


def portfolio_mark_value(valuations: Sequence[PositionValuation]) -> float:
    return sum(valuation.mark_value for valuation in valuations)


def portfolio_liquidation_value(valuations: Sequence[PositionValuation]) -> float:
    return sum(valuation.liquidation_value for valuation in valuations)


def portfolio_cashout_ratio(valuations: Sequence[PositionValuation]) -> float | None:
    mark_value = portfolio_mark_value(valuations)
    if mark_value <= 0:
        return None
    return portfolio_liquidation_value(valuations) / mark_value


def orderbook_overround(market_buckets: Sequence[MarketBucket]) -> dict[str, float | bool]:
    ask_sum = 0.0
    bid_sum = 0.0
    midpoint_sum = 0.0
    for market_bucket in market_buckets:
        ask = market_best_ask(market_bucket)
        bid = market_best_bid(market_bucket)
        midpoint = market_mark_price(market_bucket)
        ask_sum += ask if ask is not None else 0.0
        bid_sum += bid if bid is not None else 0.0
        midpoint_sum += midpoint if midpoint is not None else 0.0
    return {
        "ask_sum": ask_sum,
        "bid_sum": bid_sum,
        "midpoint_sum": midpoint_sum,
        "is_overround": ask_sum > 1.0,
    }


def _probability_key_map(
    market_buckets: Sequence[MarketBucket],
    probabilities: Mapping[str, float] | None,
) -> dict[str, float]:
    result: dict[str, float] = {}
    raw = probabilities or {}
    for market_bucket in market_buckets:
        candidates = (
            market_bucket_key(market_bucket),
            _outcome_key(market_bucket.outcome),
            market_bucket.outcome,
        )
        probability = None
        for candidate in candidates:
            if candidate in raw:
                probability = raw[candidate]
                break
        if probability is None:
            probability = _safe_optional_float(market_bucket.raw_payload.get("probability"))
        if probability is None:
            probability = market_mark_price(market_bucket)
        result[market_bucket_key(market_bucket)] = clamp_price(float(probability or 0.0))
    return result


def probabilities_from_market_buckets(
    market_buckets: Sequence[MarketBucket],
) -> dict[str, float]:
    return _probability_key_map(market_buckets, None)


def calculate_portfolio_scenarios(
    portfolio: Portfolio,
    market_buckets: Sequence[MarketBucket],
    *,
    probabilities: Mapping[str, float] | None = None,
    hedge_legs: Sequence[HedgeLeg] = (),
    covered_keys: set[str] | None = None,
) -> tuple[PortfolioScenario, ...]:
    probability_map = _probability_key_map(market_buckets, probabilities)
    position_shares: dict[str, float] = {}
    for position in portfolio.positions:
        key = position_key(position)
        position_shares[key] = position_shares.get(key, 0.0) + max(0.0, position.shares)
    hedge_shares: dict[str, float] = {}
    for leg in hedge_legs:
        key = bucket_key(leg.bucket)
        hedge_shares[key] = hedge_shares.get(key, 0.0) + max(0.0, leg.shares)

    total_cost = portfolio.total_cost + sum(max(0.0, leg.total_cost) for leg in hedge_legs)
    scenarios: list[PortfolioScenario] = []
    for market_bucket in market_buckets:
        key = market_bucket_key(market_bucket)
        payoff = position_shares.get(key, 0.0) + hedge_shares.get(key, 0.0)
        net_pnl = payoff - total_cost
        scenarios.append(
            PortfolioScenario(
                outcome=market_bucket.outcome,
                bucket=market_bucket.bucket,
                probability=probability_map.get(key, 0.0),
                payoff=payoff,
                total_cost=total_cost,
                net_pnl=net_pnl,
                is_covered=covered_keys is None or key in covered_keys,
            )
        )
    return tuple(scenarios)


def recommend_passive_entry(
    market_bucket: MarketBucket,
    *,
    model_probability: float,
    min_edge: float = 0.03,
    maker_fee_rate: float = DEFAULT_MAKER_FEE_RATE,
    taker_fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    is_overround: bool = False,
    tick_size: float = 0.01,
    expected_exit_cost: float | None = None,
) -> PassiveEntryRecommendation:
    probability = clamp_price(model_probability)
    midpoint = market_mark_price(market_bucket)
    best_bid = market_best_bid(market_bucket)
    best_ask = market_best_ask(market_bucket)
    reference = midpoint if midpoint is not None else clamp_price(market_bucket.price)

    exit_cost = (
        expected_exit_cost
        if expected_exit_cost is not None
        else estimate_expected_exit_cost(market_bucket, fee_rate=taker_fee_rate)
    )
    price_cap = probability - min_edge - exit_cost
    if best_bid is not None:
        passive_anchor = best_bid + max(0.0, tick_size)
    else:
        passive_anchor = max(0.0, reference - max(0.0, tick_size))
    if best_ask is not None:
        passive_anchor = min(passive_anchor, max(0.0, best_ask - max(0.0, tick_size)))
    limit_bid = clamp_price(min(passive_anchor, price_cap))
    if limit_bid <= 0:
        limit_bid = None

    passive_fee = (
        binary_contract_fee(shares=1.0, price=limit_bid, fee_rate=maker_fee_rate)
        if limit_bid is not None
        else 0.0
    )
    passive_entry_cost = limit_bid or reference
    passive_net_edge = probability - passive_entry_cost - passive_fee - exit_cost

    taker_price = best_ask if best_ask is not None else reference
    taker_fee = binary_contract_fee(shares=1.0, price=taker_price, fee_rate=taker_fee_rate)
    taker_net_edge = probability - taker_price - taker_fee - exit_cost

    if is_overround:
        return PassiveEntryRecommendation(
            action="SKIP_OVERROUND",
            outcome=market_bucket.outcome,
            model_probability=probability,
            executable_entry_cost=passive_entry_cost,
            fee=passive_fee,
            expected_exit_cost=exit_cost,
            net_edge=passive_net_edge,
            limit_bid=limit_bid,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            reason="全桶 ask 总和大于 1，低价桶不自动构成套利。",
        )
    if limit_bid is not None and passive_net_edge >= min_edge:
        return PassiveEntryRecommendation(
            action="ACCUMULATE_PASSIVE",
            outcome=market_bucket.outcome,
            model_probability=probability,
            executable_entry_cost=passive_entry_cost,
            fee=passive_fee,
            expected_exit_cost=exit_cost,
            net_edge=passive_net_edge,
            limit_bid=limit_bid,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            reason="模型概率扣除被动入场成本、fee 和预期退出成本后仍有 Edge。",
        )
    if taker_net_edge >= min_edge:
        return PassiveEntryRecommendation(
            action="TAKE_EDGE_SMALL",
            outcome=market_bucket.outcome,
            model_probability=probability,
            executable_entry_cost=taker_price,
            fee=taker_fee,
            expected_exit_cost=exit_cost,
            net_edge=taker_net_edge,
            limit_bid=limit_bid,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            reason="吃单后仍有 Edge，但应小仓位验证流动性。",
        )
    return PassiveEntryRecommendation(
        action="SKIP_NO_EDGE",
        outcome=market_bucket.outcome,
        model_probability=probability,
        executable_entry_cost=passive_entry_cost,
        fee=passive_fee,
        expected_exit_cost=exit_cost,
        net_edge=passive_net_edge,
        limit_bid=limit_bid,
        maker_fee_rate=maker_fee_rate,
        taker_fee_rate=taker_fee_rate,
        reason="模型概率不足以覆盖入场成本、fee 和预期退出成本。",
    )


def estimate_expected_exit_cost(
    market_bucket: MarketBucket,
    *,
    fee_rate: float = DEFAULT_TAKER_FEE_RATE,
) -> float:
    midpoint = market_mark_price(market_bucket)
    best_bid = market_best_bid(market_bucket)
    if midpoint is None or best_bid is None:
        return 0.0
    spread_cost = max(0.0, midpoint - best_bid)
    exit_fee = binary_contract_fee(shares=1.0, price=best_bid, fee_rate=fee_rate)
    return spread_cost + exit_fee


def generate_passive_exit_plan(
    position: Position,
    market_bucket: MarketBucket | None,
    *,
    fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    fractions: Sequence[float] = DEFAULT_EXIT_FRACTIONS,
    retain_fraction: float = DEFAULT_RETAIN_FRACTION,
    price_levels: Sequence[float] | None = None,
    min_cashout_ratio: float = 0.50,
) -> PassiveExitPlan:
    valuation = value_position(position, market_bucket, fee_rate=fee_rate)
    shares = max(0.0, position.shares)
    average_cost = position.average_cost
    if price_levels is None:
        price_levels = _default_exit_prices(average_cost, valuation.mark_price)
    normalized_fractions = tuple(max(0.0, fraction) for fraction in fractions)
    max_sell_fraction = max(0.0, min(1.0, 1.0 - max(0.0, retain_fraction)))
    total_fraction = sum(normalized_fractions)
    scale = min(1.0, max_sell_fraction / total_fraction) if total_fraction > 0 else 0.0

    ladder: list[ExitLadderLeg] = []
    sold_shares = 0.0
    for index, (fraction, raw_price) in enumerate(
        zip(normalized_fractions, price_levels, strict=False),
        start=1,
    ):
        scaled_fraction = fraction * scale
        leg_shares = shares * scaled_fraction
        price = clamp_price(float(raw_price))
        gross = leg_shares * price
        fee = binary_contract_fee(shares=leg_shares, price=price, fee_rate=fee_rate)
        sold_shares += leg_shares
        ladder.append(
            ExitLadderLeg(
                fraction=scaled_fraction,
                shares=leg_shares,
                limit_price=price,
                gross_value=gross,
                fee=fee,
                net_value=max(0.0, gross - fee),
                label=f"sell {scaled_fraction:.0%} at {price:.2f}",
            )
        )

    retained_shares = max(0.0, shares - sold_shares)
    action = _exit_action(
        position=position,
        valuation=valuation,
        min_cashout_ratio=min_cashout_ratio,
    )
    return PassiveExitPlan(
        action=action,
        outcome=position.outcome,
        total_shares=shares,
        retained_shares=retained_shares,
        average_cost=average_cost,
        mark_value=valuation.mark_value,
        liquidation_value=valuation.liquidation_value,
        cashout_ratio=valuation.cashout_ratio,
        ladder=tuple(ladder),
    )


def _default_exit_prices(average_cost: float, mark_price: float | None) -> tuple[float, float, float]:
    if average_cost <= 0.42:
        return (0.45, 0.55, 0.65)
    reference = max(average_cost, mark_price or 0.0)
    return (
        clamp_price(reference + 0.05),
        clamp_price(reference + 0.15),
        clamp_price(reference + 0.25),
    )


def _exit_action(
    *,
    position: Position,
    valuation: PositionValuation,
    min_cashout_ratio: float,
) -> str:
    if (
        valuation.cashout_ratio is not None
        and valuation.cashout_ratio >= min_cashout_ratio
        and valuation.best_bid is not None
        and valuation.best_bid >= position.average_cost
    ):
        return "DISTRIBUTE_PASSIVE"
    if valuation.mark_value > position.total_cost:
        return "PASSIVE_EXIT_ONLY"
    return "HOLD_TO_RESOLUTION_ONLY"


def calculate_hedge_lock(
    portfolio: Portfolio,
    market_buckets: Sequence[MarketBucket],
    *,
    probabilities: Mapping[str, float] | None = None,
    target_profit: float = 0.0,
    covered_outcomes: Sequence[str] | None = None,
    tail_probability_cutoff: float = 0.0,
    max_tail_probability: float = 0.05,
    fee_rate: float = DEFAULT_TAKER_FEE_RATE,
) -> PortfolioLockResult:
    metrics = orderbook_overround(market_buckets)
    probability_map = _probability_key_map(market_buckets, probabilities)
    covered_keys = _covered_bucket_keys(
        market_buckets,
        probability_map,
        covered_outcomes=covered_outcomes,
        tail_probability_cutoff=tail_probability_cutoff,
    )
    position_shares: dict[str, float] = {}
    for position in portfolio.positions:
        key = position_key(position)
        position_shares[key] = position_shares.get(key, 0.0) + max(0.0, position.shares)

    cost_by_key: dict[str, float] = {}
    bucket_by_key: dict[str, MarketBucket] = {}
    for market_bucket in market_buckets:
        key = market_bucket_key(market_bucket)
        bucket_by_key[key] = market_bucket
        entry_cost = market_entry_cost_per_share(market_bucket, fee_rate=fee_rate)
        if entry_cost is not None:
            cost_by_key[key] = entry_cost

    hedge_shares, feasible, solver_note = _solve_required_hedges(
        covered_keys=covered_keys,
        position_shares=position_shares,
        cost_by_key=cost_by_key,
        current_cost=portfolio.total_cost,
        target_profit=target_profit,
    )
    hedge_legs: list[HedgeLeg] = []
    for key, shares in hedge_shares.items():
        if shares <= 1e-9:
            continue
        market_bucket = bucket_by_key[key]
        ask = market_best_ask(market_bucket)
        if ask is None:
            continue
        cost = shares * ask
        fee = binary_contract_fee(shares=shares, price=ask, fee_rate=fee_rate)
        hedge_legs.append(
            HedgeLeg(
                outcome=market_bucket.outcome,
                bucket=market_bucket.bucket,
                shares=shares,
                price=ask,
                cost=cost,
                fee=fee,
                token_id=market_bucket.token_id,
                reason="补足核心覆盖区间的结算状态 payoff。",
            )
        )

    hedge_tuple = tuple(hedge_legs)
    scenarios = calculate_portfolio_scenarios(
        portfolio,
        market_buckets,
        probabilities=probabilities,
        hedge_legs=hedge_tuple,
        covered_keys=covered_keys,
    )
    covered_scenarios = tuple(scenario for scenario in scenarios if scenario.is_covered)
    worst_case_pnl = min((scenario.net_pnl for scenario in scenarios), default=-portfolio.total_cost)
    covered_worst_case_pnl = min(
        (scenario.net_pnl for scenario in covered_scenarios),
        default=worst_case_pnl,
    )
    covered_probability = sum(
        probability_map.get(market_bucket_key(market_bucket), 0.0)
        for market_bucket in market_buckets
        if market_bucket_key(market_bucket) in covered_keys
    )
    uncovered_tail_probability = max(0.0, 1.0 - covered_probability)
    hedge_cost = sum(leg.total_cost for leg in hedge_tuple)
    all_keys = {market_bucket_key(market_bucket) for market_bucket in market_buckets}
    covers_all = covered_keys == all_keys
    is_overround = bool(metrics["is_overround"])
    tolerance = 1e-8
    is_true_arbitrage = (
        feasible
        and covers_all
        and not is_overround
        and float(metrics["ask_sum"]) < 1.0
        and worst_case_pnl >= target_profit - tolerance
    )
    is_tail_risk_lock = (
        feasible
        and not covers_all
        and covered_worst_case_pnl >= target_profit - tolerance
        and uncovered_tail_probability > 0.0
    )
    recommendation = _hedge_recommendation(
        is_true_arbitrage=is_true_arbitrage,
        is_tail_risk_lock=is_tail_risk_lock,
        is_overround=is_overround,
        covered_worst_case_pnl=covered_worst_case_pnl,
        target_profit=target_profit,
        uncovered_tail_probability=uncovered_tail_probability,
        max_tail_probability=max_tail_probability,
    )
    notes: list[str] = []
    if solver_note:
        notes.append(solver_note)
    if is_overround:
        notes.append("全桶 ask 总和大于 1，不能标记为无风险套利。")
    if is_tail_risk_lock:
        notes.append("只覆盖核心桶位，未覆盖尾部仍可能亏损。")
    return PortfolioLockResult(
        scenarios=scenarios,
        hedge_legs=hedge_tuple,
        current_cost=portfolio.total_cost,
        hedge_cost=hedge_cost,
        total_cost=portfolio.total_cost + hedge_cost,
        lock_profit=covered_worst_case_pnl,
        worst_case_pnl=worst_case_pnl,
        covered_worst_case_pnl=covered_worst_case_pnl,
        covered_probability=covered_probability,
        uncovered_tail_probability=uncovered_tail_probability,
        ask_sum=float(metrics["ask_sum"]),
        bid_sum=float(metrics["bid_sum"]),
        midpoint_sum=float(metrics["midpoint_sum"]),
        is_overround=is_overround,
        is_true_arbitrage=is_true_arbitrage,
        is_tail_risk_lock=is_tail_risk_lock,
        recommendation=recommendation,
        target_profit=target_profit,
        notes=tuple(notes),
    )


def _covered_bucket_keys(
    market_buckets: Sequence[MarketBucket],
    probability_map: Mapping[str, float],
    *,
    covered_outcomes: Sequence[str] | None,
    tail_probability_cutoff: float,
) -> set[str]:
    if covered_outcomes:
        requested = {_outcome_key(outcome) for outcome in covered_outcomes}
        return {
            market_bucket_key(market_bucket)
            for market_bucket in market_buckets
            if _outcome_key(market_bucket.outcome) in requested
            or market_bucket_key(market_bucket) in requested
        }
    cutoff = max(0.0, tail_probability_cutoff)
    if cutoff > 0:
        return {
            market_bucket_key(market_bucket)
            for market_bucket in market_buckets
            if probability_map.get(market_bucket_key(market_bucket), 0.0) >= cutoff
        }
    return {market_bucket_key(market_bucket) for market_bucket in market_buckets}


def _solve_required_hedges(
    *,
    covered_keys: set[str],
    position_shares: Mapping[str, float],
    cost_by_key: Mapping[str, float],
    current_cost: float,
    target_profit: float,
) -> tuple[dict[str, float], bool, str]:
    missing = sorted(key for key in covered_keys if key not in cost_by_key)
    if missing:
        return {}, False, "部分核心桶缺少 ask，无法计算完整 hedge cost。"

    h_cost = 0.0
    hedge_shares: dict[str, float] = {}
    for _ in range(200):
        next_shares = {
            key: max(
                0.0,
                target_profit + current_cost + h_cost - position_shares.get(key, 0.0),
            )
            for key in covered_keys
        }
        next_cost = sum(next_shares[key] * cost_by_key[key] for key in covered_keys)
        if abs(next_cost - h_cost) <= 1e-9:
            return next_shares, True, ""
        if next_cost > 1_000_000:
            return next_shares, False, "hedge 方程未收敛，通常意味着覆盖桶 ask 成本过高。"
        hedge_shares = next_shares
        h_cost = next_cost
    return hedge_shares, False, "hedge 方程达到迭代上限。"


def _hedge_recommendation(
    *,
    is_true_arbitrage: bool,
    is_tail_risk_lock: bool,
    is_overround: bool,
    covered_worst_case_pnl: float,
    target_profit: float,
    uncovered_tail_probability: float,
    max_tail_probability: float,
) -> str:
    if is_true_arbitrage:
        return "HEDGE_LOCK"
    if is_tail_risk_lock:
        if uncovered_tail_probability <= max_tail_probability:
            return "HEDGE_LOCK"
        return "TAIL_RISK_TOO_HIGH"
    if is_overround:
        return "SKIP_OVERROUND"
    if covered_worst_case_pnl >= target_profit:
        return "HEDGE_LOCK"
    return "TAIL_RISK_TOO_HIGH"


def load_positions_file(
    path: Path,
    *,
    default_unit: TemperatureUnit = "F",
) -> tuple[Position, ...]:
    rows = _load_rows(path, key="positions")
    return tuple(position_from_mapping(row, default_unit=default_unit) for row in rows)


def load_market_buckets_file(
    path: Path,
    *,
    default_unit: TemperatureUnit = "F",
) -> tuple[MarketBucket, ...]:
    rows = _load_rows(path, key="markets")
    return tuple(market_bucket_from_mapping(row, default_unit=default_unit) for row in rows)


def _load_rows(path: Path, *, key: str) -> list[Mapping[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            payload = payload.get(key) or payload.get("rows") or []
        if not isinstance(payload, list):
            raise ValueError(f"{path} JSON root must be a list or contain '{key}'.")
        return [item for item in payload if isinstance(item, Mapping)]
    with path.open("r", encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def position_from_mapping(
    row: Mapping[str, Any],
    *,
    default_unit: TemperatureUnit = "F",
) -> Position:
    outcome = str(row.get("outcome") or row.get("bucket") or row.get("label") or "")
    if not outcome:
        raise ValueError("Position row requires outcome.")
    bucket = parse_temperature_bucket(outcome, default_unit=default_unit)
    shares = _safe_float(row.get("shares") or row.get("size"))
    average_price = _safe_optional_float(
        row.get("average_entry_price")
        or row.get("average_cost")
        or row.get("avg_cost")
        or row.get("price")
    )
    total_cost = _safe_optional_float(row.get("total_cost") or row.get("cost"))
    if total_cost is None:
        total_cost = shares * (average_price or 0.0)
    return Position(
        outcome=outcome,
        bucket=bucket,
        shares=shares,
        total_cost=total_cost,
        token_id=str(row["token_id"]) if row.get("token_id") else None,
        market_id=str(row["market_id"]) if row.get("market_id") else None,
        slug=str(row["slug"]) if row.get("slug") else None,
        average_entry_price=average_price,
        settlement_station=str(row["settlement_station"])
        if row.get("settlement_station")
        else None,
        station_id=str(row["station_id"]) if row.get("station_id") else None,
        metar_source=str(row["metar_source"]) if row.get("metar_source") else None,
        raw_payload=dict(row),
    )


def market_bucket_from_mapping(
    row: Mapping[str, Any],
    *,
    default_unit: TemperatureUnit = "F",
) -> MarketBucket:
    outcome = str(row.get("outcome") or row.get("bucket") or row.get("label") or "")
    if not outcome:
        raise ValueError("Market row requires outcome.")
    bucket = parse_temperature_bucket(outcome, default_unit=default_unit)
    orderbook = _orderbook_from_best_levels(row)
    price = _safe_float(row.get("price") or row.get("market_price") or row.get("midpoint"))
    if price <= 0 and orderbook is not None and orderbook.midpoint is not None:
        price = orderbook.midpoint
    return MarketBucket(
        market_id=str(row.get("market_id") or row.get("slug") or "portfolio-file"),
        question=str(row.get("question") or "Portfolio file market"),
        slug=str(row["slug"]) if row.get("slug") else None,
        condition_id=str(row["condition_id"]) if row.get("condition_id") else None,
        outcome=outcome,
        price=price,
        bucket=bucket,
        token_id=str(row["token_id"]) if row.get("token_id") else None,
        orderbook=orderbook,
        raw_payload=dict(row),
    )


def market_buckets_from_positions(
    positions: Sequence[Position],
    *,
    default_unit: TemperatureUnit = "F",
) -> tuple[MarketBucket, ...]:
    market_buckets: list[MarketBucket] = []
    for position in positions:
        payload = dict(position.raw_payload)
        payload.setdefault("outcome", position.outcome)
        if "price" not in payload and "market_price" not in payload:
            payload["price"] = position.average_cost
        market_buckets.append(market_bucket_from_mapping(payload, default_unit=default_unit))
    return tuple(market_buckets)


def _orderbook_from_best_levels(row: Mapping[str, Any]) -> OrderBookSnapshot | None:
    best_bid = _safe_optional_float(row.get("best_bid") or row.get("bid"))
    best_ask = _safe_optional_float(row.get("best_ask") or row.get("ask"))
    if best_bid is None and best_ask is None:
        return None
    bid_size = _safe_float(row.get("bid_size"), default=10_000.0)
    ask_size = _safe_float(row.get("ask_size"), default=10_000.0)
    token_id = str(row.get("token_id") or row.get("outcome") or "manual")
    return OrderBookSnapshot(
        token_id=token_id,
        bids=(
            (OrderBookLevel(price=best_bid, size=bid_size),)
            if best_bid is not None
            else ()
        ),
        asks=(
            (OrderBookLevel(price=best_ask, size=ask_size),)
            if best_ask is not None
            else ()
        ),
    )


def parse_inline_positions(
    text: str,
    *,
    default_unit: TemperatureUnit = "F",
) -> tuple[Position, ...]:
    rows = _inline_csv_rows(text)
    return tuple(position_from_mapping(row, default_unit=default_unit) for row in rows)


def parse_inline_market_buckets(
    text: str,
    *,
    default_unit: TemperatureUnit = "F",
) -> tuple[MarketBucket, ...]:
    rows = _inline_csv_rows(text)
    return tuple(market_bucket_from_mapping(row, default_unit=default_unit) for row in rows)


def _inline_csv_rows(text: str) -> list[Mapping[str, Any]]:
    cleaned = "\n".join(line for line in text.splitlines() if line.strip())
    if not cleaned.strip():
        return []
    return list(csv.DictReader(cleaned.splitlines()))
