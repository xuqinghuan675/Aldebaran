"""Aldebaran v3.0 多 Agent 推理引擎。

架构：
  5 并行分析 Agent（V4.1 Flash thinking）  市场环境 / 基本面 / 技术 / 资金流向 / 事件催化
       ↓
  F 层 多空辩论（V4.1 Flash thinking）     看多→看空→裁决（基于5份市场报告）
       ↓
  Z 层 时间维度                    2~5 交易日超短线约束（确定性，无 LLM）
       ↓
  G 层 最终决策（V4.1 Flash thinking）     输出严格 JSON

设计要点：
  - 所有 LLM 调用走统一 base.py 客户端（带重试、超时）
  - 个股 AI 分析链路统一使用 deepseek-flash (V4.1 Flash) thinking
  - 分析层只能基于已取得资料、确定性画像和上游 Agent 结论，不允许补充未提供事实
  - 每个 Agent 的 system_prompt 字节级稳定 → DeepSeek caching 命中
  - user prompt 结构：[稳定前缀(profile+memory)] + [变化数据]
"""
from core.agents.macro_agent import run as run_macro_agent

from core.agents.company_agent import run as run_company_agent
from core.agents.technical_agent import run as run_technical_agent
from core.agents.fundflow_agent import run as run_fundflow_agent
from core.agents.news_event_agent import run as run_news_event_agent
from core.agents.synthesis import run_debate
from core.agents.horizon import build_horizon_directive
from core.agents.decision import run_decision

__all__ = [
    'run_macro_agent', 'run_company_agent', 'run_technical_agent',
    'run_fundflow_agent', 'run_news_event_agent',
    'run_debate', 'build_horizon_directive', 'run_decision',
]
