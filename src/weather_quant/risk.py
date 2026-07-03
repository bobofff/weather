"""Position sizing and risk controls for binary weather markets."""

from __future__ import annotations

from dataclasses import dataclass

from weather_quant.models import (
    DEFAULT_TAKER_FEE_RATE,
    BucketSignal,
    FillEstimate,
)


KELLY_MULTIPLIERS = {
    "full": 1.0,
    "half": 0.5,
    "quarter": 0.25,
}

ENTRY_ACTIONS = {"BUY", "BUY_YES", "ACCUMULATE_PASSIVE", "TAKE_EDGE_SMALL"}
SKIP_ACTIONS = {
    "SKIP_NO_EDGE",
    "SKIP_ILLIQUID",
    "SKIP_OVERROUND",
    "TAIL_RISK_TOO_HIGH",
    "PASSIVE_EXIT_ONLY",
}


@dataclass(frozen=True)
class RiskConfig:
    bankroll: float
    kelly_mode: str = "half"
    min_edge: float = 0.03
    min_price: float = 0.01
    max_price: float = 0.95
    max_trade_fraction: float = 0.03
    max_daily_fraction: float = 0.12
    max_market_fraction: float = 0.06
    max_city_fraction: float = 0.08
    min_stake: float = 1.0
    fee_rate: float = DEFAULT_TAKER_FEE_RATE
    min_cashout_ratio: float = 0.50
    max_entry_slippage: float = 0.10
    max_exit_slippage: float = 0.20
    depth_usage_fraction: float = 0.25

    @property
    def kelly_multiplier(self) -> float:
        text = str(self.kelly_mode).strip().lower()
        if text in KELLY_MULTIPLIERS:
            return KELLY_MULTIPLIERS[text]
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError(f"Unsupported Kelly mode: {self.kelly_mode}") from exc
        return max(0.0, min(value, 1.0))


@dataclass(frozen=True)
class PositionRecommendation:
    should_trade: bool
    reason: str
    stake: float
    shares: float
    full_kelly_fraction: float
    scaled_kelly_fraction: float
    capped_fraction: float
    max_loss: float
    potential_profit: float
    depth_based_stake_cap: float | None = None
    entry_fill: FillEstimate | None = None
    exit_fill: FillEstimate | None = None
    cashout_ratio: float | None = None


