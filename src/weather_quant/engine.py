"""Prediction engine that turns forecasts and market buckets into edge."""

from __future__ import annotations

from datetime import date
from typing import Sequence

from weather_quant.market import GammaMarketClient
from weather_quant.models import (
    BucketSignal,
    CityConfig,
    DEFAULT_MAKER_FEE_RATE,
    DEFAULT_TAKER_FEE_RATE,
    LiquidityMetrics,
    MarketBucket,
    PredictionReport,
    TemperatureBucket,
    TemperatureKind,
)
from weather_quant.portfolio import recommend_passive_entry
from weather_quant.probability import TemperatureProbabilityModel
from weather_quant.weather import WeatherEnsembleProvider


class PredictionEngineError(RuntimeError):
    """Raised when a prediction report cannot be built."""


class PredictionEngine:
    """High-level orchestrator for forecast, market, probability, and edge."""

    def __init__(
        self,
        *,
        weather_provider: WeatherEnsembleProvider | None = None,
        market_client: GammaMarketClient | None = None,
        probability_model: TemperatureProbabilityModel | None = None,
        buy_edge_threshold: float = 0.03,
        fee_rate: float = DEFAULT_TAKER_FEE_RATE,
        maker_fee_rate: float = DEFAULT_MAKER_FEE_RATE,
        min_cashout_ratio: float = 0.50,
        max_entry_slippage: float = 0.10,
        max_exit_slippage: float = 0.20,
        depth_usage_fraction: float = 0.25,
    ) -> None:
        self.weather_provider = weather_provider or WeatherEnsembleProvider()
        self.market_client = market_client or GammaMarketClient()
        self.probability_model = probability_model or TemperatureProbabilityModel()
        self.buy_edge_threshold = buy_edge_threshold
        self.fee_rate = fee_rate
        self.maker_fee_rate = maker_fee_rate
        self.min_cashout_ratio = min_cashout_ratio
        self.max_entry_slippage = max_entry_slippage
        self.max_exit_slippage = max_exit_slippage
        self.depth_usage_fraction = depth_usage_fraction

    def build_report(
        self,
        *,
        city: CityConfig,
        target_date: date,
        kind: TemperatureKind,
        market_query: str | None = None,
        market_slug: str | None = None,
        condition_id: str | None = None,
        fallback_buckets: Sequence[TemperatureBucket] = (),
        models: tuple[str, ...] | None = None,
        refresh_clob_midpoints: bool = False,
        use_orderbook: bool = False,
        fee_rate: float | None = None,
        maker_fee_rate: float | None = None,
        min_cashout_ratio: float | None = None,
        max_entry_slippage: float | None = None,
        max_exit_slippage: float | None = None,
        depth_usage_fraction: float | None = None,
    ) -> PredictionReport:
        market_buckets = self._load_market_buckets(
            city=city,
            market_query=market_query,
            market_slug=market_slug,
            condition_id=condition_id,
            fallback_buckets=fallback_buckets,
            refresh_clob_midpoints=refresh_clob_midpoints,
            include_orderbooks=use_orderbook,
        )
        if not market_buckets:
            raise PredictionEngineError("No market buckets were found or parsed.")

        ensemble = self.weather_provider.fetch_ensemble(
            city,
            target_date=target_date,
            kind=kind,
            models=models,
        )
        buckets = tuple(market_bucket.bucket for market_bucket in market_buckets)
        distribution = self.probability_model.build_distribution(
            ensemble,
            buckets,
            unit=buckets[0].unit,
        )
        signals: list[BucketSignal] = []
        effective_fee_rate = self.fee_rate if fee_rate is None else fee_rate
        effective_maker_fee_rate = (
            self.maker_fee_rate if maker_fee_rate is None else maker_fee_rate
        )
        effective_min_cashout_ratio = (
            self.min_cashout_ratio
            if min_cashout_ratio is None
            else min_cashout_ratio
        )
        effective_max_entry_slippage = (
            self.max_entry_slippage
            if max_entry_slippage is None
            else max_entry_slippage
        )
        effective_max_exit_slippage = (
            self.max_exit_slippage
            if max_exit_slippage is None
            else max_exit_slippage
        )
        effective_depth_usage_fraction = (
            self.depth_usage_fraction
            if depth_usage_fraction is None
            else depth_usage_fraction
        )
        for market_bucket, probability in zip(
            market_buckets,
            distribution.probabilities,
            strict=True,
        ):
            signals.append(
                build_bucket_signal(
                    market_bucket=market_bucket,
                    probability=probability.probability,
                    buy_edge_threshold=self.buy_edge_threshold,
                    use_orderbook=use_orderbook,
                    fee_rate=effective_fee_rate,
                    maker_fee_rate=effective_maker_fee_rate,
                    min_cashout_ratio=effective_min_cashout_ratio,
                    max_entry_slippage=effective_max_entry_slippage,
                    max_exit_slippage=effective_max_exit_slippage,
                    depth_usage_fraction=effective_depth_usage_fraction,
                )
            )

        question = market_buckets[0].question or (
            f"{city.name} {target_date.isoformat()} {kind} temperature"
        )
        return PredictionReport(
            city=city,
            target_date=target_date,
            kind=kind,
            market_question=question,
            distribution=distribution,
            ensemble=ensemble,
            signals=tuple(sorted(signals, key=lambda signal: signal.edge, reverse=True)),
        )

    def _load_market_buckets(
        self,
        *,
        city: CityConfig,
        market_query: str | None,
        market_slug: str | None,
        condition_id: str | None,
        fallback_buckets: Sequence[TemperatureBucket],
        refresh_clob_midpoints: bool,
        include_orderbooks: bool,
    ) -> tuple[MarketBucket, ...]:
        if market_query or market_slug or condition_id:
            return self.market_client.get_market_buckets(
                query=market_query,
                slug=market_slug,
                condition_id=condition_id,
                default_unit=city.settlement_unit,
                refresh_clob_midpoints=refresh_clob_midpoints,
                include_orderbooks=include_orderbooks,
            )
        if fallback_buckets:
            return tuple(
                MarketBucket(
                    market_id="manual",
                    question="Manual fallback buckets",
                    slug=None,
                    condition_id=None,
                    outcome=bucket.label,
                    price=0.0,
                    bucket=bucket,
                )
                for bucket in fallback_buckets
            )
        raise PredictionEngineError("A market query/slug/condition_id or fallback buckets is required.")


