"""解禁日历查询。

查询指定股票未来 30 天内的限售股解禁计划。
返回 dict 或 None（无解禁 / 数据不可得）。
"""
from __future__ import annotations

from datetime import date, timedelta


def fetch_restricted_release(code: str) -> dict | None:
    """查询该股未来 30 天解禁情况。

    Returns:
        dict with keys: release_date, volume(万股), ratio(%), type, summary
        None 表示无解禁或数据获取失败。
    """
    try:
        import akshare as ak
        today = date.today()
        end = today + timedelta(days=30)
        df = ak.stock_restricted_release_detail_em(
            start_date=today.strftime('%Y%m%d'),
            end_date=end.strftime('%Y%m%d'),
        )
        if df is None or df.empty:
            return None

        # 匹配股票代码列（akshare 返回字段名可能含空格，做 strip）
        code_col = next((c for c in df.columns if '代码' in c), None)
        if code_col is None:
            return None
        mask = df[code_col].astype(str).str.strip() == str(code).strip()
        sub = df[mask]
        if sub.empty:
            return None

        row = sub.iloc[0]

        def _get(keys: list[str], default='') -> str:
            for k in keys:
                for col in df.columns:
                    if k in col:
                        v = row.get(col, default)
                        return str(v).strip() if v is not None else default
            return default

        release_date = _get(['解禁时间', '解禁日期'])
        volume_raw = _get(['解禁数量'])
        ratio_raw = _get(['解禁比例', '占总股本'])
        rtype = _get(['上市类型', '限售股类型'])

        try:
            volume = float(volume_raw.replace(',', '') or 0) / 10000  # 股 → 万股
        except Exception:
            volume = 0.0

        try:
            ratio = float(str(ratio_raw).replace('%', '').replace(',', '') or 0)
        except Exception:
            ratio = 0.0

        summary = (
            f'解禁预警：{release_date} 解禁 {volume:.0f} 万股'
            f'（占流通盘 {ratio:.1f}%），类型：{rtype}'
        )
        return {
            'release_date': release_date,
            'volume': volume,
            'ratio': ratio,
            'type': rtype,
            'summary': summary,
        }
    except Exception:
        return None
