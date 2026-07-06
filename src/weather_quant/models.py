"""Shared domain models for Polymarket weather markets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal, Mapping


TemperatureKind = Literal["high", "low"]
TemperatureUnit = Literal["C", "F"]
OrderSide = Literal["buy", "sell"]
DEFAULT_TAKER_FEE_RATE = 0.05
DEFAULT_MAKER_FEE_RATE = 0.0


def binary_contract_fee(*, shares: float, price: float, fee_rate: float) -> float:
    """Polymarket binary fee model for one matched price level."""

    normalized_shares = max(0.0, shares)
    normalized_price = max(0.0, min(1.0, price))
    return normalized_shares * max(0.0, fee_rate) * normalized_price * (1.0 - normalized_price)


@dataclass(frozen=True)
class TemperatureBucket:
    """Continuous temperature interval used by market outcomes."""

    label: str
    lower: float | None
    upper: float | None
    unit: TemperatureUnit = "F"
    lower_inclusive: bool = True
    upper_inclusive: bool = False

    def contains(self, value: float) -> bool:
        if self.lower is not None:
            if self.lower_inclusive and value < self.lower:
                return False
            if not self.lower_inclusive and value <= self.lower:
                return False
        if self.upper is not None:
            if self.upper_inclusive and value > self.upper:
                return False
            if not self.upper_inclusive and value >= self.upper:
                return False
        return True

    @property
    def canonical_key(self) -> str:
        lower = "-inf" if self.lower is None else f"{self.lower:.4f}"
        upper = "inf" if self.upper is None else f"{self.upper:.4f}"
        return f"{self.unit}:{lower}:{upper}"


@dataclass(frozen=True)
class CityConfig:
    """Settlement-location configuration for a weather market city."""

    city_id: str
    name: str
    latitude: float
    longitude: float
    timezone: str = "auto"
    settlement_station: str | None = None
    station_id: str | None = None
    metar_source: str | None = None
    forecast_granularity: Literal["city", "station"] = "city"
    settlement_unit: TemperatureUnit = "F"
    weather_models: tuple[str, ...] = ("ecmwf_ifs025", "gfs_seamless", "ukmo_seamless")
    model_weights: Mapping[str, float] = field(default_factory=dict)
    model_error_std: float = 2.5
    min_distribution_std: float = 1.0
    elevation: float | None = None
    cell_selection: Literal["land", "sea", "nearest"] | None = None


@dataclass(frozen=True)
class ForecastPoint:
    """One forecast value from one weather model or provider."""

    city_id: str
    target_date: date
    kind: TemperatureKind
    value: float
    unit: TemperatureUnit
    source_model: str
    settlement_station: str | None = None
    station_id: str | None = None
    metar_source: str | None = None
    forecast_granularity: Literal["city", "station"] = "city"
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EnsembleForecast:
    """All forecast points used for one city/date/kind prediction."""

    city: CityConfig
    target_date: date
    kind: TemperatureKind
    points: tuple[ForecastPoint, ...]
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def source_models(self) -> tuple[str, ...]:
        return tuple(point.source_model for point in self.points)


@dataclass(frozen=True)
class EnsembleMemberForecast:
    """Hourly values for one perturbed ensemble member."""

    provider: str
    model: str
    member_id: str
    hourly_times: tuple[str, ...]
    hourly_values: tuple[float | None, ...]
    unit: TemperatureUnit
    timezone: str
    raw_hourly: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EnsembleMemberDailyValue:
    """One member reduced to the settlement day's high or low."""

    member_id: str
    target_date: date
    kind: TemperatureKind
    value: float
    unit: TemperatureUnit
    hourly_times: tuple[str, ...] = ()
    hourly_values: tuple[float | None, ...] = ()
    bucket_label: str | None = None
    bucket_key: str | None = None


@dataclass(frozen=True)
class EnsembleRun:
    """One initialized ensemble model run for one city/date/kind."""

    run_key: str
    provider: str
    model: str
    city: CityConfig
    target_date: date
    kind: TemperatureKind
    members: tuple[EnsembleMemberForecast, ...]
    daily_values: tuple[EnsembleMemberDailyValue, ...]
    run_time: datetime | None = None
    initialization_time: datetime | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_payload: Mapping[str, Any] = field(default_factory=dict)
    payload_hash: str | None = None

    @property
    def member_count(self) -> int:
        return len(self.daily_values)


@dataclass(frozen=True)
class EnsembleBucketProbability:
    """Empirical probability for one bucket based on member hits."""

    bucket: TemperatureBucket
    hit_count: int
    probability: float
    total_members: int
    unmatched_count: int
    empirical_mean: float | None = None
    empirical_std: float | None = None
    p10: float | None = None
    p50: float | None = None
    p90: float | None = None


