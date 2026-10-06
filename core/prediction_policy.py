from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping


FORCED_HORIZON_DAYS = 3


def normalize_horizon_days(_: Any = None) -> int:
    """All current-trading-route predictions are evaluated on a fixed 3-trading-day window."""
    return FORCED_HORIZON_DAYS


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, Mapping):
        for key in (
            "date",
            "datetime",
            "time",
            "timestamp",
            "created_at",
            "published_at",
            "publish_time",
            "发布时间",
            "日期",
        ):
            parsed = _parse_date(value.get(key))
            if parsed is not None:
                return parsed
        return None
    for attr in (
        "date",
        "datetime",
        "time",
        "timestamp",
        "created_at",
        "published_at",
        "publish_time",
    ):
        if hasattr(value, attr):
            parsed = _parse_date(getattr(value, attr))
            if parsed is not None:
                return parsed
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        head = raw[:19].replace("/", "-")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(head[: len(fmt)], fmt).date()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _fresh_count(items: Any, today: date) -> int:
    if not isinstance(items, list):
        return 0
    count = 0
    for item in items:
        parsed = _parse_date(item)
        if parsed is None:
            continue
        age = (today - parsed).days
        if 0 <= age <= FORCED_HORIZON_DAYS:
            count += 1
    return count


def build_intel_coverage(stock_news: Any, intel_events: Any, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    fresh_news_count = _fresh_count(stock_news, today)
    fresh_event_count = _fresh_count(intel_events, today)
    stock_news_count = len(stock_news) if isinstance(stock_news, list) else 0
    event_count = len(intel_events) if isinstance(intel_events, list) else 0
    fresh_total = fresh_news_count + fresh_event_count

    if fresh_total >= 2 or (fresh_news_count >= 1 and fresh_event_count >= 1):
        level = "fresh"
    elif fresh_total == 1 or stock_news_count + event_count > 0:
        level = "thin"
    else:
        level = "missing"

    return {
        "coverage_level": level,
        "horizon_days": FORCED_HORIZON_DAYS,
        "fresh_news_count": fresh_news_count,
        "fresh_event_count": fresh_event_count,
        "fresh_stock_news_count": fresh_news_count,
        "fresh_intel_event_count": fresh_event_count,
        "stock_news_count": stock_news_count,
        "event_count": event_count,
    }


def _report_text(agent_reports: Mapping[str, Any] | None, *keys: str) -> str:
    if not isinstance(agent_reports, Mapping):
        return ""
    parts: list[str] = []
    for key in keys:
        value = agent_reports.get(key)
        if value is None:
            continue
        parts.append(str(value))
    return "\n".join(parts)


def _stance(text: str) -> str:
    lower = text.lower()
    bearish_markers = ("bearish", "看空", "偏空", "下跌", "减仓", "流出", "风险", "弱", "谨慎", "回避")
    bullish_markers = ("bullish", "看多", "偏多", "上涨", "加仓", "流入", "机会", "强", "突破", "买入")
    bearish = any(token in lower for token in bearish_markers)
    bullish = any(token in lower for token in bullish_markers)
    if bearish and not bullish:
        return "bearish"
    if bullish and not bearish:
        return "bullish"
    if bearish and bullish:
        return "mixed"
    return "neutral"


def _flow_days(context: Mapping[str, Any]) -> int | None:
    profile = context.get("flow_profile")
    if isinstance(profile, Mapping):
        value = profile.get("days") or profile.get("window_days")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _has_overheat(context: Mapping[str, Any]) -> bool:
    technical = context.get("technical_profile")
    flags: list[str] = []
    if isinstance(technical, Mapping):
        raw_flags = technical.get("risk_flags") or technical.get("tags") or []
        if isinstance(raw_flags, list):
            flags.extend(str(item) for item in raw_flags)
    raw_tags = context.get("behavior_tags") or []
    if isinstance(raw_tags, list):
        flags.extend(str(item) for item in raw_tags)
    joined = " ".join(flags)
    return any(token in joined for token in ("过热", "超买", "高换手", "加速", "一致性拥挤"))


def _mainline_fit(context: Mapping[str, Any]) -> bool:
    profile = context.get("mainline_fit")
    if not isinstance(profile, Mapping):
        return False
    return bool(profile.get("fit") or profile.get("major_mainline") or profile.get("is_mainline"))


def build_agent_calibration_context(
    context: Mapping[str, Any] | None,
    stock_news: Any,
    intel_events: Any,
    agent_reports: Mapping[str, Any] | None,
    today: date | None = None,
) -> dict[str, Any]:
    context = context or {}
    coverage = build_intel_coverage(stock_news, intel_events, today=today)
    coverage_level = coverage["coverage_level"]
    flow_days = _flow_days(context)
    overheat = _has_overheat(context)

    event_stance = _stance(_report_text(agent_reports, "news", "event", "events"))
    fundflow_stance = _stance(_report_text(agent_reports, "fundflow", "flow", "capital"))
    technical_stance = _stance(_report_text(agent_reports, "technical", "tech"))
    fundamental_stance = _stance(_report_text(agent_reports, "company", "fundamental"))
    market_stance = _stance(_report_text(agent_reports, "macro", "market", "index"))

    directives: list[str] = [
        f"预测周期固定为 {FORCED_HORIZON_DAYS} 个交易日，禁止按情绪自动扩到 5 日。",
    ]

    if coverage_level == "missing":
        event_adjustment = {
            "weight": "lowest",
            "treatment": "missing_not_bearish",
            "stance": event_stance,
        }
        directives.append("事件中性=情报缺口，不等于利空，不能单独压低评级。")
    elif coverage_level == "thin":
        event_adjustment = {
            "weight": "low",
            "treatment": "thin_intel_low_confidence",
            "stance": event_stance,
        }
        directives.append("事件情报偏薄，只能降低置信度，不能当成强趋势证据。")
    else:
        event_adjustment = {
            "weight": "normal",
            "treatment": "fresh_event_signal",
            "stance": event_stance,
        }

    short_flow = flow_days is not None and flow_days < FORCED_HORIZON_DAYS
    contrarian_noise = (
        short_flow
        and fundflow_stance == "bearish"
        and technical_stance in {"bullish", "mixed"}
        and _mainline_fit(context)
    )
    if short_flow:
        fundflow_adjustment = {
            "weight": "low",
            "treatment": "short_window_bearish_low_weight"
            if fundflow_stance == "bearish"
            else "short_window_low_confidence",
            "stance": fundflow_stance,
            "window_days": flow_days,
            "contrarian_noise": contrarian_noise,
        }
        directives.append("资金流样本少于3个交易日时，bearish 只按短窗噪声处理。")
    else:
        fundflow_adjustment = {
            "weight": "normal",
            "treatment": "three_day_or_longer_flow",
            "stance": fundflow_stance,
            "window_days": flow_days,
            "contrarian_noise": False,
        }

    technical_adjustment = {
        "weight": "primary_direction",
        "treatment": "direction_signal_overheat_separate",
        "stance": technical_stance,
        "overheat": overheat,
    }
    directives.append("技术面负责三日方向，过热只进入仓位和止损，不直接否定趋势。")

    if fundamental_stance == "bearish" and overheat and coverage_level != "fresh":
        fundamental_adjustment = {
            "weight": "high_in_overheat_missing_intel",
            "treatment": "protective_downgrade",
            "stance": fundamental_stance,
        }
        directives.append("基本面偏空在技术过热且情报不足时，作为保护性降档触发器。")
    else:
        fundamental_adjustment = {
            "weight": "low_for_3day",
            "treatment": "background_quality_filter",
            "stance": fundamental_stance,
        }

    market_adjustment = {
        "weight": "background",
        "treatment": "context_only",
        "stance": market_stance,
        "can_trigger_buy": False,
    }
    directives.append("大盘/行业只能当背景，不能单独触发买入。")

    return {
        "horizon_days": FORCED_HORIZON_DAYS,
        "intel_coverage": coverage,
        "agent_adjustments": {
            "event": event_adjustment,
            "fundflow": fundflow_adjustment,
            "technical": technical_adjustment,
            "fundamental": fundamental_adjustment,
            "market": market_adjustment,
        },
        "directives": directives,
    }


def format_calibration_context(calibration_context: Mapping[str, Any] | None) -> str:
    if not calibration_context:
        return "Agent校准：无额外校准上下文。"
    coverage = calibration_context.get("intel_coverage") or {}
    if isinstance(coverage, Mapping):
        coverage_line = (
            f"情报覆盖={coverage.get('coverage_level', 'unknown')}，"
            f"新鲜新闻={coverage.get('fresh_news_count', 0)}，"
            f"新鲜事件={coverage.get('fresh_event_count', 0)}。"
        )
    else:
        coverage_line = "情报覆盖=unknown。"
    directives = calibration_context.get("directives") or []
    if not isinstance(directives, list):
        directives = [str(directives)]
    directive_text = "\n".join(f"- {item}" for item in directives if str(item).strip())
    adjustments = calibration_context.get("agent_adjustments") or {}
    adjustment_lines: list[str] = []
    if isinstance(adjustments, Mapping):
        for name in ("event", "fundflow", "technical", "fundamental", "market"):
            item = adjustments.get(name)
            if not isinstance(item, Mapping):
                continue
            adjustment_lines.append(
                f"- {name}: weight={item.get('weight')}, treatment={item.get('treatment')}, stance={item.get('stance')}"
            )
    return "\n".join(
        part
        for part in (
            "Agent校准：",
            coverage_line,
            directive_text,
            "\n".join(adjustment_lines),
        )
        if part
    )
