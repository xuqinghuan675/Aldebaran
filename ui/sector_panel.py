"""板块资金流面板：折线图 + 柱状图 + 排行表格。

支持「概念 / 行业」口径切换，默认概念（贴近热点主题）。
柱状图：净流入 TOP20 + 净流出 TOP10。
9 个 bug 修复（详见 memory）：
#1 严格 top20+底10；#2 NaN 断线；#3 当日缓存（按 kind 分文件）；
#4 默认概念口径，保留行业切换；#5 y 轴只扩张；
#6 见 data_worker 重试；#7 末端标签去重错位；
#8 午休缩为 5 分钟视觉宽度；#9 午休柱状图冻结。
"""
import pandas as pd
import numpy as np
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from core.qt_runtime import configure_qt_runtime
configure_qt_runtime()

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QTabWidget, QLabel, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QDialog, QPushButton, QListWidget, QListWidgetItem, QLineEdit, QMessageBox,
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QColor

import matplotlib
matplotlib.use('QtAgg')
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backend_bases import MouseButton
from matplotlib.figure import Figure
import matplotlib.patches as mpatches

from core.constants import DARK_BG, CHART_BG, MUTED, RED, GREEN, LINE_COLORS
from core.data_worker import DataWorker, is_trading_time, is_lunch_break
from ui.widgets.table_utils import configure_no_truncation
from core.cache import (
    save_sector_history, load_sector_history,
    save_sector_tracked, load_sector_tracked,
    save_sector_latest_df, load_sector_latest_df,
    save_sector_minute_history, load_sector_minute_history,
    save_sector_constituents, load_sector_constituents,
    load_ui_config, update_ui_config,
)
from core.net_setup import push2_get
from core.data_source import quotes_routed

_KIND_NAMES = {
    'concept': '概念',
    'industry': '行业',
}
_KINDS = ('concept', 'industry')
_FOCUS_SECTOR_KEYWORDS = (
    # 概念：AI主线（算力/大模型/应用）
    '大模型', 'AI应用', 'AI Agent', '算力', '人工智能',
    '智算中心', 'CPO', '光通信', '光模块',
    # 概念：人形机器人主线
    '人形机器人', '机器人', '谐波减速器', '丝杠', '传感器', '执行器',
    # 概念：自主可控
    'PCB', '半导体', '国产芯片', '存储芯片', '国产软件',
    # 概念：反内卷/涨价链
    '光伏', '锂电池', '化工', '钢铁', '锂矿',
    # 概念：其他主线
    '消费电子', '创新药', '商业航天', '白酒',
    '储能', '有色金属', '电力设备', '固态电池', '通信技术',
    # 行业：高潜力赛道
    '电子', '光伏设备', '医疗器械', '军工', '新能源车',
    '计算机', '通信设备', '工程机械', '低空经济', '量子',
    # 行业：有色/贵金属
    '黄金', '铜', '小金属', '稀土',
    # 行业：创新药/出海链
    '家电', '船舶', '跨境电商',
    # 行业：匹配实际名称
    '自动化设备', '激光设备', '激光', '数字芯片',
    '电网设备', '半导体设备', '半导体材料',
    '国防军工', '逆变器', '高端制造',
    # 科技主线（扩展）
    '钠离子', '自动驾驶', '智能驾驶', '车路云',
    'eVTOL', '飞行汽车', '无人机',
    '卫星互联网', '北斗', '脑机',
    # 制造主线（扩展）
    '航空发动机', '航天', '舰船',
    '核电', '核聚变', '工业母机', '数控机床',
    # 资源主线（扩展）
    '钨', '钼', '锑', '锗', '铝', '白银',
    # 医药主线（扩展）
    'GLP-1', 'ADC', '减肥药', 'CXO', '医美',
    # 消费/出海主线（扩展）
    '品牌出海', '宠物', '游戏', 'IP', '文旅',
    # 政策主线（扩展）
    '央企重组', '国企改革', '化债', '数据要素', '信创',
)
_FOCUS_SECTOR_ALIASES = {
    '光模块': '光通信模块',
    '军工': '国防军工',
    '新能源车': '新能源汽车',
    '量子': '量子技术',
    '激光': '激光设备',
    '低空经济': '低空经济（概念）',
    '家电': '家用电器',
    '船舶': '船舶制造',
    # 科技别名
    '固态电池': '电池',
    '自动驾驶': '汽车',
    'eVTOL': '低空经济（概念）',
    '卫星互联网': '通信',
    '商业航天': '航天',
    # 制造别名
    '核电': '电力',
    # 医药别名
    'GLP-1': '医药生物',
    'ADC': '医药生物',
    'CXO': '医药生物',
    # 消费/出海别名
    '品牌出海': '家用电器',
    '跨境电商': '商业贸易',
    '游戏': '传媒',
    # 政策别名
    '数据要素': '计算机',
    '信创': '计算机',
}

# ---------- 显示模式 ----------
# 折线图自选板块支持的显示模式：FOCUS（原默认）/ TOP5/10/20（按净流入）/ CUSTOM（用户自选）
DISPLAY_MODE_FOCUS = 'focus'
DISPLAY_MODE_TOP5 = 'top5'
DISPLAY_MODE_TOP10 = 'top10'
DISPLAY_MODE_TOP20 = 'top20'
DISPLAY_MODE_CUSTOM = 'custom'
_DISPLAY_MODE_LABELS = (
    (DISPLAY_MODE_FOCUS, 'FOCUS默认'),
    (DISPLAY_MODE_TOP5, 'TOP5'),
    (DISPLAY_MODE_TOP10, 'TOP10'),
    (DISPLAY_MODE_TOP20, 'TOP20'),
    (DISPLAY_MODE_CUSTOM, '自选'),
)

# ---------- 预置细分概念清单（P0-A 客户反馈⑨）----------
# ⚠️ 名称需与东财概念板块库实际命名对齐：
#   - 部分名称可能与东财不一致，请运行 tools/dump_sector_names.py 校准
#   - 不匹配时通过 _match_existing_sector 模糊匹配兜底（startswith / substring）
#   - 该清单仅用于「自选」对话框默认勾选项与可选项快捷入口
_PRESET_CONCEPT_LIST = (
    # 算力链
    '光通信模块', 'CPO', '液冷概念', '算力', 'AI服务器', 'HBM', 'PCB概念',
    # 半导体
    '先进封装', '第三代半导体', 'EDA', '光刻胶', '存储芯片',
    # 新能源
    '固态电池', '钠离子电池', '储能', '光伏概念', '风电',
    # 机器人
    '人形机器人', '减速器', '伺服系统', '机器视觉',
    # 政策/主题
    '低空经济（概念）', '商业航天', '卫星互联网', '可控核聚变', '量子技术',
    '创新药', '合成生物', '华为概念',
    # 周期
    '贵金属', '工业金属', '稀土永磁', '煤炭', '油气',
)


_TRACKED_SECTOR_EXCLUDES = (
    # 互联互通 / 宽指 / 被动标签
    '融资融券', '深股通', '沪股通', 'MSCI', '富时罗素',
    '标普道琼斯', '标准普尔', '转融券', '证金持股',
    '同花顺漂亮100', '沪深300', '中证', '上证50', '科创50', '北证50',
    '深成500', '大盘股', '蓝筹', '宽基',
    # 机构/持仓标签
    'QFII', '社保基金', '养老金', '机构重仓', '基金重仓', '保险重仓', '国家队',
    # 财报/事件驱动标签
    '年报预增', '一季报预增', '半年报预增', '三季报预增', '季报预增', '预盈预增',
    '回购增持', '高送转',
    # 央国企改革等泛政策概念（资金分散、方向性弱）
    '央国企',
    # 资金量巨大但行业信号弱
    '证券', '非银金融',
)


def _normalize_focus_keyword(keyword):
    return _FOCUS_SECTOR_ALIASES.get(keyword, keyword)


def _dedup_ordered(items):
    seen = set()
    result = []
    for item in items:
        item = _normalize_focus_keyword(str(item).strip())
        key = item.upper()
        if not item or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _is_excluded_tracked_sector(name):
    name = str(name)
    return any(keyword in name for keyword in _TRACKED_SECTOR_EXCLUDES)


def _filter_tracked_sector_list(items):
    return [str(item) for item in (items or []) if str(item) and not _is_excluded_tracked_sector(item)]


def _filter_sector_df(df):
    if df is None or df.empty or '行业' not in df.columns:
        return df
    mask = ~df['行业'].astype(str).apply(_is_excluded_tracked_sector)
    return df[mask].copy()


