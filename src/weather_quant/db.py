"""SQLite schema for weather ensemble research data."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from weather_quant.paths import PROJECT_ROOT


DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "weather.db"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  object_type TEXT NOT NULL,
  object_name TEXT NOT NULL,
  column_name TEXT,
  comment TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weather_ensemble_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_key TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  run_time TEXT,
  initialization_time TEXT,
  city_id TEXT NOT NULL,
  target_date TEXT NOT NULL,
  kind TEXT NOT NULL,
  latitude REAL NOT NULL,
  longitude REAL NOT NULL,
  timezone TEXT NOT NULL,
  settlement_station TEXT,
  station_id TEXT,
  metar_source TEXT,
  forecast_granularity TEXT NOT NULL,
  member_count INTEGER NOT NULL,
  fetched_at TEXT NOT NULL,
  raw_payload_json TEXT,
  payload_hash TEXT
);

CREATE TABLE IF NOT EXISTS weather_ensemble_members (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_key TEXT NOT NULL,
  member_id TEXT NOT NULL,
  target_date TEXT NOT NULL,
  kind TEXT NOT NULL,
  daily_value REAL NOT NULL,
  unit TEXT NOT NULL,
  bucket_label TEXT,
  bucket_key TEXT,
  raw_hourly_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weather_bucket_probabilities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_key TEXT NOT NULL,
  bucket_label TEXT NOT NULL,
  bucket_key TEXT NOT NULL,
  hit_count INTEGER NOT NULL,
  probability REAL NOT NULL,
  total_members INTEGER NOT NULL,
  unmatched_count INTEGER NOT NULL,
  empirical_mean REAL,
  empirical_std REAL,
  p10 REAL,
  p50 REAL,
  p90 REAL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weather_market_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_snapshot_group TEXT NOT NULL,
  market_slug TEXT,
  condition_id TEXT,
  outcome TEXT NOT NULL,
  token_id TEXT,
  bucket_label TEXT NOT NULL,
  bucket_key TEXT NOT NULL,
  price REAL,
  best_bid REAL,
  best_ask REAL,
  midpoint REAL,
  spread REAL,
  ask_sum REAL,
  bid_sum REAL,
  midpoint_sum REAL,
  is_overround INTEGER NOT NULL,
  fetched_at TEXT NOT NULL,
  raw_payload_json TEXT
);

CREATE TABLE IF NOT EXISTS weather_signal_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_key TEXT NOT NULL,
  market_snapshot_group TEXT,
  outcome TEXT NOT NULL,
  bucket_key TEXT NOT NULL,
  ensemble_probability REAL NOT NULL,
  market_midpoint REAL,
  best_bid REAL,
  best_ask REAL,
  executable_entry_cost REAL,
  fee REAL,
  expected_exit_cost REAL,
  edge REAL NOT NULL,
  recommendation TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


SCHEMA_COMMENTS: tuple[tuple[str, str, str | None, str], ...] = (
    ("table", "schema_comments", None, "SQLite 对象中文说明表"),
    ("column", "schema_comments", "id", "自增主键"),
    ("column", "schema_comments", "object_type", "对象类型，table 或 column"),
    ("column", "schema_comments", "object_name", "表名或对象名"),
    ("column", "schema_comments", "column_name", "字段名，表说明时为空"),
    ("column", "schema_comments", "comment", "中文说明文本"),
    ("table", "weather_ensemble_runs", None, "一次天气集合预报初始化运行"),
    ("column", "weather_ensemble_runs", "id", "自增主键"),
    ("column", "weather_ensemble_runs", "run_key", "运行文本键，用于关联 member/probability/signal"),
    ("column", "weather_ensemble_runs", "provider", "数据提供方，例如 open-meteo"),
    ("column", "weather_ensemble_runs", "model", "集合预报模型"),
    ("column", "weather_ensemble_runs", "run_time", "模型运行时间"),
    ("column", "weather_ensemble_runs", "initialization_time", "模型初始化时间"),
    ("column", "weather_ensemble_runs", "city_id", "城市配置 ID"),
    ("column", "weather_ensemble_runs", "target_date", "目标结算日期"),
    ("column", "weather_ensemble_runs", "kind", "温度类型 high 或 low"),
    ("column", "weather_ensemble_runs", "latitude", "请求纬度"),
    ("column", "weather_ensemble_runs", "longitude", "请求经度"),
    ("column", "weather_ensemble_runs", "timezone", "聚合使用的时区"),
    ("column", "weather_ensemble_runs", "settlement_station", "市场结算站说明"),
    ("column", "weather_ensemble_runs", "station_id", "结算站代码"),
    ("column", "weather_ensemble_runs", "metar_source", "站点观测来源"),
    ("column", "weather_ensemble_runs", "forecast_granularity", "预报粒度，city 或 station"),
    ("column", "weather_ensemble_runs", "member_count", "可用 ensemble member 数量"),
    ("column", "weather_ensemble_runs", "fetched_at", "拉取时间"),
    ("column", "weather_ensemble_runs", "raw_payload_json", "原始响应 JSON"),
    ("column", "weather_ensemble_runs", "payload_hash", "原始响应哈希"),
    ("table", "weather_ensemble_members", None, "集合预报 member 的日高温/低温结果"),
    ("column", "weather_ensemble_members", "id", "自增主键"),
    ("column", "weather_ensemble_members", "run_key", "所属运行文本键"),
    ("column", "weather_ensemble_members", "member_id", "member 标识"),
    ("column", "weather_ensemble_members", "target_date", "目标结算日期"),
    ("column", "weather_ensemble_members", "kind", "温度类型 high 或 low"),
    ("column", "weather_ensemble_members", "daily_value", "聚合后的日高温或日低温"),
    ("column", "weather_ensemble_members", "unit", "温度单位"),
    ("column", "weather_ensemble_members", "bucket_label", "命中的温度桶标签"),
    ("column", "weather_ensemble_members", "bucket_key", "命中的温度桶规范键"),
    ("column", "weather_ensemble_members", "raw_hourly_json", "该 member 当日小时数据 JSON"),
    ("column", "weather_ensemble_members", "created_at", "写入时间"),
    ("table", "weather_bucket_probabilities", None, "member 命中次数形成的桶概率"),
    ("column", "weather_bucket_probabilities", "id", "自增主键"),
    ("column", "weather_bucket_probabilities", "run_key", "所属运行文本键"),
    ("column", "weather_bucket_probabilities", "bucket_label", "温度桶标签"),
    ("column", "weather_bucket_probabilities", "bucket_key", "温度桶规范键"),
    ("column", "weather_bucket_probabilities", "hit_count", "命中 member 数量"),
    ("column", "weather_bucket_probabilities", "probability", "命中概率"),
    ("column", "weather_bucket_probabilities", "total_members", "总 member 数量"),
    ("column", "weather_bucket_probabilities", "unmatched_count", "未命中任何桶的 member 数量"),
    ("column", "weather_bucket_probabilities", "empirical_mean", "member 经验均值"),
    ("column", "weather_bucket_probabilities", "empirical_std", "member 经验标准差"),
    ("column", "weather_bucket_probabilities", "p10", "member 经验 10 分位"),
    ("column", "weather_bucket_probabilities", "p50", "member 经验 50 分位"),
    ("column", "weather_bucket_probabilities", "p90", "member 经验 90 分位"),
    ("column", "weather_bucket_probabilities", "created_at", "写入时间"),
    ("table", "weather_market_snapshots", None, "Polymarket 温度桶市场快照"),
    ("column", "weather_market_snapshots", "id", "自增主键"),
    ("column", "weather_market_snapshots", "market_snapshot_group", "同一次市场快照分组键"),
    ("column", "weather_market_snapshots", "market_slug", "Polymarket slug"),
    ("column", "weather_market_snapshots", "condition_id", "Polymarket condition id"),
    ("column", "weather_market_snapshots", "outcome", "市场 outcome 文本"),
    ("column", "weather_market_snapshots", "token_id", "CLOB token id"),
    ("column", "weather_market_snapshots", "bucket_label", "温度桶标签"),
    ("column", "weather_market_snapshots", "bucket_key", "温度桶规范键"),
    ("column", "weather_market_snapshots", "price", "市场价格"),
    ("column", "weather_market_snapshots", "best_bid", "最佳买价"),
    ("column", "weather_market_snapshots", "best_ask", "最佳卖价"),
    ("column", "weather_market_snapshots", "midpoint", "盘口中点"),
    ("column", "weather_market_snapshots", "spread", "买卖价差"),
    ("column", "weather_market_snapshots", "ask_sum", "同组 ask 概率和"),
    ("column", "weather_market_snapshots", "bid_sum", "同组 bid 概率和"),
    ("column", "weather_market_snapshots", "midpoint_sum", "同组 midpoint 概率和"),
    ("column", "weather_market_snapshots", "is_overround", "是否存在 overround"),
    ("column", "weather_market_snapshots", "fetched_at", "快照时间"),
    ("column", "weather_market_snapshots", "raw_payload_json", "原始市场数据 JSON"),
    ("table", "weather_signal_snapshots", None, "ensemble 概率与市场价格形成的信号快照"),
    ("column", "weather_signal_snapshots", "id", "自增主键"),
    ("column", "weather_signal_snapshots", "run_key", "所属运行文本键"),
    ("column", "weather_signal_snapshots", "market_snapshot_group", "对应市场快照分组键"),
    ("column", "weather_signal_snapshots", "outcome", "市场 outcome 文本"),
    ("column", "weather_signal_snapshots", "bucket_key", "温度桶规范键"),
    ("column", "weather_signal_snapshots", "ensemble_probability", "ensemble member 命中概率"),
    ("column", "weather_signal_snapshots", "market_midpoint", "市场中点价格"),
    ("column", "weather_signal_snapshots", "best_bid", "最佳买价"),
    ("column", "weather_signal_snapshots", "best_ask", "最佳卖价"),
    ("column", "weather_signal_snapshots", "executable_entry_cost", "可执行入场成本"),
    ("column", "weather_signal_snapshots", "fee", "单位合约估算费用"),
    ("column", "weather_signal_snapshots", "expected_exit_cost", "预期退出成本"),
    ("column", "weather_signal_snapshots", "edge", "扣除成本后的 edge"),
    ("column", "weather_signal_snapshots", "recommendation", "交易建议"),
    ("column", "weather_signal_snapshots", "created_at", "写入时间"),
)


def connect_database(path: Path | str | None = None) -> sqlite3.Connection:
    database_path = Path(path).expanduser() if path else DEFAULT_DB_PATH
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection


def init_database(path: Path | str | None = None) -> Path:
    database_path = Path(path).expanduser() if path else DEFAULT_DB_PATH
    with connect_database(database_path) as connection:
        connection.executescript(SCHEMA_SQL)
        connection.execute("DELETE FROM schema_comments")
        connection.executemany(
            """
            INSERT INTO schema_comments (object_type, object_name, column_name, comment)
            VALUES (?, ?, ?, ?)
            """,
            SCHEMA_COMMENTS,
        )
        connection.commit()
    return database_path
