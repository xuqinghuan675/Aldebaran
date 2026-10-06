"""板块 → GitHub 仓库映射表。

用途：为特定板块提供"技术/开源生态"维度的情报来源。
当相关仓库有新 Release / 重大 Issue 时，生成 IntelEvent 注入情报面板。

格式：
    SECTOR_REPO_MAP[板块关键词] = [
        {"repo": "owner/repo", "label": "显示名称", "kind": "data|tool|research"},
    ]

维护说明：
  - 本文件是**纯静态**配置，不会随 repo 业务转向自动更新。
  - 建议每季度核查一次：`git log --oneline -5 <repo>` 确认仓库仍与板块相关。
  - 若某仓库已归档或转向，删除对应条目。
  - GitHub intel 注入后 **不经过** classify_headline 的 DeepSeek 分类，
    related_sectors 直接由本映射决定——mapping 失效会导致板块关联错误，
    而非分类错误（影响 Agent1 宏观分析师的情报上下文，但后果可控）。
"""
from __future__ import annotations

SECTOR_REPO_MAP: dict[str, list[dict]] = {
    # 人工智能 / 大模型
    "人工智能": [
        {"repo": "ggerganov/llama.cpp",      "label": "llama.cpp",        "kind": "tool"},
        {"repo": "huggingface/transformers",  "label": "HuggingFace",      "kind": "tool"},
        {"repo": "openai/openai-python",      "label": "OpenAI SDK",       "kind": "tool"},
    ],
    "大模型": [
        {"repo": "ggerganov/llama.cpp",       "label": "llama.cpp",        "kind": "tool"},
        {"repo": "deepseek-ai/DeepSeek-V3",   "label": "DeepSeek-V3",      "kind": "research"},
    ],
    # 半导体 / 芯片
    "半导体": [
        {"repo": "chiphuyen/aie-book",        "label": "AI芯片参考",       "kind": "research"},
        {"repo": "ucb-bar/chipyard",          "label": "Chipyard",         "kind": "tool"},
    ],
    "芯片": [
        {"repo": "ucb-bar/chipyard",          "label": "Chipyard",         "kind": "tool"},
    ],
    # 新能源 / 储能
    "新能源": [
        {"repo": "NREL/SAM",                  "label": "NREL SAM",         "kind": "tool"},
        {"repo": "nwchemgit/nwchem",          "label": "NWChem",           "kind": "research"},
    ],
    "储能": [
        {"repo": "NREL/SAM",                  "label": "NREL SAM",         "kind": "tool"},
    ],
    # 金融数据 / 量化
    "量化": [
        {"repo": "akfamily/akshare",          "label": "AKShare",          "kind": "data"},
        {"repo": "waditu/tushare",            "label": "Tushare",          "kind": "data"},
        {"repo": "quantopian/zipline",        "label": "Zipline",          "kind": "tool"},
    ],
    "金融科技": [
        {"repo": "akfamily/akshare",          "label": "AKShare",          "kind": "data"},
        {"repo": "OpenBB-finance/OpenBBTerminal", "label": "OpenBB",       "kind": "tool"},
    ],
    # 光伏 / 太阳能
    "光伏": [
        {"repo": "NREL/SAM",                  "label": "NREL SAM",         "kind": "tool"},
    ],
    # 通信 / 光模块
    "通信": [
        {"repo": "cisco/ChezScheme",          "label": "思科开源",         "kind": "tool"},
    ],
    # 机器人 / 工业自动化
    "机器人": [
        {"repo": "ros/ros2",                  "label": "ROS 2",            "kind": "tool"},
        {"repo": "bulletphysics/bullet3",     "label": "Bullet Physics",   "kind": "tool"},
    ],
    # 医疗 / 生物科技
    "医疗": [
        {"repo": "chanzuckerberg/cellxgene",  "label": "CellxGene",        "kind": "research"},
    ],
    "生物科技": [
        {"repo": "chanzuckerberg/cellxgene",  "label": "CellxGene",        "kind": "research"},
        {"repo": "deepmind/alphafold",        "label": "AlphaFold",        "kind": "research"},
    ],
}


def get_repos_for_sectors(sectors: list[str]) -> list[dict]:
    """返回给定板块列表对应的仓库集合（去重）。"""
    seen: set[str] = set()
    result: list[dict] = []
    for sector in sectors:
        for entry in SECTOR_REPO_MAP.get(sector, []):
            repo = entry['repo']
            if repo not in seen:
                seen.add(repo)
                result.append(entry)
    return result
