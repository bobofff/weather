# Polymarket Weather Quant

独立的 Polymarket 天气交易研究项目，包含：

- Python 量化核心：天气桶解析、订单簿、组合情景矩阵、被动建仓/退出、hedge lock、tail-risk lock、离线回测。
- Node/HTML 前端：`server.js` 使用 Node 原生 HTTP 托管静态页面，前端由 `frontend/index.html`、`frontend/styles.css`、`frontend/app.js` 组成，不再用 Python 拼页面。

## 目录

```text
src/weather_quant/       # Python 量化核心与 CLI
frontend/                # HTML/CSS/JS 前端
server.js                # 零依赖 Node 静态服务和组合评估 API
config/                  # 示例配置
tests/                   # 离线单元测试
```

## 安装

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
```

Node 前端没有第三方依赖，不需要 `npm install`。

## 启动前端

```bash
npm start
```

默认地址：

```text
前端：http://127.0.0.1:58888
后端：http://127.0.0.1:56666
```

第一步从城市下拉选择城市，例如慕尼黑、莫斯科、安卡拉或拉斯维加斯，并选择日期和最高温/最低温后点击“获取盘口”。页面会通过 `POST /api/markets` 调用 Polymarket `GET /events/keyset` 自动发现相关 event，再从 event 的 nested markets 解析 `conditionId`、`clobTokenIds` 和天气桶，并从 CLOB 拉订单簿，不需要 CSV，也不需要每天手动找 `conditionId`。返回结果会限定到一个盘口组，例如 `Shanghai + July 3 + highest temperature`；同一天的 lowest temperature 会作为另一个盘口组处理。`slug` / `condition id` / 关键词覆盖只作为精确定位兜底。后续组合评估才会用到持仓 CSV；市场快照 CSV 只是离线兜底。

组合页面展示 current cost、mark value、liquidation value、cashout ratio、被动退出 ladder、hedge legs、covered probability、tail risk 和 worst-case PnL。

AI 解读面板通过 `POST /api/llm-summary` 调用 OpenAI Responses API，只解释本地已经算好的 `Ensemble Signal` 或组合评估结果，不参与概率、edge、hedge、PnL 等核心计算。启用前需要配置：

```bash
cp .env.example .env
```

然后在 `.env` 里填写 `OPENAI_API_KEY`。服务启动时会自动加载 `.env`，也支持 `export OPENAI_API_KEY="sk-..."` 这种写法。修改 `.env` 后需要重启 `npm start` 进程。

## 下一步路线图

项目当前已经能获取温度预报、天气模型集合概率和 Polymarket 市场盘口。下一阶段重点是从“能看数据”升级到“能判断机会并复盘判断质量”：

1. 信号评分面板：把模型概率、市场隐含概率、可执行入场成本、fee、预期退出成本、edge、盘口 spread 和深度合成一张交易信号表。
2. 模型校准与结果复盘：保存当时模型、盘口、最终结算温度和命中桶，用来检查模型系统性偏差。
3. 仓位与风险约束：限制单市场投入、同城市同日期暴露、tail risk、worst-case PnL 和低流动性盘口。
4. 自动监控与提醒：当 edge、盘口价格或天气模型更新出现显著变化时提醒，但先不自动下单。
5. 交易执行闭环：稳定后再增加建议订单、手动确认下单、成交记录和后续自动化。

第一阶段已落到前端“信号评分”面板：`Ensemble Signal` 会输出评分、raw edge、可执行 edge、成本、spread、深度和动作建议。

注意：TCP 端口必须小于等于 `65535`，所以 `88888` 和 `66666` 不能被操作系统监听。这里使用最接近的合法端口 `58888` / `56666`。

端口可通过环境变量覆盖：

```bash
FRONTEND_PORT=58888 BACKEND_PORT=56666 npm start
```

## CLI

```bash
weather market --query "New York high temperature July 3" --use-orderbook
weather signal --city new-york --date 2026-07-03 --kind high --query "New York high temperature July 3" --use-orderbook
weather portfolio --positions data/weather_positions.csv --markets data/weather_market_snapshot.csv
weather portfolio --positions data/weather_positions.csv --slug new-york-high-temperature-july-3
weather hedge --positions data/weather_positions.csv --markets data/weather_market_snapshot.csv --tail-probability-cutoff 0.05
weather hedge --positions data/weather_positions.csv --query "New York high temperature July 3" --tail-probability-cutoff 0.05
weather backtest --orderbook-snapshots data/weather_orderbook_snapshots.csv --passive-entry-fill --passive-exit-ladder --hedge-lock
```

## Ensemble Member 概率

项目支持把天气概率从正态近似升级为集合预报 member 的经验分布。一个 Ensemble Run 是一次模型初始化，例如 00Z run；一个 Ensemble Member 是这个 run 里的一个扰动成员。默认不要简单混合多个 run：旧 run 会重复使用已经过时的信息，适合做趋势稳定性复盘，但实盘决策应以 latest run 为主。

经验概率的计算方式是：

```text
P(bucket) = 命中该温度桶的 ensemble member 数 / ensemble member 总数
```

每个 member 会先从 hourly `temperature_2m` 按城市/结算站 timezone 聚合成目标结算日的 high 或 low，再判断落入哪个 Polymarket 温度桶。输出会包含每个桶的 hit count、probability、unmatched count、empirical mean/std、p10/p50/p90，以及前端可画图的桶概率、member rug/scatter、CDF points 和 market implied probability 对比。

Open-Meteo ensemble 客户端走 `https://ensemble-api.open-meteo.com/v1/ensemble`，普通 forecast 仍走 `https://api.open-meteo.com/v1/forecast`。当前先支持 hourly `temperature_2m` 和 `gfs_seamless`、`ecmwf_ifs025`、`ecmwf_aifs025` 等 ensemble model 参数。测试全部使用 mock payload，不访问真实网络。