@dataclass(frozen=True)
class EnsembleDistribution:
    """Empirical member-hit distribution across market buckets."""

    run: EnsembleRun
    probabilities: tuple[EnsembleBucketProbability, ...]
    member_values: tuple[EnsembleMemberDailyValue, ...]
    unit: TemperatureUnit
    total_members: int
    unmatched_count: int
    empirical_mean: float | None = None
    empirical_std: float | None = None
    p10: float | None = None
    p50: float | None = None
    p90: float | None = None


@dataclass(frozen=True)
class MarketBucket:
    """A tradable Polymarket outcome mapped to a temperature bucket."""

    market_id: str
    question: str
    slug: str | None
    condition_id: str | None
    outcome: str
    price: float
    bucket: TemperatureBucket
    token_id: str | None = None
    orderbook: "OrderBookSnapshot | None" = None
    raw_payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderBookLevel:
    """One price level in YES-token shares."""

    price: float
    size: float


@dataclass(frozen=True)
class FillEstimate:
    """Estimated market-order fill through the visible order book."""

    side: OrderSide
    requested_usdc: float
    requested_shares: float
    filled_shares: float
    notional: float
    fee: float
    net_value: float
    vwap: float | None
    effective_price: float | None
    slippage: float | None
    is_complete: bool
    remaining_usdc: float = 0.0
    remaining_shares: float = 0.0
    best_price: float | None = None
    worst_price: float | None = None
    fee_rate: float = DEFAULT_TAKER_FEE_RATE

    @property
    def liquidation_value(self) -> float:
        return self.net_value if self.side == "sell" else 0.0


@dataclass(frozen=True)
class LiquidityMetrics:
    """Liquidity snapshot derived from executable order-book depth."""

    token_id: str | None
    best_bid: float | None
    best_ask: float | None
    midpoint: float | None
    spread: float | None
    buy_estimate: FillEstimate | None = None
    sell_estimate: FillEstimate | None = None
    cashout_ratio: float | None = None
    depth_to_target_price: float = 0.0
    max_position_shares: float = 0.0
    depth_based_stake_cap: float = 0.0
    max_sell_value: float = 0.0