class PositionSizer:
    """Kelly sizing with hard exposure caps."""

    def size_yes(
        self,
        signal: BucketSignal,
        risk: RiskConfig,
        *,
        current_daily_exposure: float = 0.0,
        current_market_exposure: float = 0.0,
        current_city_exposure: float = 0.0,
    ) -> PositionRecommendation:
        probability = max(0.0, min(1.0, signal.probability))
        if signal.recommendation in SKIP_ACTIONS:
            return self._skip(f"signal action is {signal.recommendation}")

        price = self._entry_price(signal)
        edge = signal.executable_edge if signal.executable_edge is not None else probability - price

        if edge < risk.min_edge:
            return self._skip(f"edge below minimum: {edge:.4f}")
        if price < risk.min_price or price > risk.max_price:
            return self._skip(f"price outside allowed range: {price:.4f}")
        if risk.bankroll <= 0:
            return self._skip("bankroll must be positive")

        full_kelly_fraction = max(0.0, edge / max(1.0 - price, 1e-9))
        scaled_kelly_fraction = full_kelly_fraction * risk.kelly_multiplier

        remaining_daily = max(0.0, risk.bankroll * risk.max_daily_fraction - current_daily_exposure)
        remaining_market = max(0.0, risk.bankroll * risk.max_market_fraction - current_market_exposure)
        remaining_city = max(0.0, risk.bankroll * risk.max_city_fraction - current_city_exposure)
        cap_fraction = min(
            risk.max_trade_fraction,
            remaining_daily / risk.bankroll,
            remaining_market / risk.bankroll,
            remaining_city / risk.bankroll,
        )
        capped_fraction = max(0.0, min(scaled_kelly_fraction, cap_fraction))
        stake = risk.bankroll * capped_fraction
        depth_based_stake_cap = self._depth_based_stake_cap(signal, risk)
        if depth_based_stake_cap is not None:
            stake = min(stake, depth_based_stake_cap)
        if stake < risk.min_stake:
            return PositionRecommendation(
                should_trade=False,
                reason="stake below minimum after caps",
                stake=0.0,
                shares=0.0,
                full_kelly_fraction=full_kelly_fraction,
                scaled_kelly_fraction=scaled_kelly_fraction,
                capped_fraction=capped_fraction,
                max_loss=0.0,
                potential_profit=0.0,
                depth_based_stake_cap=depth_based_stake_cap,
            )

        entry_fill = None
        exit_fill = None
        cashout_ratio = None
        if signal.market_bucket.orderbook is not None:
            entry_fill = signal.market_bucket.orderbook.estimate_market_buy(
                stake,
                fee_rate=risk.fee_rate,
            )
            if entry_fill.filled_shares <= 0:
                return self._skip("no executable ask depth")
            if not entry_fill.is_complete:
                stake = entry_fill.net_value
            if entry_fill.slippage is not None and entry_fill.slippage > risk.max_entry_slippage:
                return self._skip(f"entry slippage too high: {entry_fill.slippage:.4f}")
            shares = entry_fill.filled_shares
            stake = entry_fill.net_value
            exit_fill = signal.market_bucket.orderbook.estimate_market_sell(
                shares,
                fee_rate=risk.fee_rate,
            )
            cashout_ratio = signal.market_bucket.orderbook.cashout_ratio(
                shares=shares,
                mark_price=signal.market_price,
                fee_rate=risk.fee_rate,
            )
            if (
                signal.recommendation in ENTRY_ACTIONS
                and cashout_ratio is not None
                and cashout_ratio < risk.min_cashout_ratio
            ):
                return self._skip(f"cashout ratio too low: {cashout_ratio:.4f}")
            if (
                signal.recommendation in ENTRY_ACTIONS
                and exit_fill.slippage is not None
                and exit_fill.slippage > risk.max_exit_slippage
            ):
                return self._skip(f"exit slippage too high: {exit_fill.slippage:.4f}")
        else:
            shares = stake / price

        potential_profit = shares - stake
        return PositionRecommendation(
            should_trade=True,
            reason="ok",
            stake=stake,
            shares=shares,
            full_kelly_fraction=full_kelly_fraction,
            scaled_kelly_fraction=scaled_kelly_fraction,
            capped_fraction=capped_fraction,
            max_loss=stake,
            potential_profit=potential_profit,
            depth_based_stake_cap=depth_based_stake_cap,
            entry_fill=entry_fill,
            exit_fill=exit_fill,
            cashout_ratio=cashout_ratio,
        )

    @staticmethod
    def _skip(reason: str) -> PositionRecommendation:
        return PositionRecommendation(
            should_trade=False,
            reason=reason,
            stake=0.0,
            shares=0.0,
            full_kelly_fraction=0.0,
            scaled_kelly_fraction=0.0,
            capped_fraction=0.0,
            max_loss=0.0,
            potential_profit=0.0,
        )

    @staticmethod
    def _entry_price(signal: BucketSignal) -> float:
        if (
            signal.liquidity is not None
            and signal.liquidity.buy_estimate is not None
            and signal.liquidity.buy_estimate.effective_price is not None
        ):
            return max(0.0, min(1.0, signal.liquidity.buy_estimate.effective_price))
        return max(0.0, min(1.0, signal.market_price))

    @staticmethod
    def _depth_based_stake_cap(
        signal: BucketSignal,
        risk: RiskConfig,
    ) -> float | None:
        if signal.liquidity is not None:
            return max(0.0, signal.liquidity.depth_based_stake_cap)
        orderbook = signal.market_bucket.orderbook
        if orderbook is None:
            return None
        entry_cap = orderbook.max_buy_usdc_by_depth(
            max_entry_slippage=risk.max_entry_slippage,
            depth_usage_fraction=risk.depth_usage_fraction,
            fee_rate=risk.fee_rate,
        )
        max_shares = orderbook.max_position_by_depth(
            mark_price=signal.market_price,
            max_exit_slippage=risk.max_exit_slippage,
            depth_usage_fraction=risk.depth_usage_fraction,
        )
        entry_price = PositionSizer._entry_price(signal)
        return min(entry_cap, max_shares * entry_price)
