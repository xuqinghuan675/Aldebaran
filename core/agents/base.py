# Inspired by TradingAgents v0.2.4 (Apache-2.0)
"""Agent 通用 LLM 客户端 — DeepSeek API 调用 + caching 友好结构。

caching 设计：
  message 结构 = [system, user]
  user = stable_prefix + '\n\n---\n\n' + variable_suffix

  stable_prefix 字节级稳定 → DeepSeek 自动命中 prompt cache（前缀匹配）
  variable_suffix 每次变化（当日数据、价格）

注意：DeepSeek 的 prompt cache 是自动的（基于 prefix hash），不需要传任何 flag。
我们要做的是把不变的内容放在前面，变化的放在后面。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

_DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'
_MODEL_FLASH = 'deepseek-v4-flash'
_MODEL_PRO = 'deepseek-flash'  # legacy constant name; deep-analysis path now uses V4.1 Flash
_TIMEOUT_FLASH = 30
_TIMEOUT_PRO = 90

# stable / variable 之间的分隔标记（不会影响语义，只作可读性）
_SECTION_SEPARATOR = '\n\n---\n\n'

logger = logging.getLogger(__name__)

_API_BLOCK_UNTIL = 0.0
_API_BLOCK_ERROR = ''

def build_cached_user_message(stable_prefix: str, variable_suffix: str) -> str:
    """组合稳定前缀和变化后缀为单个 user message。

    DeepSeek 会自动对 [system + 稳定前缀] 部分做 prefix cache。
    """
    prefix = (stable_prefix or '').strip()
    suffix = (variable_suffix or '').strip()
    if not prefix:
        return suffix
    if not suffix:
        return prefix
    return prefix + _SECTION_SEPARATOR + suffix


def call_llm(
    system_prompt: str,
    user_message: str,
    api_key: str,
    *,
    model: str = _MODEL_FLASH,
    max_tokens: int = 600,
    temperature: float = 0.3,
    response_format: dict | None = None,
    retries: int = 1,
    timeout: int | None = None,
) -> tuple[str, str, dict | None]:
    """调用 DeepSeek，返回 (content, reasoning_content, usage)。

    Args:
        system_prompt: 系统提示（应字节级稳定以命中 caching）
        user_message: 用户消息（建议用 build_cached_user_message 组合）
        api_key: DeepSeek API key
        model: deepseek-v4-flash (legacy alias) | deepseek-flash (V4.1 Flash)
        max_tokens: 输出上限
        temperature: 采样温度；V4.1 Flash thinking 下该参数会被 API 忽略
        response_format: 传 {'type': 'json_object'} 强制 JSON
        retries: 失败重试次数（不含首次）

    Returns:
        (content, reasoning_content, usage_dict)
        失败时 content 为空字符串，usage 为 None
    """
    global _API_BLOCK_UNTIL, _API_BLOCK_ERROR
    timeout = timeout or (_TIMEOUT_PRO if model == _MODEL_PRO else _TIMEOUT_FLASH)
    if _API_BLOCK_UNTIL > time.time():
        return '', '', {
            'http_status': 402,
            'error': _API_BLOCK_ERROR or 'DeepSeek API 余额不足（HTTP 402，请充值）',
        }

    payload: dict[str, Any] = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_message},
        ],
        'max_tokens': max_tokens,
        'temperature': temperature,
    }
    if model == _MODEL_PRO:
        payload['reasoning_effort'] = 'high'
        payload['thinking'] = {'type': 'enabled'}
    if response_format:
        payload['response_format'] = response_format

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }

    last_err = ''
    last_reasoning = ''
    last_usage: dict | None = None
    rate_limit_hits = 0   # 429 单独计数，最多额外退避重试 2 次（批量并发兜底）
    attempt = 0
    max_attempts = retries + 1
    while attempt < max_attempts:
        try:
            r = requests.post(_DEEPSEEK_URL, headers=headers, json=payload, timeout=timeout)
            if r.status_code == 429:
                # 限流兜底：更长退避后重试，不计入常规 retries，最多 2 次
                rate_limit_hits += 1
                last_err = 'rate_limited(429)'
                if rate_limit_hits <= 2:
                    wait = 5 * rate_limit_hits
                    logger.warning(f'LLM 限流(429)，{wait}s 后重试（第 {rate_limit_hits} 次）')
                    time.sleep(wait)
                    continue
                logger.warning('LLM 限流(429) 已达兜底重试上限，放弃本次调用')
                return '', '', None
            if r.status_code == 402:
                last_err = 'DeepSeek API 余额不足（HTTP 402，请充值后重试）'
                _API_BLOCK_ERROR = last_err
                _API_BLOCK_UNTIL = time.time() + 30
                logger.error(last_err)
                return '', '', {
                    'http_status': 402,
                    'error': last_err,
                }
            r.raise_for_status()
            data = r.json()
            choice = data['choices'][0]
            msg = choice['message']
            content = msg.get('content', '') or ''
            reasoning = msg.get('reasoning_content', '') or ''
            usage = dict(data.get('usage') or {})
            finish_reason = str(choice.get('finish_reason') or '')
            usage['finish_reason'] = finish_reason
            last_reasoning = reasoning
            last_usage = usage

            reasoning_tokens = (
                (usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
            )
            if finish_reason == 'length':
                last_err = (
                    f'truncated(finish_reason=length, reasoning_tokens={reasoning_tokens})'
                )
                if attempt < retries:
                    current_max = int(payload.get('max_tokens') or max_tokens)
                    payload['max_tokens'] = min(max(current_max * 2, current_max + 512), 32768)
                    if response_format:
                        payload.pop('reasoning_effort', None)
                        payload['thinking'] = {'type': 'disabled'}
                    logger.warning(
                        'LLM 输出被截断，扩大 max_tokens 后重试: %s -> %s '
                        '(reasoning_tokens=%s%s)',
                        current_max, payload['max_tokens'], reasoning_tokens,
                        '，JSON 重试切换 non-thinking' if response_format else '',
                    )
            elif not content.strip():
                last_err = (
                    f'empty_content(finish_reason={finish_reason or "unknown"}, '
                    f'reasoning_tokens={reasoning_tokens})'
                )
                if attempt < retries:
                    if response_format:
                        payload.pop('reasoning_effort', None)
                        payload['thinking'] = {'type': 'disabled'}
                    logger.warning(
                        'LLM 返回空 content，自动重试 '
                        '(finish_reason=%s, reasoning_tokens=%s%s)',
                        finish_reason or 'unknown', reasoning_tokens,
                        '，JSON 重试切换 non-thinking' if response_format else '',
                    )
            else:
                return content, reasoning, usage
        except requests.Timeout:
            last_err = f'timeout(>{timeout}s)'
        except requests.HTTPError as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            last_err = f'HTTP {status}: {e}' if status else str(e)
            if status in (400, 401, 402, 422):
                return '', last_reasoning, {
                    'http_status': status,
                    'error': last_err,
                }
        except Exception as e:
            last_err = str(e)
        if attempt < retries:
            time.sleep(2 ** attempt)
            logger.warning(f'LLM call retry {attempt + 1}: {last_err}')
        attempt += 1
    logger.warning(f'LLM call failed: {last_err}')
    return '', last_reasoning, last_usage


def cache_hit_rate(usage: dict | None) -> float:
    """从 usage 信息解析 prompt cache 命中率（DeepSeek 字段 prompt_cache_hit_tokens）。

    返回 0.0-1.0 之间的数值；usage 为 None 或字段缺失返回 0.0。
    """
    if not usage:
        return 0.0
    hit = usage.get('prompt_cache_hit_tokens', 0) or 0
    total = usage.get('prompt_tokens', 0) or 0
    if total <= 0:
        return 0.0
    return min(1.0, hit / total)


# ---------- 便捷封装 ----------

# ─── Agent JSON 工具 ─────────────────────────────────────────────────

# 单 Agent 输出 schema（嵌入各 agent _SYSTEM 末尾）
AGENT_JSON_SCHEMA = '''\
以严格 JSON 输出，不含额外文字：
{
  "analyst": "<角色名>",
  "stance": "bullish|bearish|neutral",
  "summary": "<≤80字中文摘要>",
  "evidence": [{"name":"<信号名>","value":"<值或简述>","weight":"high|medium|low"}],
  "counter_evidence": [{"name":"<反方向信号>","value":"<值或简述>","weight":"high|medium|low"}],
  "intel_refs": ["<引用情报/新闻标题≤30字>"],
  "confidence": <1-10整数>
}'''

STRICT_SOURCE_GROUNDING = '''\
资料边界硬约束：
- 只能基于本次输入中明确提供的资料、数字、新闻标题、日期、Agent 结论和确定性规则分析。
- 禁止补充输入未提供的历史事实、首次/连续/多日、行业排名、订单、政策、机构行为、估值修复、主力确认等结论。
- 输入写明“单日/仅1日/样本不足/数据缺失”时，必须保留该边界；不得把单日写成近3/5/10日趋势，不得把模型解析失败写成真实数据缺失。
- 需要但输入没有的资料，必须写“资料未提供/无法验证”，并降低置信度或维持观察。
- 财务“首次/历史亏损/持续盈利/同比环比”等历史口径，只有输入直接给出时才允许引用。
'''


def parse_agent_json(content: str, analyst_name: str) -> dict:
    """从 agent LLM 响应解析结构化 JSON；失败返回兜底 dict。"""
    import json, re

    def _norm_stance(v) -> str:
        s = str(v or '').strip().lower()
        mapping = {
            'bullish': 'bullish', 'bearish': 'bearish', 'neutral': 'neutral',
            '看多': 'bullish', '偏多': 'bullish', '多': 'bullish',
            '看空': 'bearish', '偏空': 'bearish', '空': 'bearish',
            '中性': 'neutral', '观望': 'neutral', '震荡': 'neutral',
        }
        return mapping.get(s, 'neutral')

    def _normalize(d: dict) -> dict:
        d.setdefault('analyst', analyst_name)
        if 'signals' in d and 'evidence' not in d:
            d['evidence'] = d.pop('signals')
        d['stance'] = _norm_stance(d.get('stance'))
        d.setdefault('evidence', [])
        d.setdefault('counter_evidence', [])
        d.setdefault('intel_refs', [])
        d.setdefault('confidence', 0)
        # confidence=0 与非 neutral stance 矛盾，强制归 neutral
        if d.get('confidence', 0) <= 0 and d.get('stance') != 'neutral':
            d['stance'] = 'neutral'
        return d

    if content:
        # 尝试直接解析
        try:
            d = json.loads(content.strip())
            if isinstance(d, dict) and 'stance' in d:
                return _normalize(d)
        except (json.JSONDecodeError, ValueError):
            pass
        # 尝试从内容中提取完整 JSON 对象
        m = re.search(r'\{.*\}', content, re.DOTALL)
        if m:
            try:
                d = json.loads(m.group())
                if isinstance(d, dict) and 'stance' in d:
                    return _normalize(d)
            except (json.JSONDecodeError, ValueError):
                pass
        # 截断 JSON 兜底：用 regex 逐字段提取已解析部分
        partial: dict = {'analyst': analyst_name, 'stance': 'neutral',
                         'evidence': [], 'counter_evidence': [], 
                         'intel_refs': [], 'confidence': 0}
        found_any = False
        for key in ('analyst', 'stance', 'summary', 'confidence'):
            pat = rf'"{key}"\s*:\s*"([^"]*)"' if key != 'confidence' else rf'"{key}"\s*:\s*(\d+)'
            km = re.search(pat, content)
            if km:
                found_any = True
                partial[key] = int(km.group(1)) if key == 'confidence' else km.group(1)
        if found_any:
            partial['stance'] = _norm_stance(partial.get('stance'))
            return _normalize(partial)
    return {
        'analyst': analyst_name, 'stance': 'neutral',
        'summary': '（数据获取失败，请重新预测）',
        'evidence': [], 'counter_evidence': [], 'intel_refs': [], 'confidence': 0,
    }


_STANCE_LABEL = {'bullish': '看多▲', 'bearish': '看空▼', 'neutral': '中性→'}


def agent_dict_to_text(d) -> str:
    """将 agent 结果 dict（或旧版 str）序列化为可读文本，供 F/G 层 prompt 使用。"""
    if isinstance(d, str):
        return d
    if not isinstance(d, dict):
        return str(d)
    stance = _STANCE_LABEL.get(d.get('stance', ''), d.get('stance', ''))
    lines = [f"[{d.get('analyst', '?')}] {stance}  置信 {d.get('confidence', 0)}/10"]
    if d.get('summary'):
        lines.append(f"摘要：{d['summary']}")
    strategy_report = d.get('strategy_report')
    if isinstance(strategy_report, dict) and strategy_report:
        strategy_bits = []
        if strategy_report.get('selected_strategy'):
            strategy_bits.append(f"选择策略={strategy_report.get('selected_strategy')}")
        if strategy_report.get('stage'):
            strategy_bits.append(f"阶段={strategy_report.get('stage')}")
        if 'buy_ready' in strategy_report:
            strategy_bits.append(f"可买={bool(strategy_report.get('buy_ready'))}")
        if strategy_report.get('reason'):
            strategy_bits.append(f"理由={strategy_report.get('reason')}")
        if strategy_bits:
            lines.append(f"策略报告：{'；'.join(strategy_bits)}")
        triggers = strategy_report.get('trigger_conditions') or []
        missing = strategy_report.get('missing_conditions') or []
        if triggers:
            lines.append(f"  触发条件：{'；'.join(map(str, triggers[:3]))}")
        if missing:
            lines.append(f"  缺口：{'；'.join(map(str, missing[:3]))}")
    evidence = d.get('evidence') or d.get('signals') or []
    for sig in evidence[:5]:
        lines.append(f"  • {sig.get('name','')}: {sig.get('value','')} ({sig.get('weight','')})")
    counter = d.get('counter_evidence') or []
    if counter:
        lines.append('  反方证据:')
        for sig in counter[:2]:
            lines.append(f"  ✗ {sig.get('name','')}: {sig.get('value','')}")
    if d.get('intel_refs'):
        lines.append(f"情报引用：{'  |  '.join((d['intel_refs'] or [])[:3])}")
    return '\n'.join(lines)


def agent_dict_to_html(d, ref_ids: dict | None = None) -> str:
    """将 agent dict 转为 HTML 字符串，intel_refs 可设为可点击锚点。

    ref_ids: {ref_title: event_id}，有匹配时生成 href="intel_<event_id>"。
    """
    import html as _html
    if ref_ids is None:
        ref_ids = {}
    if isinstance(d, str) and d.strip().startswith('{'):
        import json as _json
        try:
            parsed = _json.loads(d)
            if isinstance(parsed, dict) and 'stance' in parsed:
                d = parsed
        except (ValueError, _json.JSONDecodeError):
            pass
    if not isinstance(d, dict):
        return _html.escape(str(d) if d else '')
    stance = _STANCE_LABEL.get(d.get('stance', ''), d.get('stance', ''))
    analyst = _html.escape(str(d.get('analyst', '?')))
    conf = d.get('confidence', 0)
    parts = [f'<b style="color:#7ec8e3;">[{analyst}]</b> {_html.escape(stance)}  置信 {conf}/10']
    if d.get('summary'):
        parts.append(f'<span style="color:#8899aa;">摘要：</span>{_html.escape(d["summary"])}')
    strategy_report = d.get('strategy_report')
    if isinstance(strategy_report, dict) and strategy_report:
        strategy_bits = []
        if strategy_report.get('selected_strategy'):
            strategy_bits.append(f"选择策略={strategy_report.get('selected_strategy')}")
        if strategy_report.get('stage'):
            strategy_bits.append(f"阶段={strategy_report.get('stage')}")
        if 'buy_ready' in strategy_report:
            strategy_bits.append(f"可买={bool(strategy_report.get('buy_ready'))}")
        if strategy_report.get('reason'):
            strategy_bits.append(f"理由={strategy_report.get('reason')}")
        if strategy_bits:
            parts.append(
                '<span style="color:#8899aa;">策略报告：</span>'
                + _html.escape('；'.join(strategy_bits))
            )
        triggers = strategy_report.get('trigger_conditions') or []
        missing = strategy_report.get('missing_conditions') or []
        for item in triggers[:3]:
            parts.append(f'&nbsp;&nbsp;&bull; 触发条件：{_html.escape(str(item))}')
        for item in missing[:3]:
            parts.append(f'&nbsp;&nbsp;&bull; 缺口：{_html.escape(str(item))}')
    _w = {'high': '#f0b429', 'medium': '#aabbcc', 'low': '#667788'}
    _wl = {'high': '[高]', 'medium': '[中]', 'low': '[低]'}
    evidence = d.get('evidence') or d.get('signals') or []
    for sig in evidence[:5]:
        n = _html.escape(str(sig.get('name', '')))
        v = _html.escape(str(sig.get('value', '')))
        w = sig.get('weight', '')
        wc = _w.get(w, '#aabbcc')
        wt = _wl.get(w, '')
        parts.append(f'&nbsp;&nbsp;&bull; {n}: {v} <span style="color:{wc};">{wt}</span>')
    counter = d.get('counter_evidence') or []
    if counter:
        parts.append('<span style="color:#8899aa;">反方证据：</span>')
        for sig in counter[:2]:
            n = _html.escape(str(sig.get('name', '')))
            v = _html.escape(str(sig.get('value', '')))
            parts.append(f'&nbsp;&nbsp;&bull; <span style="color:#cc8888;">✗ {n}: {v}</span>')
    refs = (d.get('intel_refs') or [])[:5]
    if refs:
        parts.append('<span style="color:#8899aa;">情报引用：</span>')
        for ref in refs:
            ref_e = _html.escape(str(ref))
            eid = ref_ids.get(ref, '')
            if eid:
                parts.append(
                    f'&nbsp;&nbsp;&bull; <a href="intel_{eid}" '
                    f'style="color:#4a9eff;text-decoration:none;">{ref_e}</a>'
                )
            else:
                parts.append(f'&nbsp;&nbsp;&bull; {ref_e}')
    return '<br>'.join(parts)


def call_flash(system: str, user: str, api_key: str, **kw) -> str:
    """便捷调用 flash，仅返回 content 字符串（失败返回空）。"""
    content, _, _ = call_llm(system, user, api_key, model=_MODEL_FLASH, **kw)
    return content


def call_pro(system: str, user: str, api_key: str, **kw) -> tuple[str, str]:
    """兼容旧调用名：使用 V4.1 Flash thinking，返回 (content, reasoning)。"""
    content, reasoning, _ = call_llm(system, user, api_key, model=_MODEL_PRO, **kw)
    return content, reasoning