@dataclass(frozen=True)
class OrderBookSnapshot:
    """Visible CLOB depth for one YES token."""

    token_id: str
    bids: tuple[OrderBookLevel, ...] = ()
    asks: tuple[OrderBookLevel, ...] = ()
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        bids = tuple(
            sorted(
                (level for level in self.bids if level.size > 0 and 0.0 <= level.price <= 1.0),
                key=lambda level: level.price,
                reverse=True,
            )
        )
        asks = tuple(
            sorted(
                (level for level in self.asks if level.size > 0 and 0.0 <= level.price <= 1.0),
                key=lambda level: level.price,
            )
        )
        object.__setattr__(self, "bids", bids)
        object.__setattr__(self, "asks", asks)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return max(0.0, self.best_ask - self.best_bid)

    def estimate_market_buy(
        self,
        stake_usdc: float,
        *,
        fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    ) -> FillEstimate:
        budget = max(0.0, stake_usdc)
        remaining = budget
        filled_shares = 0.0
        notional = 0.0
        fee = 0.0
        worst_price: float | None = None
        for level in self.asks:
            level_fee_per_share = binary_contract_fee(
                shares=1.0,
                price=level.price,
                fee_rate=fee_rate,
            )
            cash_per_share = level.price + level_fee_per_share
            if cash_per_share <= 0:
                continue
            level_shares = min(level.size, remaining / cash_per_share)
            if level_shares <= 0:
                break
            level_notional = level_shares * level.price
            level_fee = binary_contract_fee(
                shares=level_shares,
                price=level.price,
                fee_rate=fee_rate,
            )
            filled_shares += level_shares
            notional += level_notional
            fee += level_fee
            remaining -= level_notional + level_fee
            worst_price = level.price
            if level_shares < level.size or remaining <= 1e-12:
                break

        net_value = notional + fee
        vwap = notional / filled_shares if filled_shares else None
        effective_price = net_value / filled_shares if filled_shares else None
        best_price = self.best_ask
        slippage = (
            (vwap - best_price) / best_price
            if vwap is not None and best_price and best_price > 0
            else None
        )
        return FillEstimate(
            side="buy",
            requested_usdc=budget,
            requested_shares=0.0,
            filled_shares=filled_shares,
            notional=notional,
            fee=fee,
            net_value=net_value,
            vwap=vwap,
            effective_price=effective_price,
            slippage=slippage,
            is_complete=remaining <= 1e-9,
            remaining_usdc=max(0.0, remaining),
            best_price=best_price,
            worst_price=worst_price,
            fee_rate=fee_rate,
        )

    def estimate_market_sell(
        self,
        shares: float,
        *,
        fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    ) -> FillEstimate:
        requested_shares = max(0.0, shares)
        remaining = requested_shares
        filled_shares = 0.0
        notional = 0.0
        fee = 0.0
        worst_price: float | None = None
        for level in self.bids:
            level_shares = min(level.size, remaining)
            if level_shares <= 0:
                break
            level_notional = level_shares * level.price
            level_fee = binary_contract_fee(
                shares=level_shares,
                price=level.price,
                fee_rate=fee_rate,
            )
            filled_shares += level_shares
            notional += level_notional
            fee += level_fee
            remaining -= level_shares
            worst_price = level.price
            if remaining <= 1e-12:
                break

        net_value = max(0.0, notional - fee)
        vwap = notional / filled_shares if filled_shares else None
        effective_price = net_value / filled_shares if filled_shares else None
        best_price = self.best_bid
        slippage = (
            (best_price - vwap) / best_price
            if vwap is not None and best_price and best_price > 0
            else None
        )
        return FillEstimate(
            side="sell",
            requested_usdc=0.0,
            requested_shares=requested_shares,
            filled_shares=filled_shares,
            notional=notional,
            fee=fee,
            net_value=net_value,
            vwap=vwap,
            effective_price=effective_price,
            slippage=slippage,
            is_complete=remaining <= 1e-9,
            remaining_shares=max(0.0, remaining),
            best_price=best_price,
            worst_price=worst_price,
            fee_rate=fee_rate,
        )

    def cashout_ratio(
        self,
        *,
        shares: float,
        mark_price: float | None = None,
        fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    ) -> float | None:
        normalized_shares = max(0.0, shares)
        if normalized_shares <= 0:
            return None
        mark = self.midpoint if mark_price is None else mark_price
        if mark is None or mark <= 0:
            return None
        liquidation_value = self.estimate_market_sell(
            normalized_shares,
            fee_rate=fee_rate,
        ).net_value
        mark_value = normalized_shares * mark
        return liquidation_value / mark_value if mark_value > 0 else None

    def depth_to_target_price(
        self,
        *,
        side: OrderSide,
        target_price: float,
    ) -> float:
        normalized_target = max(0.0, min(1.0, target_price))
        if side == "buy":
            return sum(level.size for level in self.asks if level.price <= normalized_target)
        return sum(level.size for level in self.bids if level.price >= normalized_target)

    def max_buy_usdc_by_depth(
        self,
        *,
        max_entry_slippage: float,
        depth_usage_fraction: float,
        fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    ) -> float:
        if self.best_ask is None:
            return 0.0
        target_price = min(1.0, self.best_ask * (1.0 + max(0.0, max_entry_slippage)))
        usage = max(0.0, min(1.0, depth_usage_fraction))
        total = 0.0
        for level in self.asks:
            if level.price > target_price:
                continue
            shares = level.size * usage
            total += shares * level.price
            total += binary_contract_fee(shares=shares, price=level.price, fee_rate=fee_rate)
        return total

    def max_position_by_depth(
        self,
        *,
        mark_price: float | None = None,
        max_exit_slippage: float,
        depth_usage_fraction: float,
    ) -> float:
        reference = mark_price or self.midpoint or self.best_bid
        if reference is None or reference <= 0:
            return 0.0
        target_price = max(0.0, reference * (1.0 - max(0.0, max_exit_slippage)))
        usage = max(0.0, min(1.0, depth_usage_fraction))
        return self.depth_to_target_price(side="sell", target_price=target_price) * usage


@dataclass(frozen=True)
class BucketProbability:
    bucket: TemperatureBucket
    probability: float


@dataclass(frozen=True)
class ForecastDistribution:
    """Normal approximation derived from the forecast ensemble."""

    mean: float
    std: float
    unit: TemperatureUnit
    probabilities: tuple[BucketProbability, ...]


@dataclass(frozen=True)
class BucketSignal:
    """Prediction-vs-market signal for one outcome bucket."""

    market_bucket: MarketBucket
    probability: float
    market_price: float
    edge: float
    expected_value: float
    fair_price: float
    recommendation: str
    raw_edge: float | None = None
    executable_edge: float | None = None
    hold_to_resolution_ev: float | None = None
    exit_now_ev: float | None = None
    expected_exit_ev: float | None = None
    limit_bid: float | None = None
    executable_entry_cost: float | None = None
    expected_exit_cost: float | None = None
    entry_fee_cost: float | None = None
    liquidity: LiquidityMetrics | None = None