def build_bucket_signal(
    *,
    market_bucket: MarketBucket,
    probability: float,
    buy_edge_threshold: float,
    use_orderbook: bool = False,
    fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    maker_fee_rate: float = DEFAULT_MAKER_FEE_RATE,
    min_cashout_ratio: float = 0.50,
    max_entry_slippage: float = 0.10,
    max_exit_slippage: float = 0.20,
    depth_usage_fraction: float = 0.25,
) -> BucketSignal:
    """Build one signal with optional liquidity and fee awareness."""

    orderbook = market_bucket.orderbook if use_orderbook else None
    midpoint = orderbook.midpoint if orderbook and orderbook.midpoint is not None else None
    market_price = max(0.0, min(1.0, midpoint if midpoint is not None else market_bucket.price))
    raw_edge = probability - market_price

    if orderbook is None:
        recommendation = _basic_recommendation(raw_edge, buy_edge_threshold)
        return BucketSignal(
            market_bucket=market_bucket,
            probability=probability,
            market_price=market_price,
            edge=raw_edge,
            expected_value=raw_edge,
            fair_price=probability,
            recommendation=recommendation,
            raw_edge=raw_edge,
            executable_edge=raw_edge,
            hold_to_resolution_ev=raw_edge,
            expected_exit_ev=raw_edge,
            limit_bid=None,
            executable_entry_cost=market_price,
            expected_exit_cost=0.0,
            entry_fee_cost=0.0,
        )

    buy_probe = orderbook.estimate_market_buy(
        _probe_stake(orderbook),
        fee_rate=fee_rate,
    )
    entry_price = buy_probe.effective_price
    executable_edge = probability - entry_price if entry_price is not None else raw_edge
    hold_to_resolution_ev = executable_edge

    sell_probe = (
        orderbook.estimate_market_sell(buy_probe.filled_shares, fee_rate=fee_rate)
        if buy_probe.filled_shares > 0
        else None
    )
    cashout_ratio = (
        orderbook.cashout_ratio(
            shares=buy_probe.filled_shares,
            mark_price=market_price,
            fee_rate=fee_rate,
        )
        if buy_probe.filled_shares > 0
        else None
    )
    exit_price = sell_probe.effective_price if sell_probe else None
    exit_now_ev = (
        exit_price - entry_price
        if exit_price is not None and entry_price is not None
        else None
    )
    expected_exit_ev = (
        probability * cashout_ratio - entry_price
        if cashout_ratio is not None and entry_price is not None
        else executable_edge
    )
    passive_entry = recommend_passive_entry(
        market_bucket,
        model_probability=probability,
        min_edge=buy_edge_threshold,
        maker_fee_rate=maker_fee_rate,
        taker_fee_rate=fee_rate,
    )

    max_position_shares = orderbook.max_position_by_depth(
        mark_price=market_price,
        max_exit_slippage=max_exit_slippage,
        depth_usage_fraction=depth_usage_fraction,
    )
    entry_stake_cap = orderbook.max_buy_usdc_by_depth(
        max_entry_slippage=max_entry_slippage,
        depth_usage_fraction=depth_usage_fraction,
        fee_rate=fee_rate,
    )
    depth_stake_cap = 0.0
    if entry_price is not None:
        depth_stake_cap = min(entry_stake_cap, max_position_shares * entry_price)
    max_sell_estimate = (
        orderbook.estimate_market_sell(max_position_shares, fee_rate=fee_rate)
        if max_position_shares > 0
        else None
    )
    best_bid = orderbook.best_bid
    target_exit_price = (
        market_price * (1.0 - max(0.0, max_exit_slippage))
        if market_price > 0
        else 0.0
    )
    liquidity = LiquidityMetrics(
        token_id=market_bucket.token_id,
        best_bid=orderbook.best_bid,
        best_ask=orderbook.best_ask,
        midpoint=orderbook.midpoint,
        spread=orderbook.spread,
        buy_estimate=buy_probe,
        sell_estimate=sell_probe,
        cashout_ratio=cashout_ratio,
        depth_to_target_price=orderbook.depth_to_target_price(
            side="sell",
            target_price=target_exit_price,
        ),
        max_position_shares=max_position_shares,
        depth_based_stake_cap=depth_stake_cap,
        max_sell_value=max_sell_estimate.net_value if max_sell_estimate else 0.0,
    )
    recommendation = _liquidity_recommendation(
        raw_edge=raw_edge,
        executable_edge=executable_edge,
        buy_edge_threshold=buy_edge_threshold,
        buy_probe=buy_probe,
        sell_probe=sell_probe,
        cashout_ratio=cashout_ratio,
        min_cashout_ratio=min_cashout_ratio,
        max_entry_slippage=max_entry_slippage,
        max_exit_slippage=max_exit_slippage,
        best_bid=best_bid,
        passive_action=passive_entry.action,
    )
    return BucketSignal(
        market_bucket=market_bucket,
        probability=probability,
        market_price=market_price,
        edge=executable_edge,
        expected_value=hold_to_resolution_ev,
        fair_price=probability,
        recommendation=recommendation,
        raw_edge=raw_edge,
        executable_edge=executable_edge,
        hold_to_resolution_ev=hold_to_resolution_ev,
        exit_now_ev=exit_now_ev,
        expected_exit_ev=expected_exit_ev,
        limit_bid=passive_entry.limit_bid,
        executable_entry_cost=passive_entry.executable_entry_cost,
        expected_exit_cost=passive_entry.expected_exit_cost,
        entry_fee_cost=passive_entry.fee,
        liquidity=liquidity,
    )