def _is_anomalous_snapshot(prev_snap, snap, tracked_sectors):
    if not prev_snap or not snap or not tracked_sectors:
        return False
    jumps_big = 0
    jumps_mid = 0
    compared = 0
    for sector in tracked_sectors:
        if sector not in prev_snap or sector not in snap:
            continue
        try:
            diff = abs(float(snap[sector]) - float(prev_snap[sector]))
        except (TypeError, ValueError):
            continue
        compared += 1
        if diff >= 25:
            jumps_big += 1
        if diff >= 8:
            jumps_mid += 1
    if compared < 6:
        return False
    if jumps_big >= max(3, compared // 3):
        return True
    if jumps_mid >= max(5, compared // 2):
        return True
    return False


def _match_existing_sector(keyword, sector_names):
    keyword_upper = keyword.upper()
    for name in sector_names:
        if str(name).upper() == keyword_upper:
            return name
    for name in sector_names:
        name_str = str(name)
        if name_str.startswith(keyword) and not name_str.startswith('非'):
            return name
    for name in sector_names:
        name_str = str(name)
        if keyword_upper in name_str.upper() and not name_str.startswith('非'):
            return name
    return None


def _smooth_xy(x_values, y_values):
    x = np.asarray(x_values, dtype=float)
    y = np.asarray(y_values, dtype=float)
    if np.isnan(y).any():
        return x, y
    mask = ~(np.isnan(x) | np.isnan(y))
    x = x[mask]
    y = y[mask]
    if len(x) < 4 or len(set(x)) < 4:
        return x, y

    order = np.argsort(x)
    x = x[order]
    y = y[order]
    unique_x, unique_idx = np.unique(x, return_index=True)
    y = y[unique_idx]
    x = unique_x
    if len(x) < 4:
        return x, y

    dense_n = min(max(len(x) * 4, 40), 240)
    dense_x = np.linspace(x[0], x[-1], dense_n)
    dense_y = np.interp(dense_x, x, y)
    window = min(9, max(3, len(dense_y) // 18 * 2 + 1))
    if window >= 3 and len(dense_y) > window:
        pad = window // 2
        kernel = np.ones(window) / window
        dense_y = np.convolve(np.pad(dense_y, pad, mode='edge'), kernel, mode='valid')
        dense_y[0] = y[0]
        dense_y[-1] = y[-1]
    return dense_x, dense_y


def _build_tracked_sector_list(df, existing, now, mode=DISPLAY_MODE_FOCUS, custom_sectors=None):
    """根据显示模式构建 tracked 板块清单。

    mode:
        focus     — 原默认：FOCUS 关键词命中 + 净流入 TOP10 + 净流出 TOP5
        top5/10/20 — 按净流入绝对值取 TOP N（正流入优先）
        custom    — 用户自选清单（custom_sectors），通过模糊匹配映射到当前 df 板块名

    custom_sectors: list[str]，仅 mode=custom 时使用
    """
    existing = _filter_tracked_sector_list(existing)
    df = _filter_sector_df(df)
    if df is None or df.empty:
        return list(existing)
    sector_names = [str(name) for name in df['行业'].tolist()]
    selected = []
    seen = set()

    def add_sector(name):
        if not name:
            return
        if _is_excluded_tracked_sector(name):
            return
        key = str(name).upper()
        if key in seen:
            return
        seen.add(key)
        selected.append(str(name))

    if mode == DISPLAY_MODE_CUSTOM:
        for raw in (custom_sectors or []):
            keyword = _normalize_focus_keyword(str(raw).strip())
            matched = _match_existing_sector(keyword, sector_names)
            if matched:
                add_sector(matched)
        # 自选模式：即便为空也不回退 FOCUS，留给上层决定
        return selected

    if mode in (DISPLAY_MODE_TOP5, DISPLAY_MODE_TOP10, DISPLAY_MODE_TOP20):
        n_map = {DISPLAY_MODE_TOP5: 5, DISPLAY_MODE_TOP10: 10, DISPLAY_MODE_TOP20: 20}
        n = n_map[mode]
        # 按净额绝对值倒序，正流入优先（同绝对值时 net>0 排前）
        sorted_df = df.assign(_abs=df['净额'].abs()).sort_values(
            by=['_abs', '净额'], ascending=[False, False]
        ).head(n)
        for name in sorted_df['行业'].tolist():
            add_sector(name)
        return selected or list(existing)

    # mode == FOCUS（默认）
    if existing and len(existing) >= 15 and not (now.hour == 9 and 30 <= now.minute < 60):
        return list(existing)

    for keyword in _dedup_ordered(_FOCUS_SECTOR_KEYWORDS):
        matched = _match_existing_sector(keyword, sector_names)
        if matched:
            add_sector(matched)

    inflow = df[df['净额'] > 0].sort_values('净额', ascending=False).head(10)
    outflow = df[df['净额'] < 0].sort_values('净额', ascending=True).head(5)
    for name in inflow['行业'].tolist():
        add_sector(name)
    for name in outflow['行业'].tolist():
        add_sector(name)

    return selected or list(existing)


def _fetch_sector_data_eastmoney(kind):
    type_map = {
        'concept': '3',
        'industry': '2',
    }
    path = '/api/qt/clist/get'
    page_size = 100
    params = {
        'pn': '1',
        'pz': str(page_size),
        'po': '1',
        'np': '1',
        'ut': 'b2884a393a59ad64002292a3e90d46a5',
        'fltt': '2',
        'invt': '2',
        'fid': 'f62',
        'fid0': 'f62',
        'fs': f"m:90 t:{type_map.get(kind, '3')}",
        'stat': '1',
        'fields': 'f12,f14,f3,f62',
    }
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
        ),
    }
    rows = []
    total = 0
    page = 1
    while True:
        params['pn'] = str(page)
        resp = push2_get(path, params=params, headers=headers, timeout=8)
        resp.raise_for_status()
        data = resp.json().get('data') or {}
        page_rows = data.get('diff') or []
        if not page_rows:
            break
        rows.extend(page_rows)
        total = int(data.get('total') or len(rows))
        if page * page_size >= total:
            break
        page += 1

    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError('东财板块资金接口返回为空')

    result = pd.DataFrame({
        '代码': df.get('f12'),
        '行业': df.get('f14'),
        '行业-涨跌幅': pd.to_numeric(df.get('f3'), errors='coerce'),
        '净额': pd.to_numeric(df.get('f62'), errors='coerce') / 100000000,
    })
    result = result.dropna(subset=['行业', '净额'])
    result['行业-涨跌幅'] = result['行业-涨跌幅'].fillna(0)
    if result.empty:
        raise ValueError('东财板块资金接口字段为空')
    return result


def _fetch_sector_data(kind):
    try:
        return _fetch_sector_data_eastmoney(kind)
    except Exception as eastmoney_error:
        raise RuntimeError(f'东财接口失败: {eastmoney_error}') from eastmoney_error


def _fetch_sector_minute_series(name, code, target_date=None):
    path = '/api/qt/stock/fflow/kline/get'
    params = {
        'lmt': '0',
        'klt': '1',
        'secid': f'90.{code}',
        'fields1': 'f1,f2,f3,f7',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63',
    }
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
        ),
    }
    resp = push2_get(path, params=params, headers=headers, timeout=6)
    resp.raise_for_status()
    data = (resp.json().get('data') or {})
    rows = []
    for line in data.get('klines') or []:
        parts = str(line).split(',')
        if len(parts) < 2:
            continue
        try:
            t = datetime.strptime(parts[0], '%Y-%m-%d %H:%M')
            val = float(parts[1]) / 100000000
        except Exception:
            continue
        if target_date is not None and t.date() != target_date:
            continue
        rows.append((t, name, val))
    return rows


def _fetch_sector_minute_history(df, tracked_sectors, target_date=None):
    if '代码' not in df.columns:
        return []
    code_by_name = {
        str(row['行业']): str(row['代码'])
        for _, row in df.dropna(subset=['行业', '代码']).iterrows()
    }
    targets = [
        (name, code_by_name.get(name))
        for name in tracked_sectors
        if code_by_name.get(name)
    ]
    if not targets:
        return []

    snapshots = {}
    # 并发数从 8 降到 4：降低 push2 瞬时压力，规避 EM 按源 IP 限流
    # 实测：8 并发时 EM 偶发 RemoteDisconnected；4 并发显著减少
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_fetch_sector_minute_series, name, code, target_date): name
            for name, code in targets
        }
        for future in as_completed(futures):
            try:
                for t, name, val in future.result():
                    snapshots.setdefault(t, {})[name] = val
            except Exception:
                continue
    return sorted(snapshots.items(), key=lambda item: item[0])


def _fetch_sector_payload(
    kind, existing_tracked=None, fetch_minute_history=True,
    mode=DISPLAY_MODE_FOCUS, custom_sectors=None,
):
    now = datetime.now()
    df = _fetch_sector_data(kind)
    tracked = _build_tracked_sector_list(
        df, existing_tracked, now, mode=mode, custom_sectors=custom_sectors,
    )
    minute_history = []
    if fetch_minute_history:
        minute_history = _fetch_sector_minute_history(df, tracked, now.date())
    return kind, df, tracked, minute_history


def _code_to_secid(code):
    """纯6位代码 → 东财 secid（1.xxxxxx 或 0.xxxxxx）。"""
    c = str(code).strip()
    if c.startswith('92'):
        return f'0.{c}'
    if c.startswith(('6', '5', '9')):
        return f'1.{c}'
    return f'0.{c}'