@dataclass(frozen=True)
class PredictionReport:
    """Full prediction report for one weather market."""

    city: CityConfig
    target_date: date
    kind: TemperatureKind
    market_question: str
    distribution: ForecastDistribution
    ensemble: EnsembleForecast
    signals: tuple[BucketSignal, ...]
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def top_edges(self, limit: int = 5) -> tuple[BucketSignal, ...]:
        return tuple(
            sorted(self.signals, key=lambda signal: signal.edge, reverse=True)[:limit]
        )


@dataclass(frozen=True)
class Position:
    """Current YES-token holding for one settlement bucket."""

    outcome: str
    bucket: TemperatureBucket
    shares: float
    total_cost: float
    token_id: str | None = None
    market_id: str | None = None
    slug: str | None = None
    average_entry_price: float | None = None
    settlement_station: str | None = None
    station_id: str | None = None
    metar_source: str | None = None
    raw_payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def average_cost(self) -> float:
        if self.average_entry_price is not None:
            return self.average_entry_price
        normalized_shares = max(0.0, self.shares)
        return self.total_cost / normalized_shares if normalized_shares > 0 else 0.0


@dataclass(frozen=True)
class PositionValuation:
    """Mark-to-midpoint and executable liquidation value for one position."""

    position: Position
    mark_price: float | None
    best_bid: float | None
    best_ask: float | None
    mark_value: float
    liquidation_value: float
    cashout_ratio: float | None

    @property
    def unrealized_mark_pnl(self) -> float:
        return self.mark_value - self.position.total_cost

    @property
    def executable_pnl(self) -> float:
        return self.liquidation_value - self.position.total_cost


@dataclass(frozen=True)
class Portfolio:
    """Collection of YES-token positions across temperature buckets."""

    positions: tuple[Position, ...] = ()
    market_question: str | None = None
    target_date: date | None = None
    kind: TemperatureKind | None = None
    settlement_station: str | None = None
    station_id: str | None = None
    metar_source: str | None = None

    @property
    def total_cost(self) -> float:
        return sum(max(0.0, position.total_cost) for position in self.positions)

    @property
    def total_shares(self) -> float:
        return sum(max(0.0, position.shares) for position in self.positions)


@dataclass(frozen=True)
class HedgeLeg:
    """Recommended YES-token hedge leg for another settlement bucket."""

    outcome: str
    bucket: TemperatureBucket
    shares: float
    price: float
    cost: float
    fee: float = 0.0
    token_id: str | None = None
    action: str = "BUY_YES"
    reason: str = ""

    @property
    def total_cost(self) -> float:
        return self.cost + self.fee


@dataclass(frozen=True)
class PortfolioScenario:
    """Portfolio payoff under one possible settlement bucket."""

    outcome: str
    bucket: TemperatureBucket
    probability: float
    payoff: float
    total_cost: float
    net_pnl: float
    is_covered: bool


@dataclass(frozen=True)
class PortfolioLockResult:
    """Hedge-lock evaluation, including tail-risk and overround diagnostics."""

    scenarios: tuple[PortfolioScenario, ...]
    hedge_legs: tuple[HedgeLeg, ...]
    current_cost: float
    hedge_cost: float
    total_cost: float
    lock_profit: float
    worst_case_pnl: float
    covered_worst_case_pnl: float
    covered_probability: float
    uncovered_tail_probability: float
    ask_sum: float
    bid_sum: float
    midpoint_sum: float
    is_overround: bool
    is_true_arbitrage: bool
    is_tail_risk_lock: bool
    recommendation: str
    target_profit: float = 0.0
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PassiveEntryRecommendation:
    """Passive or small-taker entry recommendation for one bucket."""

    action: str
    outcome: str
    model_probability: float
    executable_entry_cost: float
    fee: float
    expected_exit_cost: float
    net_edge: float
    limit_bid: float | None
    maker_fee_rate: float = DEFAULT_MAKER_FEE_RATE
    taker_fee_rate: float = DEFAULT_TAKER_FEE_RATE
    reason: str = ""


@dataclass(frozen=True)
class ExitLadderLeg:
    """One passive sell order in an exit ladder."""

    fraction: float
    shares: float
    limit_price: float
    gross_value: float
    fee: float
    net_value: float
    label: str = ""


@dataclass(frozen=True)
class PassiveExitPlan:
    """Passive distribution plan for an existing position."""

    action: str
    outcome: str
    total_shares: float
    retained_shares: float
    average_cost: float
    mark_value: float
    liquidation_value: float
    cashout_ratio: float | None
    ladder: tuple[ExitLadderLeg, ...]
    warning: str = "页面浮盈不是已实现收益，只有成交后的卖出净额才是可验证收益。"