def _probe_stake(market_bucket_orderbook) -> float:  # noqa: ANN001
    best_ask = market_bucket_orderbook.best_ask
    if best_ask is None or best_ask <= 0:
        return 100.0
    first_level_size = market_bucket_orderbook.asks[0].size if market_bucket_orderbook.asks else 0.0
    return max(1.0, min(100.0, best_ask * max(1.0, first_level_size)))


def _basic_recommendation(edge: float, buy_edge_threshold: float) -> str:
    if edge >= buy_edge_threshold:
        return "TAKE_EDGE_SMALL"
    if edge <= -buy_edge_threshold:
        return "PASSIVE_EXIT_ONLY"
    if edge <= 0:
        return "SKIP_NO_EDGE"
    return "WATCH"


def _liquidity_recommendation(
    *,
    raw_edge: float,
    executable_edge: float,
    buy_edge_threshold: float,
    buy_probe,
    sell_probe,
    cashout_ratio: float | None,
    min_cashout_ratio: float,
    max_entry_slippage: float,
    max_exit_slippage: float,
    best_bid: float | None,
    passive_action: str,
) -> str:
    if raw_edge <= -buy_edge_threshold and best_bid is not None:
        return "PASSIVE_EXIT_ONLY"
    if raw_edge < buy_edge_threshold:
        return "SKIP_NO_EDGE" if raw_edge <= 0 else "WATCH"
    if not buy_probe.is_complete or buy_probe.filled_shares <= 0:
        return "SKIP_ILLIQUID"
    if buy_probe.slippage is not None and buy_probe.slippage > max_entry_slippage:
        return "SKIP_ILLIQUID"
    if executable_edge < buy_edge_threshold:
        return "SKIP_NO_EDGE"
    exit_slippage_too_high = (
        sell_probe is None
        or not sell_probe.is_complete
        or (
            sell_probe.slippage is not None
            and sell_probe.slippage > max_exit_slippage
        )
    )
    cashout_too_low = (
        cashout_ratio is not None
        and cashout_ratio < min_cashout_ratio
    )
    if exit_slippage_too_high or cashout_too_low:
        return "HOLD_TO_RESOLUTION_ONLY" if executable_edge >= buy_edge_threshold else "SKIP_ILLIQUID"
    if passive_action in {"ACCUMULATE_PASSIVE", "TAKE_EDGE_SMALL"}:
        return passive_action
    return "TAKE_EDGE_SMALL"
