"""Simple backtesting for historical weather bucket signals."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping

from weather_quant.buckets import parse_temperature_bucket
from weather_quant.engine import build_bucket_signal
from weather_quant.ledger import PerformanceStats, TradeRecord, compute_performance_stats
from weather_quant.market import parse_orderbook_snapshot
from weather_quant.models import (
    DEFAULT_TAKER_FEE_RATE,
    BucketSignal,
    MarketBucket,
    OrderBookSnapshot,
    Portfolio,
    Position,
)
from weather_quant.portfolio import (
    calculate_hedge_lock,
    generate_passive_exit_plan,
)
from weather_quant.risk import PositionSizer, RiskConfig


@dataclass(frozen=True)
class HistoricalSignal:
    city_id: str
    target_date: date
    kind: str
    outcome: str
    probability: float
    market_price: float
    settled_outcome: bool
    market_slug: str | None = None
    token_id: str | None = None


@dataclass(frozen=True)
class HistoricalOrderBookSignal:
    timestamp: datetime
    city_id: str
    target_date: date
    kind: str
    outcome: str
    probability: float
    settled_outcome: bool
    orderbook: OrderBookSnapshot
    exit_orderbook: OrderBookSnapshot | None = None
    market_slug: str | None = None


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[TradeRecord, ...]
    stats: PerformanceStats
    ending_bankroll: float


def load_historical_signals_csv(path: Path) -> tuple[HistoricalSignal, ...]:
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        rows: list[HistoricalSignal] = []
        for row in reader:
            rows.append(
                HistoricalSignal(
                    city_id=row["city_id"],
                    target_date=date.fromisoformat(row["target_date"]),
                    kind=row["kind"],
                    outcome=row["outcome"],
                    probability=float(row["probability"]),
                    market_price=float(row["market_price"]),
                    settled_outcome=str(row["settled_outcome"]).strip().lower()
                    in {"1", "true", "yes", "y", "win"},
                    market_slug=row.get("market_slug") or None,
                    token_id=row.get("token_id") or None,
                )
            )
        return tuple(rows)


def load_orderbook_snapshot_file(path: Path) -> tuple[HistoricalOrderBookSignal, ...]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("Orderbook JSON root must be a list.")
        return tuple(
            _orderbook_signal_from_mapping(item)
            for item in payload
            if isinstance(item, Mapping)
        )
    return load_orderbook_snapshot_csv(path)


def load_orderbook_snapshot_csv(path: Path) -> tuple[HistoricalOrderBookSignal, ...]:
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        return tuple(_orderbook_signal_from_mapping(row) for row in reader)


def _orderbook_signal_from_mapping(row: Mapping[str, Any]) -> HistoricalOrderBookSignal:
    token_id = str(row.get("token_id") or row.get("asset_id") or "")
    if not token_id:
        raise ValueError("Orderbook snapshot row requires token_id.")
    timestamp = _parse_timestamp(row.get("timestamp") or row.get("time"))
    target_date = date.fromisoformat(str(row["target_date"]))
    bids = _levels_payload(row.get("bid_levels") or row.get("bids"))
    asks = _levels_payload(row.get("ask_levels") or row.get("asks"))
    exit_bids = _levels_payload(row.get("exit_bid_levels") or row.get("exit_bids"))
    exit_asks = _levels_payload(row.get("exit_ask_levels") or row.get("exit_asks"))
    exit_orderbook = None
    if exit_bids or exit_asks:
        exit_orderbook = parse_orderbook_snapshot(
            token_id=token_id,
            payload={"bids": exit_bids, "asks": exit_asks},
        )
    return HistoricalOrderBookSignal(
        timestamp=timestamp,
        city_id=str(row.get("city_id") or ""),
        target_date=target_date,
        kind=str(row.get("kind") or "high"),
        outcome=str(row["outcome"]),
        probability=float(row["probability"]),
        settled_outcome=_parse_bool(row.get("settled_outcome")),
        market_slug=str(row["market_slug"]) if row.get("market_slug") else None,
        orderbook=parse_orderbook_snapshot(
            token_id=token_id,
            payload={"bids": bids, "asks": asks},
        ),
        exit_orderbook=exit_orderbook,
    )


def _parse_timestamp(value: Any) -> datetime:
    if value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return datetime.now(timezone.utc)


def _levels_payload(value: Any) -> Any:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            levels = []
            for part in text.split("|"):
                if not part.strip():
                    continue
                price, _, size = part.partition(":")
                levels.append({"price": price.strip(), "size": size.strip()})
            return levels
    return value


def _parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "win"}


def run_backtest(
    signals: tuple[HistoricalSignal, ...],
    *,
    risk: RiskConfig,
) -> BacktestResult:
    sizer = PositionSizer()
    bankroll = risk.bankroll
    trades: list[TradeRecord] = []
    daily_exposure: dict[date, float] = {}
    market_exposure: dict[str, float] = {}
    city_exposure: dict[str, float] = {}

    for index, historical in enumerate(sorted(signals, key=lambda row: row.target_date)):
        market_bucket = MarketBucket(
            market_id=historical.market_slug or "historical",
            question="Historical weather market",
            slug=historical.market_slug,
            condition_id=None,
            outcome=historical.outcome,
            price=historical.market_price,
            token_id=historical.token_id,
            bucket=parse_temperature_bucket(historical.outcome),
        )
        edge = historical.probability - historical.market_price
        signal = BucketSignal(
            market_bucket=market_bucket,
            probability=historical.probability,
            market_price=historical.market_price,
            edge=edge,
            expected_value=edge,
            fair_price=historical.probability,
            recommendation="BUY_YES" if edge > 0 else "WATCH",
        )
        current_risk = replace(risk, bankroll=bankroll)
        market_key = historical.market_slug or historical.outcome
        recommendation = sizer.size_yes(
            signal,
            current_risk,
            current_daily_exposure=daily_exposure.get(historical.target_date, 0.0),
            current_market_exposure=market_exposure.get(market_key, 0.0),
            current_city_exposure=city_exposure.get(historical.city_id, 0.0),
        )
        if not recommendation.should_trade:
            continue
        if historical.settled_outcome:
            pnl = recommendation.potential_profit
        else:
            pnl = -recommendation.max_loss
        bankroll += pnl
        daily_exposure[historical.target_date] = (
            daily_exposure.get(historical.target_date, 0.0) + recommendation.stake
        )
        market_exposure[market_key] = market_exposure.get(market_key, 0.0) + recommendation.stake
        city_exposure[historical.city_id] = (
            city_exposure.get(historical.city_id, 0.0) + recommendation.stake
        )
        trades.append(
            TradeRecord(
                trade_id=f"backtest:{index}",
                timestamp=datetime.combine(
                    historical.target_date,
                    time.min,
                    tzinfo=timezone.utc,
                ),
                city_id=historical.city_id,
                target_date=historical.target_date,
                kind=historical.kind,
                market_slug=historical.market_slug,
                outcome=historical.outcome,
                token_id=historical.token_id,
                side="YES",
                price=historical.market_price,
                probability=historical.probability,
                edge=edge,
                stake=recommendation.stake,
                shares=recommendation.shares,
                status="settled",
                realized_pnl=pnl,
            )
        )

    trade_tuple = tuple(trades)
    return BacktestResult(
        trades=trade_tuple,
        stats=compute_performance_stats(trade_tuple),
        ending_bankroll=bankroll,
    )


def run_orderbook_snapshot_backtest(
    signals: tuple[HistoricalOrderBookSignal, ...],
    *,
    risk: RiskConfig,
    passive_entry_fill: bool = False,
    passive_exit_ladder: bool = False,
    hedge_lock: bool = False,
    hold_to_resolution: bool = True,
) -> BacktestResult:
    sizer = PositionSizer()
    bankroll = risk.bankroll
    trades: list[TradeRecord] = []
    daily_exposure: dict[date, float] = {}
    market_exposure: dict[str, float] = {}
    city_exposure: dict[str, float] = {}

    for index, historical in enumerate(sorted(signals, key=lambda row: row.timestamp)):
        market_price = (
            historical.orderbook.midpoint
            if historical.orderbook.midpoint is not None
            else historical.orderbook.best_ask
            if historical.orderbook.best_ask is not None
            else 0.0
        )
        market_bucket = MarketBucket(
            market_id=historical.market_slug or "orderbook-snapshot",
            question="Historical orderbook weather market",
            slug=historical.market_slug,
            condition_id=None,
            outcome=historical.outcome,
            price=market_price,
            token_id=historical.orderbook.token_id,
            bucket=parse_temperature_bucket(historical.outcome),
            orderbook=historical.orderbook,
        )
        signal = build_bucket_signal(
            market_bucket=market_bucket,
            probability=historical.probability,
            buy_edge_threshold=risk.min_edge,
            use_orderbook=True,
            fee_rate=risk.fee_rate,
            min_cashout_ratio=risk.min_cashout_ratio,
            max_entry_slippage=risk.max_entry_slippage,
            max_exit_slippage=risk.max_exit_slippage,
            depth_usage_fraction=risk.depth_usage_fraction,
        )
        current_risk = replace(risk, bankroll=bankroll)
        market_key = historical.market_slug or historical.orderbook.token_id
        recommendation = sizer.size_yes(
            signal,
            current_risk,
            current_daily_exposure=daily_exposure.get(historical.target_date, 0.0),
            current_market_exposure=market_exposure.get(market_key, 0.0),
            current_city_exposure=city_exposure.get(historical.city_id, 0.0),
        )
        if not recommendation.should_trade:
            continue

        entry_price = (
            recommendation.entry_fill.effective_price
            if recommendation.entry_fill and recommendation.entry_fill.effective_price is not None
            else signal.market_price
        )
        shares = recommendation.shares
        stake = recommendation.stake
        notes = ["orderbook snapshot backtest"]
        if passive_entry_fill and signal.limit_bid is not None:
            passive_fill = _simulate_passive_entry(
                historical.orderbook,
                stake=stake,
                limit_bid=signal.limit_bid,
            )
            if passive_fill is None:
                continue
            entry_price, shares, stake = passive_fill
            notes.append("passive entry fill")

        hedge_note = ""
        if hedge_lock:
            lock_result = calculate_hedge_lock(
                Portfolio(
                    positions=(
                        Position(
                            outcome=historical.outcome,
                            bucket=market_bucket.bucket,
                            shares=shares,
                            total_cost=stake,
                            token_id=historical.orderbook.token_id,
                        ),
                    )
                ),
                (market_bucket,),
                probabilities={market_bucket.bucket.canonical_key: historical.probability},
                fee_rate=risk.fee_rate,
            )
            hedge_note = f" hedge={lock_result.recommendation}"
            notes.append(f"hedge lock {lock_result.recommendation}")

        if passive_exit_ladder and historical.exit_orderbook is not None:
            pnl = _simulate_passive_exit_ladder(
                outcome=historical.outcome,
                market_bucket=market_bucket,
                exit_orderbook=historical.exit_orderbook,
                shares=shares,
                stake=stake,
                settled_outcome=historical.settled_outcome,
                hold_to_resolution=hold_to_resolution,
                fee_rate=risk.fee_rate,
            )
            notes.append("passive exit ladder")
        elif historical.exit_orderbook is not None:
            exit_fill = historical.exit_orderbook.estimate_market_sell(
                shares,
                fee_rate=risk.fee_rate,
            )
            pnl = exit_fill.net_value - stake
        elif historical.settled_outcome and hold_to_resolution:
            pnl = shares - stake
        else:
            pnl = -stake
        bankroll += pnl
        daily_exposure[historical.target_date] = (
            daily_exposure.get(historical.target_date, 0.0) + stake
        )
        market_exposure[market_key] = market_exposure.get(market_key, 0.0) + stake
        city_exposure[historical.city_id] = (
            city_exposure.get(historical.city_id, 0.0) + stake
        )
        trades.append(
            TradeRecord(
                trade_id=f"orderbook-backtest:{index}",
                timestamp=historical.timestamp,
                city_id=historical.city_id,
                target_date=historical.target_date,
                kind=historical.kind,
                market_slug=historical.market_slug,
                outcome=historical.outcome,
                token_id=historical.orderbook.token_id,
                side="YES",
                price=entry_price,
                probability=historical.probability,
                edge=signal.executable_edge if signal.executable_edge is not None else signal.edge,
                stake=stake,
                shares=shares,
                status="settled",
                realized_pnl=pnl,
                notes="; ".join(notes) + hedge_note,
            )
        )

    trade_tuple = tuple(trades)
    return BacktestResult(
        trades=trade_tuple,
        stats=compute_performance_stats(trade_tuple),
        ending_bankroll=bankroll,
    )


def _simulate_passive_entry(
    orderbook: OrderBookSnapshot,
    *,
    stake: float,
    limit_bid: float,
) -> tuple[float, float, float] | None:
    best_ask = orderbook.best_ask
    if best_ask is None or best_ask > limit_bid:
        return None
    entry_price = max(0.0, min(1.0, limit_bid))
    shares = stake / entry_price if entry_price > 0 else 0.0
    if shares <= 0:
        return None
    return entry_price, shares, stake


def _simulate_passive_exit_ladder(
    *,
    outcome: str,
    market_bucket: MarketBucket,
    exit_orderbook: OrderBookSnapshot,
    shares: float,
    stake: float,
    settled_outcome: bool,
    hold_to_resolution: bool,
    fee_rate: float,
) -> float:
    position = Position(
        outcome=outcome,
        bucket=market_bucket.bucket,
        shares=shares,
        total_cost=stake,
        token_id=market_bucket.token_id,
    )
    exit_market_bucket = MarketBucket(
        market_id=market_bucket.market_id,
        question=market_bucket.question,
        slug=market_bucket.slug,
        condition_id=market_bucket.condition_id,
        outcome=market_bucket.outcome,
        price=exit_orderbook.midpoint or market_bucket.price,
        bucket=market_bucket.bucket,
        token_id=market_bucket.token_id,
        orderbook=exit_orderbook,
        raw_payload=market_bucket.raw_payload,
    )
    plan = generate_passive_exit_plan(
        position,
        exit_market_bucket,
        fee_rate=fee_rate,
    )
    realized = 0.0
    sold_shares = 0.0
    best_bid = exit_orderbook.best_bid or 0.0
    for leg in plan.ladder:
        if best_bid >= leg.limit_price:
            realized += leg.net_value
            sold_shares += leg.shares
    retained = max(0.0, shares - sold_shares)
    if hold_to_resolution and settled_outcome:
        realized += retained
    return realized - stake


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="weather backtest",
        description="Backtest historical Polymarket weather bucket signals.",
    )
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--orderbook-snapshots", type=Path)
    parser.add_argument("--bankroll", type=float, default=1_000.0)
    parser.add_argument("--kelly", default="half", choices=["full", "half", "quarter"])
    parser.add_argument("--min-edge", type=float, default=0.03)
    parser.add_argument("--fee-rate", type=float, default=DEFAULT_TAKER_FEE_RATE)
    parser.add_argument("--min-cashout-ratio", type=float, default=0.50)
    parser.add_argument("--max-entry-slippage", type=float, default=0.10)
    parser.add_argument("--max-exit-slippage", type=float, default=0.20)
    parser.add_argument("--depth-usage-fraction", type=float, default=0.25)
    parser.add_argument("--passive-entry-fill", action="store_true")
    parser.add_argument("--passive-exit-ladder", action="store_true")
    parser.add_argument("--hedge-lock", action="store_true")
    parser.add_argument("--no-hold-to-resolution", action="store_true")
    args = parser.parse_args(argv)

    risk = RiskConfig(
        bankroll=args.bankroll,
        kelly_mode=args.kelly,
        min_edge=args.min_edge,
        fee_rate=args.fee_rate,
        min_cashout_ratio=args.min_cashout_ratio,
        max_entry_slippage=args.max_entry_slippage,
        max_exit_slippage=args.max_exit_slippage,
        depth_usage_fraction=args.depth_usage_fraction,
    )
    if args.orderbook_snapshots:
        result = run_orderbook_snapshot_backtest(
            load_orderbook_snapshot_file(args.orderbook_snapshots),
            risk=risk,
            passive_entry_fill=args.passive_entry_fill,
            passive_exit_ladder=args.passive_exit_ladder,
            hedge_lock=args.hedge_lock,
            hold_to_resolution=not args.no_hold_to_resolution,
        )
    elif args.csv:
        result = run_backtest(load_historical_signals_csv(args.csv), risk=risk)
    else:
        parser.error("--csv or --orderbook-snapshots is required")
    stats = result.stats
    print(f"trades={stats.total_trades}")
    print(f"settled={stats.settled_trades}")
    print(f"win_rate={stats.win_rate:.2%}")
    print(f"total_pnl={stats.total_pnl:.2f}")
    print(f"roi={stats.roi:.2%}")
    print(f"max_drawdown={stats.max_drawdown:.2f}")
    print(f"ending_bankroll={result.ending_bankroll:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
