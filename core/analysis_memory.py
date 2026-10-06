"""分析记忆层 — Aldebaran v3.0 让胜率随样本累积提升的核心机制。

每只股票一个 markdown 归档：data/analysis_memory/{code}.md

每次 AI 分析完成后调用 archive_prediction()，把"预测 + 推理摘要"写入。
追踪任务结算时回写实际结果和精度评分。

下次分析同一只股票时，retrieve_recent() 返回最近 k 条片段，
inject_into_prompt() 把它们注入 G 层 prompt，让 AI 记得自己说过什么。

设计原则：
  - 纯字符串匹配 + 时间倒序，不引入 embedding 库（保打包体积）
  - markdown 易读，用户可手动查看 / 编辑 / 删除
  - 每条 ≤ 500 字（带截断）防 prompt 膨胀
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# 归档目录：~/.aldebaran/data/analysis_memory/（用户可写位置，打包安全）
from core.paths import ANALYSIS_MEMORY_DIR as _MEM_DIR  # noqa: E402
_LOCK = threading.Lock()

# 每条片段最大字符数（注入 prompt 时截断）
_SNIPPET_MAX_CHARS = 500
# 默认检索最近 k 条
_DEFAULT_K = 3


def _ensure_dir() -> None:
    try:
        _MEM_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _safe_code(code: str) -> str:
    """清理代码作为文件名，保留字母数字 + 中文 + 下划线，限长 30。"""
    return re.sub(r'[^0-9a-zA-Z_\u4e00-\u9fff]', '', str(code or ''))[:30] or 'unknown'


def _file_for(code: str) -> Path:
    return _MEM_DIR / f'{_safe_code(code)}.md'


# ---------- 写入 ----------

def archive_prediction(prediction: dict[str, Any], outcome: dict[str, Any] | None = None) -> bool:
    """归档一次预测。

    prediction 期望字段（与 core.predictor 输出对齐）：
      code, name, direction, confidence, entry_ref, target_pct, stop_pct,
      horizon_days, invalidation, reasoning, created_at (可选)

    outcome 可选，结算后回写时传入：
      result (win/loss/timeout/invalid), actual_pct, precision_score, settled_at

    返回 True 表示写入成功。
    """
    code = str(prediction.get('code', '')).strip()
    if not code:
        return False

    _ensure_dir()
    block = _format_block(prediction, outcome)
    fp = _file_for(code)

    try:
        with _LOCK:
            existing = fp.read_text(encoding='utf-8') if fp.exists() else ''
            fp.write_text(block + '\n' + existing, encoding='utf-8')
        return True
    except Exception:
        return False


def update_outcome(code: str, prediction_id: str, outcome: dict[str, Any]) -> bool:
    """追踪任务结算时回写。

    通过 prediction_id 定位旧块，把【结果待回填】替换为实际结果。
    若找不到旧块就当作新块追加（保证不丢数据）。
    """
    code = str(code or '').strip()
    pid = str(prediction_id or '').strip()
    if not code or not pid:
        return False

    fp = _file_for(code)
    if not fp.exists():
        # 直接当新块写入
        return archive_prediction(
            {'code': code, 'name': outcome.get('name', code), 'prediction_id': pid},
            outcome,
        )

    try:
        with _LOCK:
            text = fp.read_text(encoding='utf-8')
            # 找到 prediction_id 对应的块，并在该块的「结果」行做替换
            pattern = re.compile(
                rf'(##[^\n]*\[pid:{re.escape(pid)}\][\s\S]*?)(\n- 实际结果：[^\n]*)',
                re.MULTILINE,
            )
            outcome_line = _format_outcome_line(outcome)
            if pattern.search(text):
                new_text = pattern.sub(lambda m: m.group(1) + outcome_line, text, count=1)
            else:
                # 没找到含 [pid:xxx] 的旧块，作为新块追加在最前
                block = (
                    f'## {datetime.now().strftime("%Y-%m-%d %H:%M")} — '
                    f'{outcome.get("name", code)} ({code})  [pid:{pid}]'
                    f'{outcome_line}\n'
                )
                new_text = block + '\n' + text
            fp.write_text(new_text, encoding='utf-8')
        return True
    except Exception:
        return False


def _format_outcome_line(outcome: dict[str, Any]) -> str:
    result = str(outcome.get('result', '?')).strip()
    actual = outcome.get('actual_pct')
    prec = outcome.get('precision_score')
    parts = [f'- 实际结果：{result}']
    if actual is not None:
        try:
            parts[-1] += f' / 实际 {float(actual):+.2f}%'
        except Exception:
            pass
    if prec is not None:
        try:
            parts[-1] += f' / 精度 {float(prec) * 100:.0f}%'
        except Exception:
            pass
    return '\n' + parts[0]


def _format_block(prediction: dict[str, Any], outcome: dict[str, Any] | None) -> str:
    """格式化为 markdown 块。

    一个块的结构：
      ## 2026-05-19 14:30 — 平安银行 (000001)  [pid:000001_20260519_143000]
      - 预测：bullish / 置信 7 / 目标 +5.2% / 止损 -3.0% / 周期 5 日
      - 失效条件：大盘跌破 3000 点
      - 推理摘要：宏观偏暖，板块共振，MACD 红柱扩张
      - 实际结果：win / 实际 +3.8% / 精度 73%
    """
    code = prediction.get('code', '')
    name = prediction.get('name', code)
    pid = prediction.get('prediction_id', '')
    created = prediction.get('created_at', '') or datetime.now().strftime('%Y-%m-%d %H:%M')
    # 标题
    pid_part = f'  [pid:{pid}]' if pid else ''
    head = f'## {created} — {name} ({code}){pid_part}'

    # 预测行
    direction = prediction.get('direction', 'neutral')
    conf = prediction.get('confidence', 0)
    target = prediction.get('target_pct')
    stop = prediction.get('stop_pct')
    horizon = prediction.get('horizon_days', 0)
    target_str = f'{target:+.1f}%' if isinstance(target, (int, float)) else str(target or '?')
    stop_str = f'-{abs(stop):.1f}%' if isinstance(stop, (int, float)) else str(stop or '?')
    pred_line = (
        f'- 预测：{direction} / 置信 {conf} / 目标 {target_str} / 止损 {stop_str} / 周期 {horizon} 日'
    )

    invalid_line = ''
    invalid = (prediction.get('invalidation') or '').strip()
    if invalid:
        invalid_line = f'\n- 失效条件：{invalid[:120]}'

    reasoning_line = ''
    reasoning = (prediction.get('reasoning') or '').strip()
    if reasoning:
        # 单行化 + 截断
        flat = re.sub(r'\s+', ' ', reasoning)
        reasoning_line = f'\n- 推理摘要：{flat[:240]}'

    outcome_line = ''
    if outcome:
        outcome_line = _format_outcome_line(outcome)
    else:
        outcome_line = '\n- 实际结果：待回填'

    return f'{head}\n{pred_line}{invalid_line}{reasoning_line}{outcome_line}\n'


# ---------- 检索 ----------

def retrieve_recent(code: str, k: int = _DEFAULT_K, days: int = 90) -> list[str]:
    """读取最近 k 条片段（按时间倒序）。

    days 限制只取 N 天内的归档，避免过老。
    返回片段列表，每条已截断到 _SNIPPET_MAX_CHARS。
    """
    code = str(code or '').strip()
    if not code:
        return []
    fp = _file_for(code)
    if not fp.exists():
        return []

    try:
        text = fp.read_text(encoding='utf-8')
    except Exception:
        return []

    # 按 "## YYYY-..." 切块
    blocks = re.split(r'\n(?=## \d{4}-\d{2}-\d{2})', text.strip())
    if not blocks:
        return []

    cutoff = datetime.now() - timedelta(days=days)
    parsed: list[tuple[datetime, str]] = []

    for blk in blocks:
        blk = blk.strip()
        if not blk:
            continue
        m = re.match(r'## (\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?)', blk)
        if not m:
            continue
        try:
            ts_str = m.group(1)
            ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M' if ' ' in ts_str else '%Y-%m-%d')
        except Exception:
            ts = datetime.now()
        if ts < cutoff:
            continue
        if len(blk) > _SNIPPET_MAX_CHARS:
            blk = blk[:_SNIPPET_MAX_CHARS] + '…'
        parsed.append((ts, blk))

    # 按时间戳倒序（最新在前），与归档顺序解耦
    parsed.sort(key=lambda x: x[0], reverse=True)
    return [blk for _, blk in parsed[:k]]


def retrieve_recent_for_prompt(code: str, k: int = _DEFAULT_K) -> str:
    """同 retrieve_recent，但已格式化为 prompt-ready 单字符串。"""
    snippets = retrieve_recent(code, k=k)
    if not snippets:
        return ''
    header = f'【历史分析记忆 · 该股最近 {len(snippets)} 次】\n'
    return header + '\n'.join(snippets)


def inject_into_prompt(prompt: str, code: str, k: int = _DEFAULT_K) -> str:
    """把历史记忆拼接到 prompt 末尾。

    无记忆时原样返回，不污染 caching 命中。
    """
    mem = retrieve_recent_for_prompt(code, k=k)
    if not mem:
        return prompt
    return f'{prompt}\n\n{mem}'


def append_retrospective(
    code: str,
    prediction_id: str,
    retro_text: str,
    error_type: str | None = None,
    root_cause: str | None = None,
) -> bool:
    """把审判日反思追加到对应 prediction 的归档块下面。

    定位策略：找含 [pid:xxx] 的旧块，在 "- 实际结果：" 行后插入：
      - 反思要点：{root_cause 优先，缺则取 retro 首 200 字单行化}
      - 错误类型：{X}   （仅 error_type 非 None 时）

    找不到旧块则当新块写入（保证不丢数据）。
    """
    code = str(code or '').strip()
    pid  = str(prediction_id or '').strip()
    if not code:
        return False

    _ensure_dir()
    fp = _file_for(code)

    retro_summary = (root_cause or '').strip()
    if not retro_summary:
        retro_summary = re.sub(r'\s+', ' ', retro_text.strip())[:200]
    retro_summary = retro_summary[:200]

    extra_lines = f'\n- 反思要点：{retro_summary}'
    if error_type:
        extra_lines += f'\n- 错误类型：{error_type}'

    try:
        with _LOCK:
            if not fp.exists():
                block = (
                    f'## {datetime.now().strftime("%Y-%m-%d %H:%M")} — '
                    f'{code}  [pid:{pid}]'
                    f'\n- 实际结果：（反思补录）'
                    f'{extra_lines}\n'
                )
                fp.write_text(block, encoding='utf-8')
                return True

            text = fp.read_text(encoding='utf-8')
            pattern = re.compile(
                rf'(##[^\n]*\[pid:{re.escape(pid)}\][\s\S]*?)(\n- 实际结果：[^\n]*)',
                re.MULTILINE,
            )
            if pattern.search(text):
                new_text = pattern.sub(
                    lambda m: m.group(1) + m.group(2) + extra_lines,
                    text, count=1,
                )
            else:
                block = (
                    f'## {datetime.now().strftime("%Y-%m-%d %H:%M")} — '
                    f'{code}  [pid:{pid}]'
                    f'\n- 实际结果：（反思补录）'
                    f'{extra_lines}\n'
                )
                new_text = block + '\n' + text
            fp.write_text(new_text, encoding='utf-8')
        return True
    except Exception:
        return False


# ---------- 维护工具 ----------

def list_archived_codes() -> list[str]:
    """列出所有已归档的股票代码。"""
    _ensure_dir()
    if not _MEM_DIR.exists():
        return []
    return sorted({fp.stem for fp in _MEM_DIR.glob('*.md')})


def get_stats() -> dict[str, int]:
    """归档统计：总股票数、总块数。"""
    codes = list_archived_codes()
    total_blocks = 0
    for code in codes:
        fp = _file_for(code)
        try:
            text = fp.read_text(encoding='utf-8')
            total_blocks += len(re.findall(r'^## \d{4}-\d{2}-\d{2}', text, re.MULTILINE))
        except Exception:
            pass
    return {'codes': len(codes), 'blocks': total_blocks}


def purge_old(days: int = 180) -> int:
    """删除超过 N 天的归档块（保留最近的）。返回删除块数。"""
    cutoff = datetime.now() - timedelta(days=days)
    removed = 0
    _ensure_dir()
    for fp in _MEM_DIR.glob('*.md'):
        try:
            text = fp.read_text(encoding='utf-8')
            blocks = re.split(r'\n(?=## \d{4}-\d{2}-\d{2})', text.strip())
            kept = []
            for blk in blocks:
                m = re.match(r'## (\d{4}-\d{2}-\d{2})', blk)
                if m:
                    try:
                        ts = datetime.strptime(m.group(1), '%Y-%m-%d')
                        if ts < cutoff:
                            removed += 1
                            continue
                    except Exception:
                        pass
                kept.append(blk)
            new_text = '\n'.join(kept)
            if new_text.strip():
                fp.write_text(new_text, encoding='utf-8')
            else:
                fp.unlink()
        except Exception:
            pass
    return removed


__all__ = [
    'archive_prediction', 'update_outcome', 'append_retrospective',
    'retrieve_recent', 'retrieve_recent_for_prompt', 'inject_into_prompt',
    'list_archived_codes', 'get_stats', 'purge_old',
]