Web API 的 `/api/forecast`、`/api/ensemble` 和 `/api/ensemble-signal` 默认按 `city` 读取内置城市配置；如果 payload 同时提供 `latitude` 和 `longitude`，则会临时构造一个更具体的点位配置，并可选传 `timezone`、`unit`/`settlementUnit`、`settlementStation`、`stationId`、`forecastGranularity`、`elevation`、`cellSelection`。`cellSelection` 支持 `land`、`sea`、`nearest`。这些字段只影响天气模型请求；Polymarket 盘口发现仍使用 `marketQuery`、`marketSlug`、`conditionId` 或城市关键词。

城市配置也会保存到 SQLite 的 `weather_cities` 表。页面启动时会通过 `GET /api/cities` 初始化并读取城市列表，默认城市会自动写入表中；编辑后通过 `POST /api/cities` 保存。天气模型请求传 `useStoredCity: true` 时，会优先按表里的 `cityId` 读取经纬度、时区、单位、结算站、海拔和格点配置。

结算站坐标可以通过 `POST /api/station-lookup` 辅助解析。接口优先按 4 位 ICAO/METAR 站点 ID 查询 Aviation Weather Center station info；没有站点 ID 或未命中时，按结算站名称查询 Open-Meteo Geocoding，并返回可填入城市配置的经纬度、时区和海拔。

注意：Open-Meteo 返回的是指定模型在指定网格点的预报，不等于 Polymarket 最终结算源。实盘不要把单一模型当成真值；同一城市同一天可能出现 IFS 明显偏冷、AIFS/ICON/Meteo-France 更贴近盘口和常见天气聚合源的情况。前端默认展示多模型确定性预报，ensemble 默认使用 `ecmwf_aifs025`，`Ensemble Signal` 会尝试按城市/日期自动发现 Polymarket 桶并画出盘口隐含概率对比。

CLI 示例：

```bash
weather db init
weather ensemble --city new-york --date 2026-07-05 --kind high --model ecmwf_aifs025 --save
weather ensemble-signal --city new-york --date 2026-07-05 --kind high --model ecmwf_aifs025 --markets data/weather_market_snapshot.csv --save
weather db runs
weather db probabilities
```

`ensemble-signal` 使用：

```text
edge = ensemble_bucket_probability - executable_entry_cost - fee - expected_exit_cost
```

这里的 ensemble probability 仍然是概率分布，不是确定性结算结果。原有 overround、tail risk、cashout ratio、liquidation value、passive exit 等组合风险逻辑仍需一起看。

## SQLite 复盘库

默认数据库路径是 `data/weather.db`。初始化命令：

```bash
weather db init
```

SQLite 表不使用外键约束。表和字段的中文说明保存在 `schema_comments`：

- `weather_ensemble_runs`：一次 provider/model/city/date/kind 的 ensemble run。
- `weather_ensemble_members`：每个 member 聚合后的 daily high/low、命中桶和原始小时序列。
- `weather_bucket_probabilities`：每个桶的 hit count、概率、分位数和 unmatched count。
- `weather_market_snapshots`：Polymarket 桶、价格、bid/ask、midpoint、spread、overround 快照。
- `weather_signal_snapshots`：ensemble 概率与市场可执行价格结合后的 edge 和 recommendation。

这些数据用于复盘、校准、回测和检查 station-level 偏差。天气市场尤其要关注结算站：城市级格点预报和官方气象站/METAR 之间可能有系统差异，1°F 的误差足以改变相邻温度桶的结算结果。

## 风险说明

这里的组合锁利不是默认意义上的无风险套利。全桶 ask sum 大于 1 时是 overround；剔除低概率尾部桶只能称为 tail-risk lock，必须同时看 uncovered tail probability 和全局 worst-case PnL。页面 mark value 不是真实已实现收益，真实收益取决于 bids 深度、成交队列、fee、最终结算和 Polymarket resolution source。没有显式提供 `probability` 时，组合覆盖概率会用市场 mark/mid price 近似，不能替代独立天气模型概率。

实盘前必须核对每个市场的结算源、气象站、METAR/官方观测来源和最新 fee 规则。1°F 的结算站偏差可能让相邻桶组合从锁利变成整组亏损。

## 测试

```bash
.venv/bin/python -m compileall src
.venv/bin/python -m unittest
npm run check
```

<!-- TimescaleDB -->