def _fetch_sector_top5_stocks(sector_name, sector_code):
    """获取板块成分股 TOP5（混合策略）。

    1. 成分股列表：缓存优先，过期或无缓存时 push2 拉取
    2. 腾讯行情三因子初筛 TOP10：涨跌幅×0.4 + 成交额排名×0.3 + 换手率×0.3
    3. 对 TOP10 补东财主力资金字段 → 有资金流时按主力净额重排取 TOP5
    4. 东财失败时直接展示腾讯排序 TOP5，标注"行情兜底"
    """
    # 1. 成分股列表
    constituents = load_sector_constituents(sector_code)
    if not constituents:
        params = {
            'pn': '1', 'pz': '500', 'po': '1', 'np': '1',
            'ut': 'b2884a393a59ad64002292a3e90d46a5',
            'fltt': '2', 'invt': '2',
            'fid': 'f3',
            'fs': f'b:{sector_code}+f:!50',
            'fields': 'f12,f14',
        }
        resp = push2_get('/api/qt/clist/get', params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json().get('data') or {}
        diff = data.get('diff') or []
        constituents = [
            {'code': str(item.get('f12', '')), 'name': str(item.get('f14', ''))}
            for item in diff if item.get('f12')
        ]
        if constituents:
            save_sector_constituents(sector_code, constituents)

    if not constituents:
        raise RuntimeError(f'无法获取板块 {sector_name} 的成分股列表')

    # 2. 行情批量取
    codes = [c['code'] for c in constituents]
    name_map = {c['code']: c.get('name', '') for c in constituents}
    quotes = quotes_routed(codes, timeout=8)

    scored = []
    for code in codes:
        q = quotes.get(code, {})
        if not q:   # 只跳过完全无行情的，price=0 的盘前股票照样纳入
            continue
        scored.append({
            'code': code,
            'name': q.get('name') or name_map.get(code, ''),
            'price': q.get('price', 0),
            'pct': q.get('pct', 0),
            'amount': q.get('amount', 0),
            'turnover': q.get('turnover', 0),
        })

    if not scored:
        # 终极兜底：行情全部失败时，直接返回成分股名单（无排名）
        fallback = [
            {'code': c['code'], 'name': c.get('name', ''), 'pct': 0,
             'amount': 0, 'turnover': 0, 'main_net': None}
            for c in constituents[:5]
        ]
        return {
            'sector_name': sector_name, 'sector_code': sector_code,
            'stocks': fallback, 'fund_ok': False,
            'total_constituents': len(constituents),
        }

    # 3. 三因子打分排名
    n = len(scored)
    scored.sort(key=lambda x: x['pct'])
    for i, s in enumerate(scored):
        s['_pct_r'] = (i + 1) / n
    scored.sort(key=lambda x: x['amount'])
    for i, s in enumerate(scored):
        s['_amt_r'] = (i + 1) / n
    scored.sort(key=lambda x: x['turnover'])
    for i, s in enumerate(scored):
        s['_turn_r'] = (i + 1) / n
    for s in scored:
        s['score'] = 0.4 * s['_pct_r'] + 0.3 * s['_amt_r'] + 0.3 * s['_turn_r']
    scored.sort(key=lambda x: x['score'], reverse=True)
    top10 = scored[:10]

    # 4. 对 TOP10 补东财主力资金
    fund_ok = False
    try:
        secids = ','.join(_code_to_secid(s['code']) for s in top10)
        params = {
            'fltt': '2', 'invt': '2',
            'ut': 'b2884a393a59ad64002292a3e90d46a5',
            'fields': 'f12,f14,f62,f184',
            'secids': secids,
        }
        resp = push2_get('/api/qt/ulist.np/get', params=params, timeout=6)
        resp.raise_for_status()
        fund_diff = (resp.json().get('data') or {}).get('diff') or []
        fund_map = {}
        for item in fund_diff:
            c = str(item.get('f12', ''))
            fund_map[c] = {'main_net': item.get('f62'), 'main_pct': item.get('f184')}
        for s in top10:
            f = fund_map.get(s['code'], {})
            s['main_net'] = f.get('main_net')
            s['main_pct'] = f.get('main_pct')
        has_fund = [s for s in top10 if s.get('main_net') is not None]
        if has_fund:
            has_fund.sort(key=lambda x: float(x.get('main_net', 0) or 0), reverse=True)
            top10 = has_fund[:5]
            fund_ok = True
    except Exception:
        pass

    if not fund_ok:
        top10 = top10[:5]

    return {
        'sector_name': sector_name,
        'sector_code': sector_code,
        'stocks': top10,
        'fund_ok': fund_ok,
        'total_constituents': len(constituents),
    }


def _check_resonance_for_sectors(candidates):
    """检查板块资金共振条件（后台 worker 中运行）。

    candidates: list of {'name': str, 'code': str, 'net': float}
    返回 list of {'sector_name', 'sector_code', 'net', 'zt_count'}
    """
    from core.alert_center import is_limit_up
    results = []
    for s in candidates:
        constituents = load_sector_constituents(s['code'])
        if not constituents:
            try:
                params = {
                    'pn': '1', 'pz': '500', 'po': '1', 'np': '1',
                    'ut': 'b2884a393a59ad64002292a3e90d46a5',
                    'fltt': '2', 'invt': '2', 'fid': 'f3',
                    'fs': f'b:{s["code"]}+f:!50',
                    'fields': 'f12,f14',
                }
                resp = push2_get('/api/qt/clist/get', params=params, timeout=6)
                resp.raise_for_status()
                diff = (resp.json().get('data') or {}).get('diff') or []
                constituents = [
                    {'code': str(item.get('f12', '')), 'name': str(item.get('f14', ''))}
                    for item in diff if item.get('f12')
                ]
                if constituents:
                    save_sector_constituents(s['code'], constituents)
            except Exception:
                continue
        if not constituents:
            continue
        codes = [c['code'] for c in constituents]
        name_map = {c['code']: c.get('name', '') for c in constituents}
        quotes = quotes_routed(codes, timeout=8)
        zt_count = sum(
            1 for code, q in quotes.items()
            if q and q.get('price') and is_limit_up(
                code,
                q.get('name') or name_map.get(code, ''),
                float(q.get('pct', 0) or 0),
            )
        )
        results.append({
            'sector_name': s['name'],
            'sector_code': s['code'],
            'net': s['net'],
            'zt_count': zt_count,
        })
    return results


# ---------- 时间→X 轴映射 ----------
_AM_OPEN = 9 * 60 + 30      # 570
_AM_CLOSE = 11 * 60 + 30    # 690
_PM_OPEN = 13 * 60          # 780
_PM_CLOSE = 15 * 60         # 900
_AM_LEN = _AM_CLOSE - _AM_OPEN  # 120
_PM_LEN = _PM_CLOSE - _PM_OPEN  # 120
_X_TOTAL = _AM_LEN + _PM_LEN  # 240


def _minute_of_day(dt):
    return dt.hour * 60 + dt.minute + dt.second / 60


def _to_x(dt, period='full'):
    """真实时间 → 交易分钟序号（0~_X_TOTAL），午休区间 clamp 到 _AM_LEN 避免线段倒退。

    映射规则（full）：
        9:30 → 0，11:30 → 120，11:30~13:00 一律 snap 到 120，13:00 → 120，15:00 → 240。
    morning/afternoon 模式同样 clamp 到对应区间的两端，防止脏戳画出区外的点。
    """
    m = _minute_of_day(dt)
    if period == 'morning':
        return min(max(m - _AM_OPEN, 0), _AM_LEN)
    if period == 'afternoon':
        return min(max(m - _PM_OPEN, 0), _PM_LEN)
    # full 模式
    if m < _AM_OPEN:
        return 0
    if m <= _AM_CLOSE:
        return m - _AM_OPEN
    if m < _PM_OPEN:           # 11:30~13:00 午休 → 紧贴上午收盘点
        return _AM_LEN
    if m <= _PM_CLOSE:
        return _AM_LEN + (m - _PM_OPEN)
    return _X_TOTAL            # 15:00 之后


def _period_segments(history, period):
    am_idx = [
        i for i, (t, _) in enumerate(history)
        if _AM_OPEN <= _minute_of_day(t) <= _AM_CLOSE
    ]
    pm_idx = [
        i for i, (t, _) in enumerate(history)
        if _PM_OPEN <= _minute_of_day(t) <= _PM_CLOSE
    ]
    if period == 'morning':
        return [am_idx]
    if period == 'afternoon':
        return [pm_idx]
    # full 模式：合并为一段，matplotlib 自动跨午休画连续折线
    return [am_idx + pm_idx]


def _period_xlim(period):
    if period in ('morning', 'afternoon'):
        return 0, _AM_LEN + 34
    return 0, _X_TOTAL + 42


def _period_label(period):
    return {
        'morning': '上午',
        'afternoon': '下午',
        'full': '全天',
    }.get(period, '全天')


def _period_xticks(period):
    if period == 'morning':
        tick_times = [(9, 30), (10, 0), (10, 30), (11, 0), (11, 30)]
    elif period == 'afternoon':
        tick_times = [(13, 0), (13, 30), (14, 0), (14, 30), (15, 0)]
    else:
        ticks = [
            _to_x(datetime(2000, 1, 1, 9, 30), period),
            _to_x(datetime(2000, 1, 1, 10, 0), period),
            _to_x(datetime(2000, 1, 1, 10, 30), period),
            _to_x(datetime(2000, 1, 1, 11, 0), period),
            _AM_LEN,
            _to_x(datetime(2000, 1, 1, 13, 30), period),
            _to_x(datetime(2000, 1, 1, 14, 0), period),
            _to_x(datetime(2000, 1, 1, 14, 30), period),
            _to_x(datetime(2000, 1, 1, 15, 0), period),
        ]
        labels = ['09:30', '10:00', '10:30', '11:00', '11:30/13:00', '13:30', '14:00', '14:30', '15:00']
        return ticks, labels
    ticks = [_to_x(datetime(2000, 1, 1, h, m), period) for h, m in tick_times]
    labels = [f'{h:02d}:{m:02d}' for h, m in tick_times]
    return ticks, labels


# ---------- 折线图 ----------
class LineChartCanvas(FigureCanvas):
    def __init__(self, parent=None):
        self.fig = Figure(facecolor=DARK_BG, tight_layout=True)
        super().__init__(self.fig)
        self.setParent(parent)
        self.ax = self.fig.add_subplot(111)
        # y 轴范围状态（修复 #5：只扩张不收缩）
        self._y_min = None
        self._y_max = None
        self._y_date = None  # 跨日重置
        self._y_period = None
        self._manual_ylim = None
        self._default_ylim = None
        self._drag_start = None
        self._last_chart_args = None
        self.mpl_connect('scroll_event', self._on_scroll)
        self.mpl_connect('button_press_event', self._on_button_press)
        self.mpl_connect('motion_notify_event', self._on_motion)
        self.mpl_connect('button_release_event', self._on_button_release)
        self.show_message('板块资金分时流向', '正在初始化图表...')

    def reset_axis(self):
        self._y_min = None
        self._y_max = None
        self._y_date = None
        self._y_period = None
        self._manual_ylim = None
        self._default_ylim = None
        self._drag_start = None
        self._last_chart_args = None

    def show_message(self, title, message):
        ax = self.ax
        ax.clear()
        self.fig.patch.set_facecolor(DARK_BG)
        ax.set_facecolor(CHART_BG)
        ax.set_axis_off()
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        card = mpatches.FancyBboxPatch(
            (0.22, 0.33), 0.56, 0.34,
            boxstyle='round,pad=0.03,rounding_size=0.025',
            transform=ax.transAxes,
            linewidth=1.0,
            edgecolor='#2f4b7c',
            facecolor='#101a33',
            alpha=0.95,
        )
        ax.add_patch(card)
        if title:
            ax.text(
                0.5, 0.60, title,
                transform=ax.transAxes,
                ha='center',
                va='center',
                fontsize=15,
                fontweight='bold',
                color='white',
            )
        ax.text(
            0.5, 0.47, message,
            transform=ax.transAxes,
            ha='center',
            va='center',
            fontsize=11.5,
            color=MUTED,
            linespacing=1.9,
        )
        ax.text(
            0.5, 0.38, '加载完成后自动显示完整曲线',
            transform=ax.transAxes,
            ha='center',
            va='center',
            fontsize=10,
            color='#5dade2',
        )
        self.fig.tight_layout()
        self.draw_idle()
        self._last_chart_args = None

    def _refresh_with_manual_ylim(self):
        if not self._last_chart_args:
            self.draw_idle()
            return
        self.update_chart(**self._last_chart_args.copy())

    def _on_scroll(self, event):
        if event.inaxes != self.ax or event.ydata is None:
            return
        y_min, y_max = self.ax.get_ylim()
        if not np.isfinite(y_min) or not np.isfinite(y_max) or y_max <= y_min:
            return
        step = getattr(event, 'step', 0) or (1 if event.button == 'up' else -1)
        factor = 0.82 ** step
        center = event.ydata
        new_min = center - (center - y_min) * factor
        new_max = center + (y_max - center) * factor
        if new_max - new_min < 0.5:
            return
        self._manual_ylim = (new_min, new_max)
        self._refresh_with_manual_ylim()

    def _on_button_press(self, event):
        if event.inaxes != self.ax:
            return
        if getattr(event, 'dblclick', False):
            self._drag_start = None
            self._manual_ylim = None
            if self._default_ylim:
                self._refresh_with_manual_ylim()
            return
        if event.button == MouseButton.LEFT and event.y is not None:
            self._drag_start = (event.y, self.ax.get_ylim())

    def _on_motion(self, event):
        if not self._drag_start:
            return
        start_y, (y_min, y_max) = self._drag_start
        height = max(float(self.ax.bbox.height), 1.0)
        span = y_max - y_min
        if span <= 0:
            return
        delta = -((event.y or start_y) - start_y) / height * span
        new_min = y_min + delta
        new_max = y_max + delta
        self._manual_ylim = (new_min, new_max)
        self.ax.set_ylim(new_min, new_max)
        self.draw_idle()

    def _on_button_release(self, event):
        should_refresh = self._drag_start is not None
        self._drag_start = None
        if should_refresh and self._manual_ylim:
            self._refresh_with_manual_ylim()

    def update_chart(
        self, history, data_date=None, kind_label='板块',
        tracked_sectors=None, period='full', stale_label='',
    ):
        self._last_chart_args = {
            'history': history,
            'data_date': data_date,
            'kind_label': kind_label,
            'tracked_sectors': tracked_sectors,
            'period': period,
            'stale_label': stale_label,
        }
        ax = self.ax
        ax.clear()
        ax.set_axis_on()
        ax.set_facecolor(CHART_BG)

        # 跨日重置 y 轴
        if (
            data_date != self._y_date
            or period != self._y_period
        ):
            self._y_min = None
            self._y_max = None
            self._y_date = data_date
            self._y_period = period
            self._manual_ylim = None
            self._default_ylim = None
            self._drag_start = None

        date_str = data_date.strftime("%m月%d日") if data_date else ""

        segments = _period_segments(history, period)
        visible_indices = [i for indices in segments for i in indices]
        visible_history = [history[i] for i in visible_indices]

        if len(visible_history) < 2:
            if not is_trading_time():
                msg = (f'{date_str} 当前非交易时间\n\n'
                       f'当前时段暂无足够分时数据\n'
                       f'请切换到「资金排名」标签查看当前快照')
            elif visible_history:
                msg = f'{date_str} 当前时段等待数据积累中...\n已采集 {len(visible_history)} 个点'
            else:
                msg = f'{date_str} 当前时段暂无分时数据'
            ax.text(0.5, 0.5, msg, transform=ax.transAxes, ha='center', va='center',
                    fontsize=13, color=MUTED, linespacing=1.8)
            ax.set_xticks([])
            self.fig.tight_layout()
            self.draw_idle()
            return

        show_sectors = list(tracked_sectors or [])
        if not show_sectors:
            show_sectors = list(history[-1][1].keys())

        x_vals = [_to_x(t, period) for t, _ in history]

        all_vals = []
        line_endpoints = {}
        for i, sector in enumerate(show_sectors):
            color = LINE_COLORS[i % len(LINE_COLORS)]
            for indices in segments:
                if not indices:
                    continue
                seg_x = [x_vals[j] for j in indices]
                # 修复 #2：缺失值用 NaN，matplotlib 自动断线
                seg_y = [history[j][1].get(sector, float('nan')) for j in indices]
                plot_x, plot_y = _smooth_xy(seg_x, seg_y)
                ax.plot(plot_x, plot_y, color=color, linewidth=1.8, alpha=0.9)
                all_vals.extend(v for v in seg_y if not pd.isna(v))
            for j in reversed(visible_indices):
                endpoint_y = history[j][1].get(sector, float('nan'))
                if not pd.isna(endpoint_y):
                    line_endpoints[sector] = (x_vals[j], endpoint_y, color)
                    break

        # 修复 #5：y 轴只扩张不收缩
        if all_vals:
            cur_min, cur_max = min(all_vals), max(all_vals)
            self._y_min = cur_min if self._y_min is None else min(self._y_min, cur_min)
            self._y_max = cur_max if self._y_max is None else max(self._y_max, cur_max)
            pad = max((self._y_max - self._y_min) * 0.08, 1.0)
            ax.set_ylim(self._y_min - pad, self._y_max + pad)
            self._default_ylim = ax.get_ylim()
            if self._manual_ylim:
                ax.set_ylim(*self._manual_ylim)

        ax.axhline(y=0, color='white', linewidth=0.5, alpha=0.3)
        x_left, x_right = _period_xlim(period)
        ax.set_xlim(x_left, x_right)

        ticks, labels = _period_xticks(period)
        ax.set_xticks(ticks)
        ax.set_xticklabels(labels)

        # 末端标签：贴近线尾；同高度拥挤时横向错开，避免长引线
        labels_to_place = []
        for i, sector in enumerate(show_sectors):
            endpoint = line_endpoints.get(sector)
            if not endpoint:
                continue
            endpoint_x, endpoint_y, color = endpoint
            labels_to_place.append((sector, endpoint_x, endpoint_y, color))

        labels_to_place.sort(key=lambda x: x[2])
        y_low, y_high = ax.get_ylim()
        ax_h = y_high - y_low
        label_count = max(len(labels_to_place), 1)
        row_sep = max(ax_h * 0.018, ax_h / (label_count + 8) * 0.7)
        lower = y_low + ax_h * 0.03
        upper = y_high - ax_h * 0.03
        placed = []
        for sector, endpoint_x, endpoint_y, color in labels_to_place:
            y = min(max(endpoint_y, lower), upper)
            used_lanes = {
                item[5] for item in placed
                if abs(item[4] - y) < row_sep
            }
            lane = 0
            while lane in used_lanes:
                lane += 1
            placed.append([sector, endpoint_x, endpoint_y, color, y, lane])
        for sector, endpoint_x, endpoint_y, color, label_y, lane in placed:
            label_x = min(endpoint_x + 2.2 + lane * 9.0, x_right - 12)
            font_size = 6.8 if label_count >= 22 else 7.2
            ax.scatter([endpoint_x], [endpoint_y], s=16, color=color, zorder=4,
                       edgecolors=CHART_BG, linewidths=0.6)
            if lane > 0:
                ax.plot(
                    [endpoint_x, label_x - 0.5],
                    [endpoint_y, label_y],
                    color=color,
                    linewidth=0.45,
                    alpha=0.18,
                    zorder=3,
                )
            ax.annotate(
                f'{sector} {endpoint_y:.0f}',
                xy=(endpoint_x, endpoint_y),
                xytext=(label_x, label_y),
                textcoords='data',
                va='center',
                ha='left',
                fontsize=font_size,
                color=color,
                fontweight='bold',
                bbox={
                    'boxstyle': 'round,pad=0.15',
                    'facecolor': CHART_BG,
                    'edgecolor': 'none',
                    'alpha': 0.72,
                },
                clip_on=True,
                zorder=5,
            )

        trading_status = '' if is_trading_time() else '（已冻结）'
        stale_str = f' [{stale_label}]' if stale_label else ''
        ax.set_title(f'{date_str} {kind_label}板块资金分时流向{stale_str} {trading_status}',
                     fontsize=14, fontweight='bold', color='white', pad=12)
        rule_text = f'{_period_label(period)} | 滚轮缩放Y轴，左键拖动平移，双击复位 | 当天固定跟踪 {len(show_sectors)} 条'
        ax.text(0.012, 0.985, rule_text, transform=ax.transAxes,
                ha='left', va='top', fontsize=8, color=MUTED, alpha=0.9)
        ax.set_ylabel('净流入（亿元）', color=MUTED, fontsize=10)
        ax.tick_params(colors='white', labelsize=9)
        ax.grid(color='#444444', linewidth=0.3, alpha=0.5)
        for spine in ax.spines.values():
            spine.set_color('#444444')

        self.fig.tight_layout()
        self.draw_idle()


# ---------- 柱状图 ----------
class BarChartCanvas(FigureCanvas):
    def __init__(self, parent=None):
        self.fig = Figure(facecolor=DARK_BG, tight_layout=True)
        super().__init__(self.fig)
        self.setParent(parent)
        self.ax = self.fig.add_subplot(111)
        self.show_message('资金排名', '正在等待板块资金排名数据...')

    def show_message(self, title, message):
        ax = self.ax
        ax.clear()
        self.fig.patch.set_facecolor(DARK_BG)
        ax.set_facecolor(CHART_BG)
        ax.set_axis_off()
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        card = mpatches.FancyBboxPatch(
            (0.25, 0.36), 0.50, 0.28,
            boxstyle='round,pad=0.03,rounding_size=0.025',
            transform=ax.transAxes,
            linewidth=1.0,
            edgecolor='#2f4b7c',
            facecolor='#101a33',
            alpha=0.95,
        )
        ax.add_patch(card)
        ax.text(
            0.5, 0.55, title,
            transform=ax.transAxes,
            ha='center',
            va='center',
            fontsize=15,
            fontweight='bold',
            color='white',
        )
        ax.text(
            0.5, 0.45, message,
            transform=ax.transAxes,
            ha='center',
            va='center',
            fontsize=11.5,
            color=MUTED,
        )
        self.fig.tight_layout()
        self.draw_idle()

    def update_chart(self, df_plot, title_time, kind_label='板块'):
        ax = self.ax
        ax.clear()
        ax.set_axis_on()
        ax.set_facecolor(CHART_BG)

        abs_vals = df_plot['净额'].abs()
        colors = [RED if x > 0 else GREEN for x in df_plot['净额']]
        bars = ax.barh(df_plot['行业'], abs_vals, color=colors, height=0.52)

        x_max = abs_vals.max()
        if x_max == 0:
            x_max = 1

        n_outflow = (df_plot['净额'] < 0).sum()

        for bar, val, chg in zip(bars, df_plot['净额'], df_plot['行业-涨跌幅']):
            y = bar.get_y() + bar.get_height() / 2
            abs_val = abs(val)
            chg_str = f"+{chg:.2f}%" if chg > 0 else f"{chg:.2f}%"
            chg_color = '#ff9999' if chg > 0 else '#99ffbb'

            ax.text(abs_val * 0.01, y, f'{val:.1f}亿',
                    va='center', ha='left', fontsize=8, color='white')
            ax.text(abs_val + x_max * 0.01, y, chg_str,
                    va='center', ha='left', fontsize=7.5, color=chg_color)

        if n_outflow > 0:
            sep_y = n_outflow - 0.5
            ax.axhline(y=sep_y, color='#888888', linewidth=1, linestyle='--', alpha=0.6)
            ax.text(x_max * 0.95, sep_y + 0.3, '▲ 净流入 TOP20',
                    va='bottom', ha='right', fontsize=8, color='#ff9999', alpha=0.8)
            ax.text(x_max * 0.95, sep_y - 0.3, '▼ 净流出 TOP10',
                    va='top', ha='right', fontsize=8, color='#99ffbb', alpha=0.8)

        ax.grid(axis='x', color='#444444', linewidth=0.3, alpha=0.5)
        ax.set_xlim(0, x_max * 1.25)

        ax.set_title(f'{title_time} {kind_label}板块资金流向（净流入TOP20 + 净流出TOP10）',
                     fontsize=14, fontweight='bold', color='white', pad=12)
        ax.set_xlabel('金额（亿元）', color=MUTED, fontsize=10)
        ax.tick_params(colors='white', labelsize=8.5)
        for spine in ax.spines.values():
            spine.set_color('#444444')

        red_patch = mpatches.Patch(color=RED, label='净流入')
        green_patch = mpatches.Patch(color=GREEN, label='净流出')
        ax.legend(handles=[red_patch, green_patch], loc='lower right',
                  facecolor=DARK_BG, edgecolor='#444444', labelcolor='white')

        self.fig.tight_layout()
        self.draw_idle()


# ---------- 排行表格 ----------
def _compute_heat(df):
    """热度 = norm(涨跌幅)×0.4 + norm(|净额|)×0.3 + norm(净额)×0.3。

    换手率在板块层面不直接可用，用 |净额| 作为板块活跃度代理。
    所有归一化为 min-max [0, 1]，返回 Series。
    """
    def _norm(s):
        mn, mx = s.min(), s.max()
        return (s - mn) / (mx - mn) if mx > mn else s * 0

    pct  = pd.to_numeric(df['行业-涨跌幅'], errors='coerce').fillna(0)
    net  = pd.to_numeric(df['净额'],       errors='coerce').fillna(0)
    act  = net.abs()
    return _norm(pct) * 0.4 + _norm(act) * 0.3 + _norm(net) * 0.3


class RankTable(QTableWidget):
    _COL_FIELDS = [None, '净额', '行业-涨跌幅', '_heat']
    _COL_LABELS = ['板块', '净额(亿)', '涨跌幅', '热度']
    sector_selected = Signal(str, str)  # (sector_name, sector_code)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._df = None
        self._df_sorted = None
        self._sort_col = 1      # 默认按净额排序
        self._sort_asc = False  # 默认降序
        self.setColumnCount(4)
        self._refresh_headers()
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setCursor(Qt.CursorShape.PointingHandCursor)
        self.verticalHeader().setDefaultSectionSize(24)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setMinimumWidth(320)
        self.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self.cellDoubleClicked.connect(self._on_cell_double_clicked)

    def _refresh_headers(self):
        labels = []
        for i, name in enumerate(self._COL_LABELS):
            if i == self._sort_col:
                labels.append(f'{name} {"▲" if self._sort_asc else "▼"}')
            else:
                labels.append(name)
        self.setHorizontalHeaderLabels(labels)

    def _on_header_clicked(self, col):
        if col == 0:
            return
        if col == self._sort_col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = False
        self._refresh_headers()
        self._render()

    def update_data(self, df):
        self._df = _filter_sector_df(df)
        if self._df is not None and not self._df.empty:
            self._df = self._df.copy()
            self._df['_heat'] = _compute_heat(self._df)
        self._render()

    def _render(self):
        if self._df is None or self._df.empty:
            return
        field = self._COL_FIELDS[self._sort_col]
        df_sorted = self._df.sort_values(field, ascending=self._sort_asc).head(50)
        self._df_sorted = df_sorted.reset_index(drop=True)
        resonance_set = getattr(self, '_resonance_set', set())
        self.setRowCount(len(df_sorted))
        for i, (_, row) in enumerate(df_sorted.iterrows()):
            sector_name = str(row['行业'])
            is_resonance = sector_name in resonance_set
            display_name = f'🔥 {sector_name}' if is_resonance else sector_name
            name_item = QTableWidgetItem(display_name)
            if is_resonance:
                name_item.setForeground(QColor('#ff6633'))
            val = row['净额']
            val_item = QTableWidgetItem(f"{val:.1f}")
            val_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            chg = row['行业-涨跌幅']
            chg_item = QTableWidgetItem(f"{chg:+.2f}%")
            chg_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            heat = row.get('_heat', 0.0)
            heat_item = QTableWidgetItem(f"{heat:.2f}")
            heat_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            val_item.setForeground(QColor(RED) if val > 0 else QColor(GREEN))
            chg_item.setForeground(QColor('#ff9999') if chg > 0 else QColor('#99ffbb'))
            # heat colour: gradient from muted (cold) to gold (hot)
            heat_color = QColor('#ffcc44') if heat >= 0.7 else QColor('#f0a030') if heat >= 0.4 else QColor('#888888')
            heat_item.setForeground(heat_color)

            self.setItem(i, 0, name_item)
            self.setItem(i, 1, val_item)
            self.setItem(i, 2, chg_item)
            self.setItem(i, 3, heat_item)

    def set_resonance(self, names_set):
        """设置当前共振板块集合并重渲染。"""
        self._resonance_set = names_set
        self._render()

    def _on_cell_double_clicked(self, row, _col):
        if self._df_sorted is None or row < 0 or row >= len(self._df_sorted):
            return
        rec = self._df_sorted.iloc[row]
        name = str(rec.get('行业', ''))
        code = str(rec.get('代码', ''))
        if name and code:
            self.sector_selected.emit(name, code)


# ---------- 强势股弹窗 ----------
class Top5Dialog(QDialog):
    """板块强势股 TOP5 弹窗（非模态）。"""

    _HEADERS = ['代码', '名称', '涨跌幅', '成交额', '主力净额', '操作']

    add_to_watchlist_requested = Signal(str, str, float)  # code, name, price

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('强势股 TOP5')
        self.resize(560, 320)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowCloseButtonHint
        )
        self.setStyleSheet(f'background-color: {DARK_BG}; color: #ddd;')

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        self._title_lbl = QLabel('正在加载…')
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_lbl.setStyleSheet('color: #ccc; font-size: 13px; font-weight: bold;')
        lay.addWidget(self._title_lbl)

        self._table = QTableWidget(0, len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        configure_no_truncation(self._table)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {CHART_BG};
                alternate-background-color: #1d2d4a;
                color: #dddddd; border: none; border-radius: 6px; font-size: 12px;
            }}
            QHeaderView::section {{
                background-color: #111a30; color: #aaaaaa;
                border: none; padding: 6px; font-weight: bold; font-size: 11px;
            }}
            QTableWidget::item {{ padding: 6px 4px; }}
        """)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 75)
        self._table.setColumnWidth(2, 80)
        self._table.setColumnWidth(3, 80)
        self._table.setColumnWidth(4, 90)
        self._table.setColumnWidth(5, 72)
        lay.addWidget(self._table)

        close_btn = QPushButton('关闭')
        close_btn.setFixedWidth(80)
        close_btn.setStyleSheet(
            'QPushButton { background-color: #2a3a5a; color: #ddd; border: none;'
            ' border-radius: 4px; padding: 5px 0; font-size: 12px; }'
            'QPushButton:hover { background-color: #3a4a7a; }'
        )
        close_btn.clicked.connect(self.close)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

    def set_loading(self, sector_name):
        self.setWindowTitle(f'{sector_name} 强势股 TOP5')
        self._title_lbl.setText(f'正在获取 {sector_name} 强势股…')
        self._table.setRowCount(0)

    def set_error(self, msg):
        self._title_lbl.setText(f'获取失败: {msg}')

    def populate(self, result):
        stocks = result.get('stocks', [])
        name = result.get('sector_name', '')
        fund_ok = result.get('fund_ok', False)
        total = result.get('total_constituents', 0)
        tag = '' if fund_ok else ' [行情兜底]'
        self.setWindowTitle(f'{name} 强势股 TOP5')
        self._title_lbl.setText(f'{name} 强势股 TOP5{tag}（成分 {total} 只）')
        self._table.setRowCount(len(stocks))
        for i, s in enumerate(stocks):
            code_item = QTableWidgetItem(s.get('code', ''))
            code_item.setForeground(QColor('#aaddff'))
            name_item = QTableWidgetItem(s.get('name', ''))
            name_item.setForeground(QColor('#ffffff'))

            pct = float(s.get('pct', 0) or 0)
            pct_item = QTableWidgetItem(f'{pct:+.2f}%')
            pct_item.setForeground(QColor(RED) if pct > 0 else QColor(GREEN) if pct < 0 else QColor(MUTED))

            amount = float(s.get('amount', 0) or 0)
            if amount >= 1e8:
                amt_text = f'{amount / 1e8:.1f}亿'
            elif amount >= 1e4:
                amt_text = f'{amount / 1e4:.0f}万'
            else:
                amt_text = f'{amount:.0f}'
            amt_item = QTableWidgetItem(amt_text)
            amt_item.setForeground(QColor('#cccccc'))

            main_net = s.get('main_net')
            if main_net is not None:
                mn = float(main_net)
                if abs(mn) >= 1e8:
                    mn_text = f'{mn / 1e8:.2f}亿'
                elif abs(mn) >= 1e4:
                    mn_text = f'{mn / 1e4:.0f}万'
                else:
                    mn_text = f'{mn:.0f}'
                mn_item = QTableWidgetItem(mn_text)
                mn_item.setForeground(QColor(RED) if mn > 0 else QColor(GREEN))
            else:
                mn_item = QTableWidgetItem('—')
                mn_item.setForeground(QColor(MUTED))

            for item in (code_item, name_item, pct_item, amt_item, mn_item):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(i, 0, code_item)
            self._table.setItem(i, 1, name_item)
            self._table.setItem(i, 2, pct_item)
            self._table.setItem(i, 3, amt_item)
            self._table.setItem(i, 4, mn_item)

            # ── 操作列：加自选按钮 ──
            btn = QPushButton('加自选')
            btn.setFixedSize(62, 24)
            btn.setStyleSheet(
                'QPushButton { background-color: #2a3a5e; color: #ddd; border: 1px solid #4a6a8e; '
                'border-radius: 3px; font-size: 10px; padding: 1px 4px; }'
                'QPushButton:hover { background-color: #e94560; color: #fff; border-color: #e94560; }'
            )
            s_code = s.get('code', '')
            s_name = s.get('name', '')
            s_price = float(s.get('price', 0) or 0)
            btn.clicked.connect(lambda checked, c=s_code, n=s_name, p=s_price:
                self.add_to_watchlist_requested.emit(c, n, p))
            btn_wrap = QWidget()
            btn_wrap.setStyleSheet('background: transparent;')
            wrap_lay = QHBoxLayout(btn_wrap)
            wrap_lay.setContentsMargins(4, 2, 4, 2)
            wrap_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            wrap_lay.addWidget(btn)
            self._table.setCellWidget(i, 5, btn_wrap)


# ---------- 自选板块编辑对话框（P0-A）----------
class _CustomSectorDialog(QDialog):
    """让用户从预置清单勾选自选板块，也可手动输入任意板块名。"""

    _STYLE = (
        'QDialog { background-color: #1a1a2e; }'
        'QLabel { color: #ccc; font-size: 12px; }'
        'QLineEdit { background: #252540; color: #ddd; border: 1px solid #2f4b7c;'
        '  border-radius: 4px; padding: 4px 8px; font-size: 12px; }'
        'QListWidget { background: #252540; color: #ddd; border: 1px solid #2f4b7c;'
        '  border-radius: 4px; font-size: 12px; }'
        'QListWidget::item:hover { background: #2a3a6a; }'
        'QListWidget::item:selected { background: #1e5288; }'
        'QPushButton { background-color: #2a3a5a; color: #ddd; border: none;'
        '  border-radius: 4px; padding: 5px 14px; font-size: 12px; }'
        'QPushButton:hover { background-color: #3a4a7a; }'
        'QPushButton:disabled { color: #666; background-color: #1a2a3a; }'
    )

    def __init__(self, preset_list, selected, parent=None):
        super().__init__(parent)
        self.setWindowTitle('编辑自选板块')
        self.resize(580, 460)
        self.setStyleSheet(self._STYLE)
        self._selected = list(selected)  # 当前已选（可能含用户手动输入的名称）

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 14, 16, 14)

        # 顶部说明
        hint = QLabel(
            '从预置清单勾选，或在底部输入框添加自定义板块名；'
            '名称会通过模糊匹配映射到当前东财板块库。'
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        # 搜索框
        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText('搜索预置清单（输入关键词过滤）')
        self._search_box.textChanged.connect(self._on_search)
        root.addWidget(self._search_box)

        # 左右双列布局：预置清单 | 已选清单
        columns = QHBoxLayout()
        columns.setSpacing(12)

        left_box = QVBoxLayout()
        left_box.addWidget(QLabel('预置细分概念清单'))
        self._preset_list_widget = QListWidget()
        self._preset_list_widget.setSelectionMode(
            QListWidget.SelectionMode.MultiSelection
        )
        self._all_preset = list(preset_list)
        self._populate_preset(self._all_preset)
        left_box.addWidget(self._preset_list_widget)

        btn_row_l = QHBoxLayout()
        add_btn = QPushButton('→ 加入已选')
        add_btn.clicked.connect(self._add_from_preset)
        btn_row_l.addWidget(add_btn)
        btn_row_l.addStretch()
        left_box.addLayout(btn_row_l)

        right_box = QVBoxLayout()
        right_box.addWidget(QLabel('已选板块（支持手动编辑）'))
        self._selected_widget = QListWidget()
        self._selected_widget.setSelectionMode(
            QListWidget.SelectionMode.MultiSelection
        )
        self._refresh_selected_widget()
        right_box.addWidget(self._selected_widget)

        btn_row_r = QHBoxLayout()
        remove_btn = QPushButton('← 移除')
        remove_btn.clicked.connect(self._remove_selected)
        clear_btn = QPushButton('清空')
        clear_btn.clicked.connect(self._clear_all)
        btn_row_r.addWidget(remove_btn)
        btn_row_r.addWidget(clear_btn)
        btn_row_r.addStretch()
        right_box.addLayout(btn_row_r)

        columns.addLayout(left_box, 1)
        columns.addLayout(right_box, 1)
        root.addLayout(columns)

        # 手动输入行
        input_row = QHBoxLayout()
        input_lbl = QLabel('手动添加：')
        self._manual_input = QLineEdit()
        self._manual_input.setPlaceholderText('输入任意板块名（如：液冷服务器）后按「添加」')
        self._manual_input.returnPressed.connect(self._add_manual)
        add_manual_btn = QPushButton('添加')
        add_manual_btn.setFixedWidth(60)
        add_manual_btn.clicked.connect(self._add_manual)
        input_row.addWidget(input_lbl)
        input_row.addWidget(self._manual_input, 1)
        input_row.addWidget(add_manual_btn)
        root.addLayout(input_row)

        # 确认/取消
        footer = QHBoxLayout()
        footer.addStretch()
        ok_btn = QPushButton('确认（将触发折线刷新）')
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton('取消')
        cancel_btn.clicked.connect(self.reject)
        footer.addWidget(ok_btn)
        footer.addWidget(cancel_btn)
        root.addLayout(footer)

    def _populate_preset(self, items):
        self._preset_list_widget.clear()
        for name in items:
            item = QListWidgetItem(name)
            if name in self._selected:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)
            self._preset_list_widget.addItem(item)

    def _refresh_selected_widget(self):
        self._selected_widget.clear()
        for name in self._selected:
            self._selected_widget.addItem(name)

    def _on_search(self, text):
        kw = text.strip().lower()
        filtered = [n for n in self._all_preset if kw in n.lower()] if kw else self._all_preset
        self._populate_preset(filtered)

    def _add_from_preset(self):
        for i in range(self._preset_list_widget.count()):
            item = self._preset_list_widget.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                name = item.text()
                if name not in self._selected:
                    self._selected.append(name)
        self._refresh_selected_widget()

    def _remove_selected(self):
        to_remove = {
            self._selected_widget.item(i).text()
            for i in range(self._selected_widget.count())
            if self._selected_widget.item(i).isSelected()
        }
        self._selected = [s for s in self._selected if s not in to_remove]
        self._refresh_selected_widget()
        # 同步预置列表 checkState
        for i in range(self._preset_list_widget.count()):
            item = self._preset_list_widget.item(i)
            if item.text() in to_remove:
                item.setCheckState(Qt.CheckState.Unchecked)

    def _clear_all(self):
        self._selected.clear()
        self._refresh_selected_widget()
        for i in range(self._preset_list_widget.count()):
            self._preset_list_widget.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _add_manual(self):
        text = self._manual_input.text().strip()
        if not text:
            return
        if text not in self._selected:
            self._selected.append(text)
            self._refresh_selected_widget()
        self._manual_input.clear()

    def get_selected(self):
        return list(self._selected)


# ---------- 板块面板 ----------
class SectorPanel(QWidget):
    """行业板块资金流面板。"""

    # 信号：把状态/数据点数/底部状态栏文本上抛给 MainWindow
    status_changed = Signal(str, str)        # level: 'loading'|'success'|'error', text
    point_count_changed = Signal(int)
    bottom_status_changed = Signal(str)
    request_add_watchlist = Signal(str, str, float)  # code, name, price → 交给个股页弹「添加自选股」

    def __init__(self, parent=None):
        super().__init__(parent)
        self.kind = 'industry'
        self.period = 'full'
        self.histories = {kind: [] for kind in _KINDS}
        self.current_dates = {kind: None for kind in _KINDS}
        self.workers = {}
        self.latest_dfs = {}
        self.tracked_sectors = {kind: [] for kind in _KINDS}
        self.minute_histories = {kind: [] for kind in _KINDS}
        self._minute_history_attempted = {kind: False for kind in _KINDS}
        self._last_minute_history_fetch = {kind: None for kind in _KINDS}
        self._has_bar_data = {kind: False for kind in _KINDS}
        self._prev_top3 = {kind: set() for kind in _KINDS}
        self._resonance_names = {kind: set() for kind in _KINDS}
        self._resonance_worker = None
        self.alert_center = None  # injected by MainWindow

        # P0-A：显示模式 + 自选清单（持久化于 ~/.aldebaran/ui_config.json）
        cfg = load_ui_config()
        self.display_mode = cfg.get('sector_display_mode', DISPLAY_MODE_FOCUS)
        if self.display_mode not in dict(_DISPLAY_MODE_LABELS):
            self.display_mode = DISPLAY_MODE_FOCUS
        self.custom_sectors = list(cfg.get('sector_custom_list') or [])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(14, 10, 14, 8)
        toolbar.setSpacing(8)
        title = QLabel('💰 板块资金流')
        title.setStyleSheet('color: #ffffff; font-size: 16px; font-weight: bold;')
        toolbar.addWidget(title)
        toolbar.addStretch(1)
        kind_label = QLabel('口径')
        kind_label.setStyleSheet(f'color: {MUTED}; font-size: 12px;')
        toolbar.addWidget(kind_label)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem('概念板块', 'concept')
        self.kind_combo.addItem('行业板块', 'industry')
        self.kind_combo.setCurrentIndex(1)
        self.kind_combo.setFixedWidth(110)
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        toolbar.addWidget(self.kind_combo)

        # P0-A：显示模式 + 自选编辑
        display_label = QLabel('显示')
        display_label.setStyleSheet(f'color: {MUTED}; font-size: 12px;')
        toolbar.addWidget(display_label)
        self.display_combo = QComboBox()
        for value, label in _DISPLAY_MODE_LABELS:
            self.display_combo.addItem(label, value)
        # 设置当前选中项
        for i in range(self.display_combo.count()):
            if self.display_combo.itemData(i) == self.display_mode:
                self.display_combo.setCurrentIndex(i)
                break
        self.display_combo.setFixedWidth(110)
        self.display_combo.setToolTip(
            'FOCUS默认：FOCUS关键词+净流入TOP10+净流出TOP5\n'
            'TOP5/10/20：按净流入绝对值排序\n'
            '自选：用户编辑的板块清单'
        )
        self.display_combo.currentIndexChanged.connect(self._on_display_mode_changed)
        toolbar.addWidget(self.display_combo)
        self.edit_custom_btn = QPushButton('⚙ 编辑自选')
        self.edit_custom_btn.setFixedHeight(26)
        self.edit_custom_btn.setStyleSheet(
            'QPushButton { background-color: #2a3a5a; color: #ddd; border: none;'
            ' border-radius: 4px; padding: 2px 10px; font-size: 12px; }'
            'QPushButton:hover { background-color: #3a4a7a; }'
        )
        self.edit_custom_btn.clicked.connect(self._open_custom_sector_editor)
        toolbar.addWidget(self.edit_custom_btn)

        period_label = QLabel('时段')
        period_label.setStyleSheet(f'color: {MUTED}; font-size: 12px;')
        toolbar.addWidget(period_label)
        self.period_combo = QComboBox()
        self.period_combo.addItem('全天', 'full')
        self.period_combo.addItem('上午', 'morning')
        self.period_combo.addItem('下午', 'afternoon')
        self.period_combo.setFixedWidth(80)
        self.period_combo.currentIndexChanged.connect(self._on_period_changed)
        toolbar.addWidget(self.period_combo)
        self._period_label = period_label
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background-color: #333; width: 3px; }")

        self.tabs = QTabWidget()
        self.line_canvas = LineChartCanvas()
        self.bar_canvas = BarChartCanvas()
        self.tabs.addTab(self.line_canvas, "实时走势")
        self.tabs.addTab(self.bar_canvas, "资金排名")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        splitter.addWidget(self.tabs)

        # 右侧容器：排行表格
        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.table = RankTable()
        self.table.sector_selected.connect(self._on_sector_selected)
        right_layout.addWidget(self.table)

        hint = QLabel('双击板块查看强势股 TOP5')
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(f'color: {MUTED}; font-size: 10px; padding: 2px;')
        right_layout.addWidget(hint)

        self._top5_worker = None
        self._top5_dialog = None
        splitter.addWidget(right_container)

        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

        # 非交易时间默认显示柱状图
        if not is_trading_time():
            self.tabs.setCurrentIndex(1)
        self._on_tab_changed(self.tabs.currentIndex())

        # 修复 #3：载入今日缓存（概念/行业分别载入）
        today = datetime.now().date()
        for kind in _KINDS:
            cached = load_sector_history(today, kind)
            if cached:
                self.histories[kind] = cached
                self.current_dates[kind] = today
            minute_cached = load_sector_minute_history(today, kind)
            if minute_cached:
                self.minute_histories[kind] = minute_cached
                self._minute_history_attempted[kind] = True
                self.current_dates[kind] = today
            tracked = load_sector_tracked(today, kind)
            if tracked:
                self.tracked_sectors[kind] = _filter_tracked_sector_list(tracked)
            # 冻结态重开：载入最新 df，让资金排名/表格立刻有内容
            latest_records = load_sector_latest_df(today, kind)
            if latest_records:
                df = pd.DataFrame(latest_records)
                if not df.empty and '净额' in df.columns and '行业' in df.columns:
                    df['净额'] = pd.to_numeric(df['净额'], errors='coerce')
                    if '行业-涨跌幅' in df.columns:
                        df['行业-涨跌幅'] = pd.to_numeric(df['行业-涨跌幅'], errors='coerce').fillna(0)
                    else:
                        df['行业-涨跌幅'] = 0.0
                    df = df.dropna(subset=['净额'])
                    if not df.empty:
                        self.latest_dfs[kind] = df
                        self._has_bar_data[kind] = True
                        self.current_dates[kind] = today
        if is_trading_time():
            self._show_minute_loading()
        else:
            self._render_frozen_state()

    # ---- 公共 API ----
    def refresh(self):
        """手动刷新（任何时间均可触发，启动时也调用）"""
        self._fetch()

    def auto_refresh(self):
        """自动刷新：仅交易时间，避免非盘中重复抓取（修复 #9 之一）"""
        if is_trading_time():
            self._fetch()

    def get_market_context(self) -> dict:
        """返回当前板块资金的只读快照，供 IntelPanel 查询。

        格式: {'concept': {板块名: {'net': 净额(亿), 'pct': 涨跌幅(%)}}, ...}
        不持有 DataFrame 引用，调用方无法修改原始数据。
        """
        result: dict = {}
        for kind in ('concept', 'industry'):
            df = self.latest_dfs.get(kind)
            if df is not None and not df.empty:
                names = df['行业'].tolist()
                nets = df['净额'].tolist()
                pcts = df['行业-涨跌幅'].tolist() if '行业-涨跌幅' in df.columns else [0.0] * len(names)
                result[kind] = {
                    str(n): {'net': float(v or 0), 'pct': float(p or 0)}
                    for n, v, p in zip(names, nets, pcts)
                }
            else:
                result[kind] = {}
        return result

    def current_point_count(self):
        if (
            is_trading_time()
            and self.histories.get(self.kind)
            and not self.minute_histories.get(self.kind)
            and not self._minute_history_attempted.get(self.kind)
        ):
            return 0
        return len(self._chart_history(self.kind))

    def _render_frozen_state(self):
        self._render_current()
        history, _date, stale_label = self._effective_chart_history(self.kind)
        self.point_count_changed.emit(len(history))
        if history:
            last_time = history[-1][0].strftime("%H:%M:%S")
            stale_tag = f' [{stale_label}]' if stale_label else ''
            self.status_changed.emit('success', f"上次刷新: {last_time} [已冻结]{stale_tag}")
            self.bottom_status_changed.emit(
                f"已冻结，显示{'上交易日' if stale_label else '本地'}分时缓存"
                f" | {self._kind_label()}板块 | 折线 {len(history)} 个分钟点"
            )
        else:
            self.status_changed.emit('success', '已冻结，暂无本地分时缓存')
            self.bottom_status_changed.emit('已冻结，当前没有可显示的本地板块分时缓存')

    def _kind_label(self):
        return _KIND_NAMES.get(self.kind, '板块')

    def _show_minute_loading(self):
        today = datetime.now()
        title = f'{today.strftime("%m月%d日")} {self._kind_label()}板块资金分时流向'
        cached_points = len(self.histories.get(self.kind, []))
        cached_text = f'\n已载入本地快照缓存 {cached_points} 个点，等待分钟分时替换' if cached_points else ''
        self.line_canvas.show_message(
            title,
            f'正在连接东财1分钟资金分时接口...\n正在同步概念/行业板块数据{cached_text}',
        )

    def _on_kind_changed(self, _idx=None):
        new_kind = self.kind_combo.currentData()
        if new_kind == self.kind:
            return
        self.kind = new_kind
        self.line_canvas.reset_axis()
        if not is_trading_time():
            self._render_frozen_state()
            return
        self._render_current()
        self.point_count_changed.emit(self.current_point_count())
        self._fetch()

    def _on_tab_changed(self, index):
        """切换 tab 时隐藏/显示时段选项（时段仅对实时走势有意义）"""
        show_period = (index == 0)
        self._period_label.setVisible(show_period)
        self.period_combo.setVisible(show_period)
        if index == 1:
            # 延迟 80ms：让 Qt 完成布局尺寸计算后再 tight_layout，避免初次渲染挤压
            QTimer.singleShot(80, self._redraw_bar)

    def _on_period_changed(self, _idx=None):
        new_period = self.period_combo.currentData()
        if new_period == self.period:
            return
        self.period = new_period
        self.line_canvas.reset_axis()
        if not is_trading_time():
            self._render_frozen_state()
            return
        self._render_current()

    def _on_display_mode_changed(self, _idx=None):
        """切换 FOCUS / TOPn / 自选 显示模式。"""
        new_mode = self.display_combo.currentData()
        if new_mode == self.display_mode:
            return
        if new_mode == DISPLAY_MODE_CUSTOM and not self.custom_sectors:
            # 自选清单为空：弹编辑器
            QMessageBox.information(
                self, '自选清单为空',
                '当前自选清单为空，请先点击「编辑自选」添加板块。'
            )
            # 恢复到原模式
            for i in range(self.display_combo.count()):
                if self.display_combo.itemData(i) == self.display_mode:
                    self.display_combo.blockSignals(True)
                    self.display_combo.setCurrentIndex(i)
                    self.display_combo.blockSignals(False)
                    break
            self._open_custom_sector_editor()
            return
        self.display_mode = new_mode
        update_ui_config(sector_display_mode=new_mode)
        self._apply_mode_change()

    def _open_custom_sector_editor(self):
        """打开自选板块编辑对话框。"""
        dialog = _CustomSectorDialog(
            preset_list=_PRESET_CONCEPT_LIST,
            selected=self.custom_sectors,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.custom_sectors = dialog.get_selected()
            update_ui_config(sector_custom_list=self.custom_sectors)
            if self.display_mode == DISPLAY_MODE_CUSTOM:
                self._apply_mode_change()

    def _apply_mode_change(self):
        """显示模式或自选清单变更后，重建 tracked 列表并触发重渲染。"""
        # 用当前已缓存的 df 立即重算 tracked，无需等下次刷新
        now = datetime.now()
        for kind in _KINDS:
            df = self.latest_dfs.get(kind)
            if df is None or df.empty:
                continue
            new_tracked = _build_tracked_sector_list(
                df, [], now,
                mode=self.display_mode,
                custom_sectors=self.custom_sectors,
            )
            if new_tracked != self.tracked_sectors.get(kind):
                # 切换 tracked 时清掉分钟缓存，下次刷新会重拉
                self.minute_histories[kind] = []
                self._last_minute_history_fetch[kind] = None
                self.tracked_sectors[kind] = new_tracked
                save_sector_tracked(new_tracked, now.date(), kind)
        # 立即重渲染当前可见面板
        self.line_canvas.reset_axis()
        if not is_trading_time():
            self._render_frozen_state()
        else:
            self._render_current()
        # 触发一次后台刷新，拉取新 tracked 的分钟序列
        QTimer.singleShot(50, self._fetch)

    def _redraw_bar(self):
        """强制 bar_canvas 重新布局并渲染（用于 tab 切换后延迟触发）。"""
        self.bar_canvas.fig.tight_layout()
        self.bar_canvas.draw_idle()

    # ---- 内部 ----
    def _effective_chart_history(self, kind):
        """今日分时不足时，降级为最近可用交易日的缓存，返回 (history, date, stale_label)。"""
        today = datetime.now().date()
        history = self._chart_history(kind)
        if len(history) >= 2:
            return history, self.current_dates.get(kind) or today, ''
        for days_back in range(1, 6):
            past = today - timedelta(days=days_back)
            past_history = load_sector_minute_history(past, kind)
            if len(past_history) >= 2:
                label = '昨日参考' if days_back == 1 else f'{days_back}日前参考'
                return past_history, past, label
        return history, self.current_dates.get(kind) or today, ''

    def _render_current(self):
        today = datetime.now().date()
        df = self.latest_dfs.get(self.kind)
        if df is not None and not df.empty:
            now_str = datetime.now().strftime("%m月%d日 %H:%M")
            self.bar_canvas.update_chart(
                self._build_bar_data(df),
                now_str,
                kind_label=self._kind_label(),
            )
            self.table.update_data(df)
            self._has_bar_data[self.kind] = True
        if (
            self.histories.get(self.kind)
            and not self.minute_histories.get(self.kind)
            and not self._minute_history_attempted.get(self.kind)
            and is_trading_time()
        ):
            self._show_minute_loading()
            return
        history, data_date, stale_label = self._effective_chart_history(self.kind)
        self.line_canvas.update_chart(
            history,
            data_date=data_date,
            kind_label=self._kind_label(),
            tracked_sectors=self.tracked_sectors.get(self.kind),
            period=self.period,
            stale_label=stale_label,
        )

    def _chart_history(self, kind):
        minute = self.minute_histories.get(kind)
        if minute:
            minute = self._extend_lunch_close_point(kind, minute)
            return self._merge_missing_snapshot_sectors(kind, minute)
        snapshots = self.histories.get(kind, [])
        if not snapshots:
            return []
        bucket = {}
        order = []
        for t, snap in snapshots:
            key = t.replace(second=0, microsecond=0)
            if key not in bucket:
                order.append(key)
            bucket[key] = (t, snap)
        history = [bucket[k] for k in order]
        tracked = self.tracked_sectors.get(kind) or []
        if not tracked:
            return history
        filled = []
        last_values = {}
        prev_snap = None
        for t, snap in history:
            current = dict(snap)
            if _is_anomalous_snapshot(prev_snap, current, tracked):
                continue
            for sector in tracked:
                if sector in current:
                    last_values[sector] = current[sector]
                elif sector in last_values:
                    current[sector] = last_values[sector]
            filled.append((t, current))
            prev_snap = current
        return self._extend_lunch_close_point(kind, filled)

    def _merge_missing_snapshot_sectors(self, kind, minute_history):
        tracked = self.tracked_sectors.get(kind) or []
        if not tracked:
            return minute_history
        present = set()
        for _, snap in minute_history:
            present.update(snap.keys())
        missing = [sector for sector in tracked if sector not in present]
        if not missing:
            return minute_history
        snapshots = self.histories.get(kind, [])
        if not snapshots:
            return minute_history
        snap_by_minute = {}
        for t, snap in snapshots:
            key = t.replace(second=0, microsecond=0)
            snap_by_minute[key] = snap
        merged = []
        last_values = {}
        for t, snap in minute_history:
            current = dict(snap)
            fallback = snap_by_minute.get(t.replace(second=0, microsecond=0), {})
            for sector in missing:
                if sector in fallback:
                    last_values[sector] = fallback[sector]
                if sector in last_values:
                    current[sector] = last_values[sector]
            merged.append((t, current))
        return merged

    def _extend_lunch_close_point(self, kind, history):
        if not history:
            return history
        now = datetime.now()
        if _minute_of_day(now) < _AM_CLOSE:
            return history
        last_t, _ = history[-1]
        if last_t.date() != now.date() or _minute_of_day(last_t) >= _AM_CLOSE:
            return history
        df = self.latest_dfs.get(kind)
        if df is None or df.empty or '行业' not in df.columns or '净额' not in df.columns:
            return history
        snap = dict(zip(df['行业'], df['净额']))
        if not snap:
            return history
        close_t = last_t.replace(hour=11, minute=30, second=0, microsecond=0)
        return list(history) + [(close_t, snap)]

    def _should_fetch_minute_history(self, kind, now):
        if not self.minute_histories.get(kind):
            return True
        last = self._last_minute_history_fetch.get(kind)
        return last is None or (now - last).total_seconds() >= 55

    def _fetch(self):
        to_start = []
        for kind in _KINDS:
            worker = self.workers.get(kind)
            if not (worker and worker.isRunning()):
                to_start.append(kind)
        if not to_start:
            return

        self.status_changed.emit('loading', '正在同步获取概念/行业板块分钟分时...')
        now = datetime.now()
        mode = self.display_mode
        custom = list(self.custom_sectors)
        for kind in to_start:
            existing_tracked = (
                self.tracked_sectors.get(kind)
                if self.current_dates.get(kind) == now.date()
                else []
            )
            fetch_minute = self._should_fetch_minute_history(kind, now)
            worker = DataWorker(
                fetcher=lambda k=kind, t=existing_tracked, mh=fetch_minute, md=mode, cs=custom:
                    _fetch_sector_payload(k, t, mh, mode=md, custom_sectors=cs)
            )
            worker.data_ready.connect(self._on_data_ready)
            worker.error_occurred.connect(lambda msg, k=kind: self._on_error(msg, k))
            self.workers[kind] = worker
            worker.start()

    def _on_data_ready(self, payload):
        if len(payload) == 4:
            kind, df, tracked, minute_history = payload
        else:
            kind, df = payload
            tracked = []
            minute_history = []
        now = datetime.now()
        df['净额'] = pd.to_numeric(df['净额'], errors='coerce')
        df['行业-涨跌幅'] = pd.to_numeric(df['行业-涨跌幅'], errors='coerce')
        df = df.dropna(subset=['净额'])
        df['行业-涨跌幅'] = df['行业-涨跌幅'].fillna(0)
        df = df.drop_duplicates(subset=['行业'], keep='first')
        df = _filter_sector_df(df)
        if df.empty:
            self._on_error("返回数据为空", kind)
            return

        # 跨日重置
        if self.current_dates.get(kind) != now.date():
            self.histories[kind] = []
            self.current_dates[kind] = now.date()
            self.tracked_sectors[kind] = []
            self.minute_histories[kind] = []
            self._minute_history_attempted[kind] = False
            self._last_minute_history_fetch[kind] = None
            self._has_bar_data[kind] = False
            self._prev_top3[kind] = set()
            self._resonance_names[kind] = set()
            if kind == self.kind:
                self.line_canvas.reset_axis()

        if tracked:
            old_tracked = set(self.tracked_sectors.get(kind, []))
            new_tracked = set(tracked)
            if old_tracked and new_tracked != old_tracked:
                self.minute_histories[kind] = []
                self._last_minute_history_fetch[kind] = None
            self.tracked_sectors[kind] = tracked
            save_sector_tracked(tracked, now.date(), kind)
        else:
            tracked = _build_tracked_sector_list(
                df, self.tracked_sectors.get(kind), now,
                mode=self.display_mode,
                custom_sectors=self.custom_sectors,
            )
            if tracked:
                self.tracked_sectors[kind] = tracked
                save_sector_tracked(tracked, now.date(), kind)

        if minute_history:
            self.minute_histories[kind] = minute_history
            self._last_minute_history_fetch[kind] = now
            save_sector_minute_history(minute_history, now.date(), kind)
        self._minute_history_attempted[kind] = True

        # 仅交易时间记录折线点 + 写缓存（修复 #3）
        if is_trading_time(now):
            snapshot = dict(zip(df['行业'], df['净额']))
            self.histories[kind].append((now, snapshot))
            save_sector_history(self.histories[kind], self.current_dates[kind], kind)

        self.latest_dfs[kind] = df
        save_sector_latest_df(df, now.date(), kind)

        # 交易时段：板块 TOP3 突破提醒 + 资金共振后台检查（所有 kind 均需检测）
        if is_trading_time(now):
            self._check_sector_top3_alerts(df, kind)
            self._trigger_resonance_check(df, kind)

        if kind != self.kind:
            return

        chart_history, chart_date, stale_label = self._effective_chart_history(kind)
        self.point_count_changed.emit(len(chart_history))

        # 折线图始终重渲染（即使非交易时间也展示当日已有数据）
        self.line_canvas.update_chart(
            chart_history,
            data_date=chart_date,
            kind_label=self._kind_label(),
            tracked_sectors=self.tracked_sectors.get(kind),
            period=self.period,
            stale_label=stale_label,
        )

        # 柱状图：午休时不更新已有数据（修复 #9）
        if not (is_lunch_break(now) and self._has_bar_data[kind]):
            df_plot = self._build_bar_data(df)
            now_str = now.strftime("%m月%d日 %H:%M")
            self.bar_canvas.update_chart(df_plot, now_str, kind_label=self._kind_label())
            self._has_bar_data[kind] = True

        # 表格始终更新
        self.table.update_data(df)

        # 恢复当前 kind 的共振标记（表格重渲染后需要重新应用）
        if self._resonance_names.get(kind):
            self.table.set_resonance(self._resonance_names[kind])

        now_t = now.strftime("%H:%M:%S")
        frozen = '' if is_trading_time(now) else ' [已冻结]'
        self.status_changed.emit('success', f"上次刷新: {now_t}{frozen}")
        try:
            top_row = df.loc[df['净额'].idxmax()]
            top_info = f" | 净流入最大: {top_row['行业']} {top_row['净额']:.1f}亿"
        except (ValueError, KeyError):
            top_info = ''
        self.bottom_status_changed.emit(
            f"数据更新于 {now_t} | 共 {len(df)} 个{self._kind_label()}板块 | "
            f"折线 {len(chart_history)} 个分钟点{top_info}{frozen}"
        )

    def _on_error(self, msg, kind=None):
        kind = kind or self.kind
        if kind != self.kind:
            self.bottom_status_changed.emit(f"{_KIND_NAMES.get(kind, '板块')}后台刷新失败: {msg}")
            return

        has_current_data = (
            self.histories.get(kind)
            or self._has_bar_data.get(kind)
            or self.table.rowCount() > 0
        )
        if has_current_data:
            self.status_changed.emit('success', '刷新失败，显示上次数据')
        else:
            self.status_changed.emit('error', '获取失败')
        self.bottom_status_changed.emit(f"错误: {msg}")

    # ---- 板块 TOP3 突破提醒 + 资金共振 ----
    def _check_sector_top3_alerts(self, df, kind):
        """检测板块净流入新进 TOP3，触发提醒。

        改进：
        1. 每交易时段头 open_silence_minutes 分钟内跳过（避免开盘集中爆发）
        2. 每轮最多推送 1 条（净流入最大），其余写日志不弹窗
        """
        ac = getattr(self, 'alert_center', None)
        if not ac or not ac.get_setting('sector_top3_enabled', True):
            return

        # ① 开盘/午盘静默期
        silence = int(ac.get_setting('open_silence_minutes', 5))
        if silence > 0:
            now = datetime.now()
            hm = now.hour * 60 + now.minute
            morning = 9 * 60 + 30
            afternoon = 13 * 60 + 0
            if (morning <= hm < morning + silence or
                    afternoon <= hm < afternoon + silence):
                return

        min_net = ac.get_setting('sector_top3_min_net', 3.0)
        cooldown = ac.get_setting('sector_top3_cooldown', 900)
        try:
            top3_df = df.sort_values('净额', ascending=False).head(3)
            current_top3 = set(top3_df['行业'].tolist())
        except Exception:
            return
        prev_top3 = self._prev_top3.get(kind, set())
        new_names = current_top3 - prev_top3
        self._prev_top3[kind] = current_top3

        # ② 收集满足门槛的新进板块，按净额排序
        candidates = []
        for name in new_names:
            try:
                net = float(df.loc[df['行业'] == name, '净额'].iloc[0])
            except Exception:
                continue
            if net >= min_net:
                candidates.append((name, net))
        if not candidates:
            return
        candidates.sort(key=lambda x: x[1], reverse=True)

        # ③ 只向用户推送净流入最大的 1 条，其余静默写日志
        for idx, (name, net) in enumerate(candidates):
            if idx == 0:
                ac.try_trigger(
                    event_type='sector_top3',
                    title=f'📊 板块冲入TOP3: {name}',
                    message=f'{name} 净流入 {net:.1f}亿，新进 TOP3',
                    level='warning',
                    key=f'sector_top3:{name}',
                    cooldown=cooldown,
                )
            else:
                ac._append_log({
                    'type': 'sector_top3',
                    'title': f'📊 板块冲入TOP3: {name}',
                    'message': f'{name} 净流入 {net:.1f}亿，新进 TOP3（同轮静默）',
                    'level': 'info',
                    'key': f'sector_top3:{name}',
                    'payload': {},
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                })

    def _trigger_resonance_check(self, df, kind):
        """对 TOP3 高净流入板块启动后台共振检查（每次最多1个 worker）。"""
        ac = getattr(self, 'alert_center', None)
        if not ac or not ac.get_setting('resonance_enabled', True):
            return
        if self._resonance_worker and self._resonance_worker.isRunning():
            return
        min_rank = ac.get_setting('resonance_min_rank', 3)
        min_net = ac.get_setting('resonance_min_net', 5.0)
        try:
            top_df = df.sort_values('净额', ascending=False).head(min_rank)
        except Exception:
            return
        candidates = [
            {'name': str(row['行业']), 'code': str(row.get('代码', '')), 'net': float(row['净额'])}
            for _, row in top_df.iterrows()
            if float(row['净额']) >= min_net and str(row.get('代码', ''))
        ]
        if not candidates:
            return
        self._resonance_worker = DataWorker(
            fetcher=lambda: _check_resonance_for_sectors(candidates)
        )
        _kind = kind
        self._resonance_worker.data_ready.connect(
            lambda r: self._on_resonance_ready(r, _kind)
        )
        self._resonance_worker.error_occurred.connect(self._on_resonance_error)
        self._resonance_worker.start()

    def _on_resonance_ready(self, results, kind):
        ac = getattr(self, 'alert_center', None)
        min_zt = ac.get_setting('resonance_min_zt', 5) if ac else 5
        cooldown = ac.get_setting('resonance_cooldown', 300) if ac else 300
        resonance_names = set()
        for r in results:
            if r['zt_count'] >= min_zt:
                resonance_names.add(r['sector_name'])
                if ac:
                    ac.try_trigger(
                        event_type='resonance',
                        title=f'🔥 资金共振: {r["sector_name"]}',
                        message=f'{r["sector_name"]} 净流入 {r["net"]:.1f}亿 | 涨停 {r["zt_count"]} 只',
                        level='critical',
                        key=f'resonance:{r["sector_code"]}',
                        cooldown=cooldown,
                    )
        self._resonance_names[kind] = resonance_names
        if kind == self.kind:
            self.table.set_resonance(resonance_names)

    def _on_resonance_error(self, msg):
        pass  # 共振检查失败静默处理，不影响主面板

    # ---- TOP5 联动 ----
    def _on_sector_selected(self, name, code):
        """板块双击 → 弹出 TOP5 强势股窗口。"""
        if self._top5_dialog is None or not self._top5_dialog.isVisible():
            self._top5_dialog = Top5Dialog(parent=None)
            self._top5_dialog.add_to_watchlist_requested.connect(self._on_add_to_watchlist_from_sector)
        self._top5_dialog.set_loading(name)
        self._top5_dialog.show()
        self._top5_dialog.raise_()
        if self._top5_worker and self._top5_worker.isRunning():
            return
        self._top5_worker = DataWorker(
            fetcher=lambda: _fetch_sector_top5_stocks(name, code)
        )
        self._top5_worker.data_ready.connect(self._on_top5_ready)
        self._top5_worker.error_occurred.connect(self._on_top5_error)
        self._top5_worker.start()

    def _on_top5_ready(self, result):
        if self._top5_dialog:
            self._top5_dialog.populate(result)

    def _on_top5_error(self, msg):
        if self._top5_dialog:
            self._top5_dialog.set_error(msg)

    def _on_add_to_watchlist_from_sector(self, code, name, price):
        """板块强势股弹窗 → 交给个股页弹「添加自选股」对话框（与自选页加自选一致）。"""
        self.request_add_watchlist.emit(code, name or '', float(price or 0))

    @staticmethod
    def _build_bar_data(df):
        """严格 top20 + 底10（修复 #1）：数据≤30 时取全集，否则严格切片，
        因为按净额排序后前20和后10的索引必然不重叠。"""
        df = _filter_sector_df(df)
        sorted_df = df.sort_values('净额', ascending=False).reset_index(drop=True)
        n = len(sorted_df)
        if n <= 30:
            return sorted_df.sort_values('净额')
        return pd.concat([sorted_df.iloc[:20], sorted_df.iloc[-10:]]).sort_values('净额')
