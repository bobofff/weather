"""Command line interface for Polymarket weather trading research."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path

from weather_quant.backtest import main as backtest_main
from weather_quant.cache import FileCache
from weather_quant.config import (
    WeatherTradingConfig,
    get_city,
    load_trading_config,
)
from weather_quant.db import DEFAULT_DB_PATH, init_database
from weather_quant.ensemble import (
    build_bucket_distribution,
    default_buckets_for_run,
    ensemble_signal_rows,
)
from weather_quant.engine import PredictionEngine
from weather_quant.http import JsonHttpClient
from weather_quant.ledger import (
    TradeLedger,
    TradeRecord,
    make_trade_id,
)
from weather_quant.market import GammaMarketClient
from weather_quant.models import (
    DEFAULT_MAKER_FEE_RATE,
    DEFAULT_TAKER_FEE_RATE,
    BucketSignal,
    Portfolio,
    PortfolioLockResult,
    PortfolioScenario,
    TemperatureKind,
)
from weather_quant.portfolio import (
    calculate_hedge_lock,
    generate_passive_exit_plan,
    load_market_buckets_file,
    load_positions_file,
    market_buckets_from_positions,
    orderbook_overround,
    portfolio_cashout_ratio,
    portfolio_liquidation_value,
    portfolio_mark_value,
    probabilities_from_market_buckets,
    value_portfolio,
)
from weather_quant.risk import PositionSizer, RiskConfig
from weather_quant.storage import WeatherStorage
from weather_quant.weather import (
    NWSForecastClient,
    OpenMeteoEnsembleClient,
    OpenMeteoForecastClient,
    WeatherEnsembleProvider,
)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期格式必须是 YYYY-MM-DD") from exc


def _parse_models(value: str | None) -> tuple[str, ...] | None:
    if not value:
        return None
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _load_config(args: argparse.Namespace) -> WeatherTradingConfig:
    config_path = Path(args.config).expanduser() if getattr(args, "config", None) else None
    return load_trading_config(config_path)


def _build_market_client(config: WeatherTradingConfig) -> GammaMarketClient:
    gamma_http = JsonHttpClient(
        base_url=config.gamma_base_url,
        timeout_seconds=config.request_timeout_seconds,
    )
    clob_http = JsonHttpClient(
        base_url=config.clob_base_url,
        timeout_seconds=config.request_timeout_seconds,
    )
    return GammaMarketClient(
        gamma_base_url=config.gamma_base_url,
        clob_base_url=config.clob_base_url,
        http_client=gamma_http,
        clob_http_client=clob_http,
        cache=FileCache(),
        cache_max_age_seconds=min(config.cache_max_age_seconds, 90),
    )


def _build_engine(config: WeatherTradingConfig, *, buy_edge_threshold: float) -> PredictionEngine:
    cache = FileCache()
    open_meteo_http = JsonHttpClient(
        base_url=config.open_meteo_base_url,
        timeout_seconds=config.request_timeout_seconds,
    )
    nws_http = JsonHttpClient(
        base_url="https://api.weather.gov",
        timeout_seconds=config.request_timeout_seconds,
        user_agent="weather-polymarket/0.1 contact:local",
    )
    weather_provider = WeatherEnsembleProvider(
        open_meteo=OpenMeteoForecastClient(
            base_url=config.open_meteo_base_url,
            http_client=open_meteo_http,
            cache=cache,
            cache_max_age_seconds=config.cache_max_age_seconds,
        ),
        nws=NWSForecastClient(
            http_client=nws_http,
            cache=cache,
            cache_max_age_seconds=config.cache_max_age_seconds,
        ),
    )
    return PredictionEngine(
        weather_provider=weather_provider,
        market_client=_build_market_client(config),
        buy_edge_threshold=buy_edge_threshold,
    )


def _kind(value: str) -> TemperatureKind:
    text = value.strip().lower()
    if text not in {"high", "low"}:
        raise argparse.ArgumentTypeError("--kind must be high or low")
    return text  # type: ignore[return-value]


def _fmt_float(value: float | None, *, precision: int = 4, percent: bool = False) -> str:
    if value is None:
        return "-"
    if percent:
        return f"{value:.2%}"
    return f"{value:.{precision}f}"


def forecast_command(args: argparse.Namespace) -> int:
    config = _load_config(args)
    city = get_city(args.city, config)
    engine = _build_engine(config, buy_edge_threshold=args.min_edge)
    ensemble = engine.weather_provider.fetch_ensemble(
        city,
        target_date=args.date,
        kind=args.kind,
        models=_parse_models(args.models),
    )
    print(f"城市: {city.name} ({city.city_id})")
    print(f"日期: {args.date.isoformat()}  类型: {args.kind}  结算单位: {city.settlement_unit}")
    for point in ensemble.points:
        print(f"{point.source_model:18s} {point.value:8.2f} {point.unit}")
    return 0


def _db_path(args: argparse.Namespace) -> Path | None:
    return Path(args.db).expanduser() if getattr(args, "db", None) else None


def db_init_command(args: argparse.Namespace) -> int:
    path = init_database(_db_path(args))
    print(f"SQLite 初始化完成: {path}")
    return 0


def db_runs_command(args: argparse.Namespace) -> int:
    rows = WeatherStorage(_db_path(args)).recent_runs(limit=args.limit)
    print("run_key                              model              city       date       kind members fetched_at")
    print("-" * 112)
    for row in rows:
        print(
            f"{row['run_key'][:36]:36s} "
            f"{row['model'][:18]:18s} "
            f"{row['city_id'][:10]:10s} "
            f"{row['target_date']:10s} "
            f"{row['kind']:4s} "
            f"{row['member_count']:7d} "
            f"{row['fetched_at']}"
        )
    return 0


def db_probabilities_command(args: argparse.Namespace) -> int:
    rows = WeatherStorage(_db_path(args)).recent_probabilities(limit=args.limit)
    print("run_key                              bucket             hits/total probability mean   p10   p50   p90")
    print("-" * 112)
    for row in rows:
        print(
            f"{row['run_key'][:36]:36s} "
            f"{row['bucket_label'][:18]:18s} "
            f"{row['hit_count']:4d}/{row['total_members']:<4d} "
            f"{row['probability']:10.2%} "
            f"{_fmt_float(row['empirical_mean'], precision=2):>6s} "
            f"{_fmt_float(row['p10'], precision=2):>6s} "
            f"{_fmt_float(row['p50'], precision=2):>6s} "
            f"{_fmt_float(row['p90'], precision=2):>6s}"
        )
    return 0


def _ensemble_client(config: WeatherTradingConfig) -> OpenMeteoEnsembleClient:
    return OpenMeteoEnsembleClient(
        base_url=config.open_meteo_ensemble_base_url,
        http_client=JsonHttpClient(
            base_url=config.open_meteo_ensemble_base_url,
            timeout_seconds=config.request_timeout_seconds,
        ),
        cache=FileCache(),
        cache_max_age_seconds=config.cache_max_age_seconds,
    )


def _load_signal_market_buckets(args: argparse.Namespace, config: WeatherTradingConfig, unit: str):
    if getattr(args, "markets", None):
        return load_market_buckets_file(Path(args.markets).expanduser(), default_unit=unit)
    query = args.query
    if not query and not args.slug and not args.condition_id:
        return ()
    client = _build_market_client(config)
    if query and not (args.slug or args.condition_id):
        return client.discover_weather_market_buckets(
            query=query,
            default_unit=unit,
            kind=args.kind,
            target_date=args.date,
            refresh_clob_midpoints=not args.use_orderbook,
            include_orderbooks=args.use_orderbook,
        )
    return client.get_market_buckets(
        query=query,
        slug=args.slug,
        condition_id=args.condition_id,
        default_unit=unit,
        refresh_clob_midpoints=not args.use_orderbook,
        include_orderbooks=args.use_orderbook,
    )


def _fetch_ensemble_distribution(args: argparse.Namespace, market_buckets=()):
    config = _load_config(args)
    city = get_city(args.city, config)
    run = _ensemble_client(config).fetch_run(
        city,
        target_date=args.date,
        kind=args.kind,
        model=args.model,
        forecast_days=args.forecast_days,
    )
    buckets = tuple(item.bucket for item in market_buckets) if market_buckets else default_buckets_for_run(run)
    distribution = build_bucket_distribution(run, buckets)
    return config, city, run, distribution


def ensemble_command(args: argparse.Namespace) -> int:
    _config, city, run, distribution = _fetch_ensemble_distribution(args)
    print(
        f"城市: {city.name} ({city.city_id}) 日期: {args.date.isoformat()} "
        f"类型: {args.kind} 模型: {args.model}"
    )
    print(
        f"members={distribution.total_members} unmatched={distribution.unmatched_count} "
        f"mean={_fmt_float(distribution.empirical_mean, precision=2)} "
        f"std={_fmt_float(distribution.empirical_std, precision=2)} "
        f"p10={_fmt_float(distribution.p10, precision=2)} "
        f"p50={_fmt_float(distribution.p50, precision=2)} "
        f"p90={_fmt_float(distribution.p90, precision=2)} {distribution.unit}"
    )
    print("bucket                   hits/total probability")
    print("-" * 52)
    for item in distribution.probabilities:
        print(
            f"{item.bucket.label[:24]:24s} "
            f"{item.hit_count:4d}/{item.total_members:<4d} "
            f"{item.probability:10.2%}"
        )
    if args.save:
        WeatherStorage(_db_path(args), initialize=True).save_distribution(distribution)
        print(f"已保存 SQLite: {args.db or DEFAULT_DB_PATH} run_key={run.run_key}")
    return 0


def ensemble_signal_command(args: argparse.Namespace) -> int:
    config = _load_config(args)
    city = get_city(args.city, config)
    market_buckets = _load_signal_market_buckets(args, config, city.settlement_unit)
    if not market_buckets:
        raise ValueError("ensemble-signal requires --markets or a Polymarket selector.")
    _config, _city, run, distribution = _fetch_ensemble_distribution(args, market_buckets)
    rows = ensemble_signal_rows(
        distribution,
        market_buckets,
        fee_rate=args.fee_rate,
        min_edge=args.min_edge,
    )
    print(
        "outcome                  ensProb hits   mid     bid     ask   entry    fee exitCost    edge action"
    )
    print("-" * 116)
    for row in rows:
        print(
            f"{row['outcome'][:24]:24s} "
            f"{row['ensembleProbability']:7.2%} "
            f"{row['hitCount']:4d} "
            f"{_fmt_float(row['marketMidpoint']):>7s} "
            f"{_fmt_float(row['bestBid']):>7s} "
            f"{_fmt_float(row['bestAsk']):>7s} "
            f"{_fmt_float(row['executableEntryCost']):>7s} "
            f"{_fmt_float(row['fee']):>6s} "
            f"{_fmt_float(row['expectedExitCost']):>8s} "
            f"{_fmt_float(row['edge'], percent=True):>8s} "
            f"{row['recommendation']}"
        )
    if args.save:
        storage = WeatherStorage(_db_path(args), initialize=True)
        storage.save_distribution(distribution)
        group = storage.save_market_snapshots(market_buckets)
        storage.save_signal_snapshots(run_key=run.run_key, rows=rows, market_snapshot_group=group)
        print(f"已保存 SQLite: {args.db or DEFAULT_DB_PATH} run_key={run.run_key}")
    return 0


def market_command(args: argparse.Namespace) -> int:
    config = _load_config(args)
    city = get_city(args.city, config) if args.city else None
    engine = _build_engine(config, buy_edge_threshold=args.min_edge)
    unit = city.settlement_unit if city else args.unit
    if args.query and not args.slug and not args.condition_id:
        buckets = engine.market_client.discover_weather_market_buckets(
            query=args.query,
            default_unit=unit,
            kind=args.kind,
            target_date=args.date,
            refresh_clob_midpoints=args.refresh_clob,
            include_orderbooks=args.use_orderbook,
        )
    else:
        buckets = engine.market_client.get_market_buckets(
            query=args.query,
            slug=args.slug,
            condition_id=args.condition_id,
            default_unit=unit,
            refresh_clob_midpoints=args.refresh_clob,
            include_orderbooks=args.use_orderbook,
        )
    for bucket in buckets:
        lower = "-inf" if bucket.bucket.lower is None else f"{bucket.bucket.lower:.2f}"
        upper = "inf" if bucket.bucket.upper is None else f"{bucket.bucket.upper:.2f}"
        orderbook = bucket.orderbook
        print(
            f"{bucket.outcome:24s} price={bucket.price:.4f} "
            f"bid={_fmt_float(orderbook.best_bid if orderbook else None)} "
            f"ask={_fmt_float(orderbook.best_ask if orderbook else None)} "
            f"spread={_fmt_float(orderbook.spread if orderbook else None)} "
            f"range={lower}..{upper} {bucket.bucket.unit} token={bucket.token_id or '-'}"
        )
    return 0


def signal_command(args: argparse.Namespace) -> int:
    config = _load_config(args)
    city = get_city(args.city, config)
    query = args.query
    if not query and not args.slug and not args.condition_id:
        temp_word = "high" if args.kind == "high" else "low"
        query = f"{city.name} {args.date.isoformat()} {temp_word} temperature"

    engine = _build_engine(config, buy_edge_threshold=args.min_edge)
    report = engine.build_report(
        city=city,
        target_date=args.date,
        kind=args.kind,
        market_query=query,
        market_slug=args.slug,
        condition_id=args.condition_id,
        models=_parse_models(args.models),
        refresh_clob_midpoints=args.refresh_clob,
        use_orderbook=args.use_orderbook,
        fee_rate=args.fee_rate,
        maker_fee_rate=args.maker_fee_rate,
        min_cashout_ratio=args.min_cashout_ratio,
        max_entry_slippage=args.max_entry_slippage,
        max_exit_slippage=args.max_exit_slippage,
        depth_usage_fraction=args.depth_usage_fraction,
    )
    print(f"市场: {report.market_question}")
    print(
        f"预测: mean={report.distribution.mean:.2f} "
        f"std={report.distribution.std:.2f} {report.distribution.unit} "
        f"models={','.join(report.ensemble.source_models)}"
    )
    print(
        "outcome                  prob     mid     bid     ask   rawEdge execEdge "
        "limitBid buyVWAP sellVWAP cashout   stake maxSell action"
    )
    print("-" * 144)
    risk = RiskConfig(
        bankroll=args.bankroll,
        kelly_mode=args.kelly,
        min_edge=args.min_edge,
        max_trade_fraction=args.max_trade_fraction,
        max_daily_fraction=args.max_daily_fraction,
        max_market_fraction=args.max_market_fraction,
        max_city_fraction=args.max_city_fraction,
        fee_rate=args.fee_rate,
        min_cashout_ratio=args.min_cashout_ratio,
        max_entry_slippage=args.max_entry_slippage,
        max_exit_slippage=args.max_exit_slippage,
        depth_usage_fraction=args.depth_usage_fraction,
    )
    sizer = PositionSizer()
    best_trade: tuple[BucketSignal, float, float, float] | None = None
    for signal in report.signals:
        position = sizer.size_yes(signal, risk)
        stake_text = f"{position.stake:.2f}" if position.should_trade else "-"
        liquidity = signal.liquidity
        buy_estimate = position.entry_fill or (liquidity.buy_estimate if liquidity else None)
        sell_estimate = position.exit_fill or (liquidity.sell_estimate if liquidity else None)
        cashout_ratio = position.cashout_ratio
        if cashout_ratio is None and liquidity is not None:
            cashout_ratio = liquidity.cashout_ratio
        max_sell = liquidity.max_sell_value if liquidity is not None else None
        print(
            f"{signal.market_bucket.outcome[:24]:24s} "
            f"{signal.probability:6.2%} "
            f"{signal.market_price:7.4f} "
            f"{_fmt_float(liquidity.best_bid if liquidity else None):>7s} "
            f"{_fmt_float(liquidity.best_ask if liquidity else None):>7s} "
            f"{_fmt_float(signal.raw_edge, percent=True):>8s} "
            f"{_fmt_float(signal.executable_edge, percent=True):>8s} "
            f"{_fmt_float(signal.limit_bid):>8s} "
            f"{_fmt_float(buy_estimate.vwap if buy_estimate else None):>7s} "
            f"{_fmt_float(sell_estimate.vwap if sell_estimate else None):>8s} "
            f"{_fmt_float(cashout_ratio, percent=True):>7s} "
            f"{stake_text:>7s} "
            f"{_fmt_float(max_sell, precision=2):>7s} "
            f"{signal.recommendation:24s}"
        )
        if position.should_trade and (
            best_trade is None or position.stake > best_trade[1]
        ):
            trade_price = (
                position.entry_fill.effective_price
                if position.entry_fill and position.entry_fill.effective_price is not None
                else signal.market_price
            )
            best_trade = (signal, position.stake, position.shares, trade_price)

    if args.record_best and best_trade:
        signal, stake, shares, trade_price = best_trade
        ledger = TradeLedger(Path(args.ledger).expanduser() if args.ledger else TradeLedger().path)
        timestamp = datetime.now(timezone.utc)
        record = TradeRecord(
            trade_id=make_trade_id(
                city_id=city.city_id,
                target_date=args.date,
                kind=args.kind,
                outcome=signal.market_bucket.outcome,
                timestamp=timestamp,
            ),
            timestamp=timestamp,
            city_id=city.city_id,
            target_date=args.date,
            kind=args.kind,
            market_slug=signal.market_bucket.slug,
            outcome=signal.market_bucket.outcome,
            token_id=signal.market_bucket.token_id,
            side="YES",
            price=trade_price,
            probability=signal.probability,
            edge=signal.executable_edge if signal.executable_edge is not None else signal.edge,
            stake=stake,
            shares=shares,
            notes="recorded from CLI signal command",
        )
        path = ledger.append(record)
        print(f"已记录交易日志: {path}")
    return 0


def _portfolio_inputs(args: argparse.Namespace) -> tuple[Portfolio, tuple]:
    positions = load_positions_file(Path(args.positions).expanduser(), default_unit=args.unit)
    if args.markets:
        market_buckets = load_market_buckets_file(
            Path(args.markets).expanduser(),
            default_unit=args.unit,
        )
    elif getattr(args, "query", None) or getattr(args, "slug", None) or getattr(args, "condition_id", None):
        config = _load_config(args)
        market_client = _build_market_client(config)
        if getattr(args, "query", None) and not (
            getattr(args, "slug", None) or getattr(args, "condition_id", None)
        ):
            market_buckets = market_client.discover_weather_market_buckets(
                query=args.query,
                default_unit=args.unit,
                kind=getattr(args, "kind", None),
                target_date=getattr(args, "date", None),
                refresh_clob_midpoints=getattr(args, "no_orderbook", False),
                include_orderbooks=not getattr(args, "no_orderbook", False),
            )
        else:
            market_buckets = market_client.get_market_buckets(
                query=getattr(args, "query", None),
                slug=getattr(args, "slug", None),
                condition_id=getattr(args, "condition_id", None),
                default_unit=args.unit,
                refresh_clob_midpoints=getattr(args, "no_orderbook", False),
                include_orderbooks=not getattr(args, "no_orderbook", False),
            )
    else:
        market_buckets = market_buckets_from_positions(positions, default_unit=args.unit)
    return Portfolio(positions=positions), market_buckets


def _print_scenarios(scenarios: tuple[PortfolioScenario, ...]) -> None:
    print("scenario                 prob      payoff   totalCost    netPnL covered")
    print("-" * 74)
    for scenario in scenarios:
        print(
            f"{scenario.outcome[:24]:24s} "
            f"{scenario.probability:7.2%} "
            f"{scenario.payoff:10.2f} "
            f"{scenario.total_cost:10.2f} "
            f"{scenario.net_pnl:9.2f} "
            f"{'yes' if scenario.is_covered else 'tail'}"
        )


def _print_lock_result(result: PortfolioLockResult) -> None:
    print(f"action={result.recommendation}")
    print(f"ask_sum={result.ask_sum:.4f}")
    print(f"bid_sum={result.bid_sum:.4f}")
    print(f"midpoint_sum={result.midpoint_sum:.4f}")
    print(f"is_overround={result.is_overround}")
    print(f"hedge_cost={result.hedge_cost:.2f}")
    print(f"lock_profit={result.lock_profit:.2f}")
    print(f"worst_case_pnl={result.worst_case_pnl:.2f}")
    print(f"covered_worst_case_pnl={result.covered_worst_case_pnl:.2f}")
    print(f"covered_probability={result.covered_probability:.2%}")
    print(f"uncovered_tail_probability={result.uncovered_tail_probability:.2%}")
    print(f"is_true_arbitrage={result.is_true_arbitrage}")
    print(f"is_tail_risk_lock={result.is_tail_risk_lock}")
    if result.notes:
        print("notes=" + " | ".join(result.notes))
    if result.hedge_legs:
        print("hedge legs:")
        for leg in result.hedge_legs:
            print(
                f"  {leg.action} {leg.outcome}: shares={leg.shares:.2f} "
                f"limit={leg.price:.4f} cost={leg.total_cost:.2f}"
            )
    _print_scenarios(result.scenarios)


def portfolio_command(args: argparse.Namespace) -> int:
    portfolio, market_buckets = _portfolio_inputs(args)
    valuations = value_portfolio(portfolio, market_buckets, fee_rate=args.fee_rate)
    overround = orderbook_overround(market_buckets)
    print(f"positions={len(portfolio.positions)}")
    print(f"current_cost={portfolio.total_cost:.2f}")
    print(f"mark_value={portfolio_mark_value(valuations):.2f}")
    print(f"liquidation_value={portfolio_liquidation_value(valuations):.2f}")
    print(f"cashout_ratio={_fmt_float(portfolio_cashout_ratio(valuations), percent=True)}")
    print(f"ask_sum={float(overround['ask_sum']):.4f}")
    print(f"bid_sum={float(overround['bid_sum']):.4f}")
    print(f"midpoint_sum={float(overround['midpoint_sum']):.4f}")
    print(f"is_overround={bool(overround['is_overround'])}")
    print("position valuations:")
    for valuation in valuations:
        print(
            f"  {valuation.position.outcome}: shares={valuation.position.shares:.2f} "
            f"cost={valuation.position.total_cost:.2f} "
            f"mark={valuation.mark_value:.2f} liquidation={valuation.liquidation_value:.2f} "
            f"cashout={_fmt_float(valuation.cashout_ratio, percent=True)}"
        )
        market_bucket = next(
            (
                item
                for item in market_buckets
                if item.bucket.canonical_key == valuation.position.bucket.canonical_key
            ),
            None,
        )
        plan = generate_passive_exit_plan(
            valuation.position,
            market_bucket,
            fee_rate=args.fee_rate,
            min_cashout_ratio=args.min_cashout_ratio,
        )
        print(f"    exit_action={plan.action}")
        for leg in plan.ladder:
            print(
                f"    ladder {leg.label}: shares={leg.shares:.2f} "
                f"net={leg.net_value:.2f}"
            )
        print(f"    retain_to_resolution={plan.retained_shares:.2f}")
    scenarios = calculate_hedge_lock(
        portfolio,
        market_buckets,
        probabilities=probabilities_from_market_buckets(market_buckets),
        target_profit=args.target_profit,
        tail_probability_cutoff=args.tail_probability_cutoff,
        max_tail_probability=args.max_tail_probability,
        fee_rate=args.fee_rate,
    ).scenarios
    _print_scenarios(scenarios)
    return 0


def hedge_command(args: argparse.Namespace) -> int:
    portfolio, market_buckets = _portfolio_inputs(args)
    covered_outcomes = (
        tuple(item.strip() for item in args.covered_outcomes.split(",") if item.strip())
        if args.covered_outcomes
        else None
    )
    result = calculate_hedge_lock(
        portfolio,
        market_buckets,
        probabilities=probabilities_from_market_buckets(market_buckets),
        target_profit=args.target_profit,
        covered_outcomes=covered_outcomes,
        tail_probability_cutoff=args.tail_probability_cutoff,
        max_tail_probability=args.max_tail_probability,
        fee_rate=args.fee_rate,
    )
    _print_lock_result(result)
    return 0


def size_command(args: argparse.Namespace) -> int:
    from weather_quant.buckets import parse_temperature_bucket
    from weather_quant.models import MarketBucket

    bucket = parse_temperature_bucket(args.outcome, default_unit=args.unit)
    market_bucket = MarketBucket(
        market_id="manual",
        question="manual sizing",
        slug=None,
        condition_id=None,
        outcome=args.outcome,
        price=args.price,
        bucket=bucket,
    )
    signal = BucketSignal(
        market_bucket=market_bucket,
        probability=args.probability,
        market_price=args.price,
        edge=args.probability - args.price,
        expected_value=args.probability - args.price,
        fair_price=args.probability,
        recommendation="BUY_YES",
    )
    recommendation = PositionSizer().size_yes(
        signal,
        RiskConfig(
            bankroll=args.bankroll,
            kelly_mode=args.kelly,
            min_edge=args.min_edge,
            max_trade_fraction=args.max_trade_fraction,
        ),
    )
    print(f"should_trade={recommendation.should_trade}")
    print(f"reason={recommendation.reason}")
    print(f"full_kelly={recommendation.full_kelly_fraction:.2%}")
    print(f"scaled_kelly={recommendation.scaled_kelly_fraction:.2%}")
    print(f"stake={recommendation.stake:.2f}")
    print(f"shares={recommendation.shares:.2f}")
    print(f"max_loss={recommendation.max_loss:.2f}")
    print(f"potential_profit={recommendation.potential_profit:.2f}")
    return 0


def performance_command(args: argparse.Namespace) -> int:
    ledger = TradeLedger(Path(args.ledger).expanduser())
    stats = ledger.stats()
    print(f"trades={stats.total_trades}")
    print(f"settled={stats.settled_trades}")
    print(f"win_rate={stats.win_rate:.2%}")
    print(f"total_stake={stats.total_stake:.2f}")
    print(f"total_pnl={stats.total_pnl:.2f}")
    print(f"roi={stats.roi:.2%}")
    print(f"max_drawdown={stats.max_drawdown:.2f}")
    print(f"average_edge={stats.average_edge:.2%}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weather",
        description="Polymarket weather prediction, edge, and risk toolkit.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--config", help="JSON/YAML config path")
        subparser.add_argument("--min-edge", type=float, default=0.03)

    forecast = subparsers.add_parser("forecast", help="Fetch ensemble forecast")
    add_common(forecast)
    forecast.add_argument("--city", required=True)
    forecast.add_argument("--date", required=True, type=_parse_date)
    forecast.add_argument("--kind", default="high", type=_kind)
    forecast.add_argument("--models", help="Comma-separated model list")
    forecast.set_defaults(func=forecast_command)

    db = subparsers.add_parser("db", help="Manage local SQLite research database")
    db_subparsers = db.add_subparsers(dest="db_command", required=True)
    db_init = db_subparsers.add_parser("init", help="Initialize SQLite schema")
    db_init.add_argument("--db", default=str(DEFAULT_DB_PATH))
    db_init.set_defaults(func=db_init_command)
    db_runs = db_subparsers.add_parser("runs", help="List recent ensemble runs")
    db_runs.add_argument("--db", default=str(DEFAULT_DB_PATH))
    db_runs.add_argument("--limit", type=int, default=20)
    db_runs.set_defaults(func=db_runs_command)
    db_probabilities = db_subparsers.add_parser(
        "probabilities",
        help="List recent bucket probability snapshots",
    )
    db_probabilities.add_argument("--db", default=str(DEFAULT_DB_PATH))
    db_probabilities.add_argument("--limit", type=int, default=50)
    db_probabilities.set_defaults(func=db_probabilities_command)

    ensemble = subparsers.add_parser("ensemble", help="Fetch member-hit ensemble distribution")
    add_common(ensemble)
    ensemble.add_argument("--city", required=True)
    ensemble.add_argument("--date", required=True, type=_parse_date)
    ensemble.add_argument("--kind", default="high", type=_kind)
    ensemble.add_argument("--model", default="gfs_seamless")
    ensemble.add_argument("--forecast-days", type=int)
    ensemble.add_argument("--save", action="store_true")
    ensemble.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ensemble.set_defaults(func=ensemble_command)

    ensemble_signal = subparsers.add_parser(
        "ensemble-signal",
        help="Build empirical ensemble-vs-market edge report",
    )
    add_common(ensemble_signal)
    ensemble_signal.add_argument("--city", required=True)
    ensemble_signal.add_argument("--date", required=True, type=_parse_date)
    ensemble_signal.add_argument("--kind", default="high", type=_kind)
    ensemble_signal.add_argument("--model", default="gfs_seamless")
    ensemble_signal.add_argument("--forecast-days", type=int)
    ensemble_signal.add_argument("--markets", help="CSV/JSON current market/orderbook snapshot")
    ensemble_signal.add_argument("--query")
    ensemble_signal.add_argument("--slug")
    ensemble_signal.add_argument("--condition-id")
    ensemble_signal.add_argument("--use-orderbook", action="store_true")
    ensemble_signal.add_argument("--fee-rate", type=float, default=DEFAULT_TAKER_FEE_RATE)
    ensemble_signal.add_argument("--save", action="store_true")
    ensemble_signal.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ensemble_signal.set_defaults(func=ensemble_signal_command)

    market = subparsers.add_parser("market", help="Inspect Polymarket buckets")
    add_common(market)
    market.add_argument("--city")
    market.add_argument("--query")
    market.add_argument("--slug")
    market.add_argument("--condition-id")
    market.add_argument("--date", type=_parse_date)
    market.add_argument("--kind", default="high", type=_kind)
    market.add_argument("--unit", default="F", choices=["F", "C"])
    market.add_argument("--refresh-clob", action="store_true")
    market.add_argument("--use-orderbook", action="store_true")
    market.set_defaults(func=market_command)

    signal = subparsers.add_parser("signal", help="Build forecast-vs-market edge report")
    add_common(signal)
    signal.add_argument("--city", required=True)
    signal.add_argument("--date", required=True, type=_parse_date)
    signal.add_argument("--kind", default="high", type=_kind)
    signal.add_argument("--query")
    signal.add_argument("--slug")
    signal.add_argument("--condition-id")
    signal.add_argument("--models")
    signal.add_argument("--refresh-clob", action="store_true")
    signal.add_argument("--use-orderbook", action="store_true")
    signal.add_argument("--fee-rate", type=float, default=DEFAULT_TAKER_FEE_RATE)
    signal.add_argument("--maker-fee-rate", type=float, default=DEFAULT_MAKER_FEE_RATE)
    signal.add_argument("--min-cashout-ratio", type=float, default=0.50)
    signal.add_argument("--max-entry-slippage", type=float, default=0.10)
    signal.add_argument("--max-exit-slippage", type=float, default=0.20)
    signal.add_argument("--depth-usage-fraction", type=float, default=0.25)
    signal.add_argument("--bankroll", type=float, default=1_000.0)
    signal.add_argument("--kelly", default="half", choices=["full", "half", "quarter"])
    signal.add_argument("--max-trade-fraction", type=float, default=0.03)
    signal.add_argument("--max-daily-fraction", type=float, default=0.12)
    signal.add_argument("--max-market-fraction", type=float, default=0.06)
    signal.add_argument("--max-city-fraction", type=float, default=0.08)
    signal.add_argument("--record-best", action="store_true")
    signal.add_argument("--ledger")
    signal.set_defaults(func=signal_command)

    portfolio = subparsers.add_parser("portfolio", help="Evaluate a weather bucket portfolio")
    portfolio.add_argument("--config", help="JSON/YAML config path")
    portfolio.add_argument("--positions", required=True, help="CSV/JSON current YES positions")
    portfolio.add_argument("--markets", help="CSV/JSON current market/orderbook snapshot")
    portfolio.add_argument("--query", help="Polymarket market search query")
    portfolio.add_argument("--slug", help="Polymarket market slug")
    portfolio.add_argument("--condition-id", help="Polymarket condition id")
    portfolio.add_argument("--date", type=_parse_date)
    portfolio.add_argument("--kind", default="high", type=_kind)
    portfolio.add_argument("--no-orderbook", action="store_true", help="Use Polymarket midpoint prices without CLOB depth")
    portfolio.add_argument("--unit", default="F", choices=["F", "C"])
    portfolio.add_argument("--fee-rate", type=float, default=DEFAULT_TAKER_FEE_RATE)
    portfolio.add_argument("--min-cashout-ratio", type=float, default=0.50)
    portfolio.add_argument("--target-profit", type=float, default=0.0)
    portfolio.add_argument("--tail-probability-cutoff", type=float, default=0.0)
    portfolio.add_argument("--max-tail-probability", type=float, default=0.05)
    portfolio.set_defaults(func=portfolio_command)

    hedge = subparsers.add_parser("hedge", help="Calculate hedge lock or tail-risk lock")
    hedge.add_argument("--config", help="JSON/YAML config path")
    hedge.add_argument("--positions", required=True, help="CSV/JSON current YES positions")
    hedge.add_argument("--markets", help="CSV/JSON current market/orderbook snapshot")
    hedge.add_argument("--query", help="Polymarket market search query")
    hedge.add_argument("--slug", help="Polymarket market slug")
    hedge.add_argument("--condition-id", help="Polymarket condition id")
    hedge.add_argument("--date", type=_parse_date)
    hedge.add_argument("--kind", default="high", type=_kind)
    hedge.add_argument("--no-orderbook", action="store_true", help="Use Polymarket midpoint prices without CLOB depth")
    hedge.add_argument("--unit", default="F", choices=["F", "C"])
    hedge.add_argument("--fee-rate", type=float, default=DEFAULT_TAKER_FEE_RATE)
    hedge.add_argument("--target-profit", type=float, default=0.0)
    hedge.add_argument("--covered-outcomes", help="Comma-separated outcomes to cover as core")
    hedge.add_argument("--tail-probability-cutoff", type=float, default=0.0)
    hedge.add_argument("--max-tail-probability", type=float, default=0.05)
    hedge.set_defaults(func=hedge_command)

    size = subparsers.add_parser("size", help="Size a manual YES position")
    size.add_argument("--outcome", required=True)
    size.add_argument("--unit", default="F", choices=["F", "C"])
    size.add_argument("--probability", required=True, type=float)
    size.add_argument("--price", required=True, type=float)
    size.add_argument("--bankroll", required=True, type=float)
    size.add_argument("--kelly", default="half", choices=["full", "half", "quarter"])
    size.add_argument("--min-edge", type=float, default=0.03)
    size.add_argument("--max-trade-fraction", type=float, default=0.03)
    size.set_defaults(func=size_command)

    backtest = subparsers.add_parser("backtest", help="Backtest historical CSV signals")
    backtest.add_argument("--csv")
    backtest.add_argument("--orderbook-snapshots")
    backtest.add_argument("--bankroll", type=float, default=1_000.0)
    backtest.add_argument("--kelly", default="half", choices=["full", "half", "quarter"])
    backtest.add_argument("--min-edge", type=float, default=0.03)
    backtest.add_argument("--fee-rate", type=float, default=DEFAULT_TAKER_FEE_RATE)
    backtest.add_argument("--min-cashout-ratio", type=float, default=0.50)
    backtest.add_argument("--max-entry-slippage", type=float, default=0.10)
    backtest.add_argument("--max-exit-slippage", type=float, default=0.20)
    backtest.add_argument("--depth-usage-fraction", type=float, default=0.25)
    backtest.add_argument("--passive-entry-fill", action="store_true")
    backtest.add_argument("--passive-exit-ladder", action="store_true")
    backtest.add_argument("--hedge-lock", action="store_true")
    backtest.add_argument("--no-hold-to-resolution", action="store_true")

    def run_backtest_from_cli(args: argparse.Namespace) -> int:
        argv = [
            "--bankroll",
            str(args.bankroll),
            "--kelly",
            args.kelly,
            "--min-edge",
            str(args.min_edge),
            "--fee-rate",
            str(args.fee_rate),
            "--min-cashout-ratio",
            str(args.min_cashout_ratio),
            "--max-entry-slippage",
            str(args.max_entry_slippage),
            "--max-exit-slippage",
            str(args.max_exit_slippage),
            "--depth-usage-fraction",
            str(args.depth_usage_fraction),
        ]
        if args.orderbook_snapshots:
            argv.extend(["--orderbook-snapshots", args.orderbook_snapshots])
        if args.passive_entry_fill:
            argv.append("--passive-entry-fill")
        if args.passive_exit_ladder:
            argv.append("--passive-exit-ladder")
        if args.hedge_lock:
            argv.append("--hedge-lock")
        if args.no_hold_to_resolution:
            argv.append("--no-hold-to-resolution")
        if args.csv:
            argv.extend(["--csv", args.csv])
        return backtest_main(argv)

    backtest.set_defaults(func=run_backtest_from_cli)

    performance = subparsers.add_parser("performance", help="Summarize local trade ledger")
    performance.add_argument("--ledger", default=str(TradeLedger().path))
    performance.set_defaults(func=performance_command)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
