"""LLM explanation helpers for locally computed weather-market results."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from weather_quant.env import load_dotenv
from weather_quant.runtime_logs import log_external_api_failure


class LlmSummaryError(RuntimeError):
    """Raised when an LLM summary cannot be generated."""


SYSTEM_PROMPT = """你是天气预测市场量化助手。你只解释输入的结构化结果，不重新计算数值，不编造缺失字段，不调用外部信息。
请始终用中文回复，称呼用户为“姜楠”。输出用于辅助理解，不构成投资建议。
请用简洁 Markdown，最多 6 条要点，重点覆盖：结论、机会、主要风险、下一步检查项。"""


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 6)
    return None


def _pick(row: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in keys:
        if key not in row:
            continue
        value = row[key]
        number = _number(value)
        result[key] = number if number is not None else value
    return result


def _sort_number(row: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        value = _number(row.get(key))
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _top_rows(
    rows: Any,
    *,
    keys: tuple[str, ...],
    limit: int,
    sort_keys: tuple[str, ...] = (),
    reverse: bool = True,
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    items = [row for row in rows if isinstance(row, Mapping)]
    if sort_keys:
        items = sorted(items, key=lambda row: _sort_number(row, *sort_keys), reverse=reverse)
    return [_pick(row, keys) for row in items[:limit]]


def _compact_ensemble_signal(result: Mapping[str, Any]) -> dict[str, Any]:
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else {}
    return {
        "type": "ensemble-signal",
        "summary": _pick(
            summary,
            (
                "cityName",
                "targetDate",
                "kind",
                "unit",
                "model",
                "memberCount",
                "unmatchedCount",
                "empiricalMean",
                "empiricalStd",
                "p10",
                "p50",
                "p90",
                "marketBucketCount",
            ),
        ),
        "highestProbabilityBuckets": _top_rows(
            result.get("probabilities"),
            keys=("bucketLabel", "bucketKey", "probability", "hitCount", "totalMembers"),
            limit=8,
            sort_keys=("probability",),
        ),
        "topSignals": _top_rows(
            result.get("signals"),
            keys=(
                "outcome",
                "recommendation",
                "reason",
                "signalScore",
                "ensembleProbability",
                "marketImpliedProbability",
                "rawEdge",
                "edge",
                "executableEntryCost",
                "bestBid",
                "bestAsk",
                "spread",
                "askDepth",
            ),
            limit=8,
            sort_keys=("signalScore", "edge", "rawEdge"),
        ),
    }


def _compact_portfolio(result: Mapping[str, Any]) -> dict[str, Any]:
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else {}
    scenarios = result.get("scenarios") if isinstance(result.get("scenarios"), list) else []
    scenario_rows = [row for row in scenarios if isinstance(row, Mapping)]
    worst_scenarios = sorted(scenario_rows, key=lambda row: _sort_number(row, "netPnl"))[:6]
    likely_scenarios = sorted(
        scenario_rows,
        key=lambda row: _sort_number(row, "probability"),
        reverse=True,
    )[:6]
    return {
        "type": "portfolio",
        "summary": _pick(
            summary,
            (
                "marketSource",
                "marketCount",
                "positions",
                "currentCost",
                "markValue",
                "liquidationValue",
                "cashoutRatio",
                "coveredProbability",
                "uncoveredTailProbability",
                "worstCasePnl",
                "coveredWorstCasePnl",
                "hedgeCost",
                "lockProfit",
                "askSum",
                "bidSum",
                "midpointSum",
                "isTrueArbitrage",
                "isTailRiskLock",
                "recommendation",
                "notes",
            ),
        ),
        "valuations": _top_rows(
            result.get("valuations"),
            keys=(
                "outcome",
                "shares",
                "cost",
                "markPrice",
                "bestBid",
                "bestAsk",
                "markValue",
                "liquidationValue",
                "cashoutRatio",
                "unrealizedMarkPnl",
                "executablePnl",
            ),
            limit=12,
        ),
        "hedgeLegs": _top_rows(
            result.get("hedgeLegs"),
            keys=("outcome", "action", "shares", "price", "cost"),
            limit=12,
        ),
        "worstScenarios": [
            _pick(row, ("outcome", "probability", "payoff", "totalCost", "netPnl", "isCovered"))
            for row in worst_scenarios
        ],
        "likelyScenarios": [
            _pick(row, ("outcome", "probability", "payoff", "totalCost", "netPnl", "isCovered"))
            for row in likely_scenarios
        ],
        "exits": _top_rows(
            result.get("exits"),
            keys=("outcome", "action", "retainedShares", "warning"),
            limit=12,
        ),
    }


def compact_result_for_llm(kind: str, result: Mapping[str, Any]) -> dict[str, Any]:
    normalized = str(kind or "").strip().lower()
    if normalized == "ensemble-signal":
        return _compact_ensemble_signal(result)
    if normalized == "portfolio":
        return _compact_portfolio(result)
    raise LlmSummaryError("kind must be ensemble-signal or portfolio.")


def extract_response_text(data: Mapping[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    output = data.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, Mapping):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        if parts:
            return "\n\n".join(parts)
    raise LlmSummaryError("OpenAI response did not contain output text.")


class OpenAILlmSummaryClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        load_dotenv()
        self.api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        self.model = model if model is not None else os.environ.get("OPENAI_MODEL") or "gpt-4.1-mini"
        resolved_base_url = (
            base_url
            if base_url is not None
            else os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        )
        self.base_url = resolved_base_url.rstrip("/")
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else float(os.environ.get("OPENAI_TIMEOUT_SECONDS") or 30.0)
        )

    def summarize(self, *, kind: str, result: Mapping[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise LlmSummaryError("未配置 OPENAI_API_KEY，无法生成 AI 解读。")
        context = compact_result_for_llm(kind, result)
        request_payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "请解释下面这份本地量化结果。不要改写或重算任何数值。\n\n"
                        f"{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}"
                    ),
                },
            ],
            "max_output_tokens": 700,
        }
        data = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/responses",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            error = LlmSummaryError(f"OpenAI Responses API failed with {exc.code}: {detail}")
            self._log_failure(action="responses_request", error=error)
            raise error from exc
        except urllib.error.URLError as exc:
            error = LlmSummaryError(f"OpenAI Responses API request failed: {exc.reason}")
            self._log_failure(action="responses_request", error=error)
            raise error from exc
        try:
            response_payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            error = LlmSummaryError("OpenAI Responses API returned invalid JSON.")
            self._log_failure(action="responses_parse", error=error)
            raise error from exc
        if not isinstance(response_payload, Mapping):
            error = LlmSummaryError("OpenAI Responses API returned an unexpected payload.")
            self._log_failure(action="responses_parse", error=error)
            raise error
        try:
            summary = extract_response_text(response_payload)
        except LlmSummaryError as exc:
            self._log_failure(action="responses_parse", error=exc)
            raise
        return {
            "kind": kind,
            "model": self.model,
            "summary": summary,
        }

    def _log_failure(self, *, action: str, error: BaseException) -> None:
        log_external_api_failure(
            provider="openai",
            action=action,
            endpoint="/responses",
            details={
                "model": self.model,
                "baseUrl": self.base_url,
            },
            error=error,
        )
