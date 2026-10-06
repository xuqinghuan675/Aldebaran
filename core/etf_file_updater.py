"""TickFlow 一键刷新客户两个 xlsm 的「实时数据」单元格（就地改，保宏/按钮/公式）。

实现方式：**zip/XML 外科手术**——只重写目标 worksheet 的 XML 单元格，其余所有
部件（drawings/按钮、vbaProject、printerSettings、其它 sheet…）原样字节拷贝。
不能用 openpyxl 保存，因为它会丢弃 xl/drawings（即客户的「日更新」按钮）。

只写客户指定的实时数据格，其余一律不动：
  - 文件 B（含「实时数据」sheet, 如 etf_historical_dataX.xlsm）：
      A日期 B开盘 C收盘 D最高 E最低 F成交量 G成交额 I涨跌幅 J涨跌额 O前收盘
      （H振幅 / K换手率 保留公式让 Excel 自己算；L代码/M名称/N板块/P份额 不动）
  - 文件 A（含「ETF列表」sheet, 如 ETF示范数据_扩展版X.xlsm）：
      仅写 F列(实时数据=现价)，按 A列代码匹配。

数据来自 core.data_source.quotes_routed：有 TickFlow token 时 ETF 走 TickFlow，
抓不到自动回退腾讯免费源。不执行任何 VBA 宏、不碰永久库、不另存新文件。
"""

from __future__ import annotations

import logging
import os
import re
import stat
import zipfile
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

_NS_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_NS_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_NS_PKG_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'


def _norm_code(raw) -> str:
    """归一成裸 6 位代码（去后缀/前缀、补前导 0）。无法识别返回 ''。"""
    if raw is None:
        return ''
    c = str(raw).strip().upper()
    for suf in ('.SH', '.SZ', '.SS', '.BJ'):
        if c.endswith(suf):
            c = c[:-len(suf)]
            break
    if c.startswith(('SH', 'SZ', 'BJ')):
        c = c[2:]
    c = ''.join(ch for ch in c if ch.isdigit())
    return c.zfill(6) if c else ''



def _num(v) -> str:
    """格式化为 Excel <v> 数字文本。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return '0'
    if f != f:  # NaN
        return '0'
    if f == int(f):
        return str(int(f))
    return repr(f)


def _detect(path):
    """只读判别类型并收集 (行号, 代码)。返回 (kind, sheet_name, rows)。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        sheets = wb.sheetnames
        if 'ETF列表' in sheets:
            kind, sheet, col = 'demo', 'ETF列表', 1      # A 列代码
        elif '实时数据' in sheets:
            kind, sheet, col = 'hist', '实时数据', 12     # L 列代码
        else:
            return None, None, []
        ws = wb[sheet]
        rows = []
        for row in ws.iter_rows(min_row=2):
            if col - 1 >= len(row):
                continue
            cell = row[col - 1]
            code = _norm_code(cell.value)
            if code:
                rows.append((cell.row, code))
        return kind, sheet, rows
    finally:
        wb.close()


def _sheet_part(zf: zipfile.ZipFile, sheet_name: str) -> str | None:
    """sheet 名 → worksheet XML 部件路径（如 xl/worksheets/sheet3.xml）。"""
    wb_root = ET.fromstring(zf.read('xl/workbook.xml'))
    rid = None
    for sh in wb_root.iter('{%s}sheet' % _NS_MAIN):
        if sh.get('name') == sheet_name:
            rid = sh.get('{%s}id' % _NS_R)
            break
    if not rid:
        return None
    rels = ET.fromstring(zf.read('xl/_rels/workbook.xml.rels'))
    for rel in rels.iter('{%s}Relationship' % _NS_PKG_REL):
        if rel.get('Id') == rid:
            tgt = rel.get('Target') or ''
            tgt = tgt.lstrip('/')
            if not tgt.startswith('xl/'):
                tgt = 'xl/' + tgt
            return tgt.replace('\\', '/')
    return None


def _set_cell(xml_text: str, ref: str, value_text: str):
    """把单个单元格改成数字值：删掉公式/类型，保留样式 s。返回 (新文本, 命中数)。"""
    pat = re.compile(r'<c r="%s"([^>]*?)(?:/>|>.*?</c>)' % re.escape(ref), re.DOTALL)

    def repl(m):
        attrs = m.group(1)
        sm = re.search(r'\s+s="\d+"', attrs)
        s_attr = sm.group(0) if sm else ''
        return '<c r="%s"%s><v>%s</v></c>' % (ref, s_attr, value_text)

    return pat.subn(repl, xml_text, count=1)


def _cell_number(xml_text: str, ref: str) -> float | None:
    pat = re.compile(r'<c r="%s"[^>]*>.*?<v>(.*?)</v>.*?</c>' % re.escape(ref), re.DOTALL)
    m = pat.search(xml_text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def _set_formula_cached_number(xml_text: str, ref: str, value_text: str) -> str:
    pat = re.compile(r'<c r="%s"([^>]*?)>(.*?)</c>' % re.escape(ref), re.DOTALL)

    def repl(m):
        attrs = re.sub(r'\s+t="[^"]*"', '', m.group(1))
        body = m.group(2)
        if '<f' not in body:
            return m.group(0)
        body = re.sub(r'<v>.*?</v>', '', body, flags=re.DOTALL)
        if '</f>' in body:
            body = re.sub(r'(</f>)', r'\1<v>%s</v>' % value_text, body, count=1)
        else:
            body = re.sub(r'(<f\b[^>]*/>)', r'\1<v>%s</v>' % value_text, body, count=1)
        return '<c r="%s"%s>%s</c>' % (ref, attrs, body)

    return pat.sub(repl, xml_text, count=1)


def _refresh_hist_formula_caches(xml_text: str, rownum: int, q: dict) -> str:
    pre = q.get('pre_close') or 0
    if pre:
        amp = ((q.get('high') or 0) - (q.get('low') or 0)) / pre * 100
        xml_text = _set_formula_cached_number(xml_text, 'H%d' % rownum, _num(amp))

    shares = _cell_number(xml_text, 'P%d' % rownum) or 0
    if shares:
        turnover = (q.get('volume') or 0) / shares * 100
        xml_text = _set_formula_cached_number(xml_text, 'K%d' % rownum, _num(turnover))

    return xml_text


def _force_workbook_recalc(xml_text: str) -> str:
    flags = ' calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"'
    pat = re.compile(r'<calcPr\b([^>]*)/?>')

    def repl(m):
        attrs = m.group(1).rstrip()
        if attrs.endswith('/'):
            attrs = attrs[:-1].rstrip()
        attrs = re.sub(r'\s+(?:calcMode|fullCalcOnLoad|forceFullCalc)="[^"]*"', '', attrs)
        return '<calcPr%s%s/>' % (attrs, flags)

    new_text, n = pat.subn(repl, xml_text, count=1)
    if n:
        return new_text
    return xml_text.replace('</workbook>', '<calcPr%s/></workbook>' % flags, 1)


def _rewrite_zip(path: str, replace: dict, drop: set) -> None:
    """替换 replace 中列出的部件、丢弃 drop 中的部件，其余字节级原样拷贝。"""
    tmp = path + '.tmp'
    with zipfile.ZipFile(path, 'r') as zin, \
            zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename in drop:
                continue
            data = replace.get(item.filename)
            if data is None:
                data = zin.read(item.filename)
            zout.writestr(item, data)
    os.replace(tmp, path)


# 把 =RTD(...) / 普通公式格改成静态值后，这些「公式/易失函数」缓存部件会与实际
# 不一致，导致 Excel 打开报「发现部分内容有问题/需要修复」：
#   - calcChain.xml          公式计算链缓存
#   - volatileDependencies.xml  易失函数(RTD 实时数据)依赖缓存
# 标准做法是整份丢弃让 Excel 打开时自动重建，并同步清掉它们在 [Content_Types].xml
# 的 Override 与 workbook.xml.rels 的 Relationship，否则会留下悬空引用同样报错。
_STALE_CACHE_PARTS = ('xl/calcChain.xml', 'xl/volatileDependencies.xml')


def _strip_part_refs(ct_text: str, rels_text: str, part: str) -> tuple[str, str]:
    """从 Content_Types 的 Override 和 workbook.xml.rels 的 Relationship 删掉对 part 的引用。"""
    base = part.split('/')[-1]
    ct = re.sub(r'<Override[^>]*PartName="/%s"[^>]*/>' % re.escape(part), '', ct_text)
    rels = re.sub(r'<Relationship[^>]*Target="[^"]*%s"[^>]*/>' % re.escape(base), '', rels_text)
    return ct, rels


def _cells_for_row(kind: str, r: int, q: dict) -> dict:
    price = q.get('price') or 0
    pre = q.get('pre_close') or 0
    if kind == 'demo':
        return {'F%d' % r: price}
    return {
        'B%d' % r: q.get('open') or 0,                  # 开盘价
        'C%d' % r: price,                               # 收盘价(现价)
        'D%d' % r: q.get('high') or 0,                  # 最高价
        'E%d' % r: q.get('low') or 0,                   # 最低价
        'F%d' % r: q.get('volume') or 0,                # 成交量
        'G%d' % r: q.get('amount') or 0,                # 成交额
        'I%d' % r: q.get('pct') or 0,                   # 涨跌幅
        'J%d' % r: round(price - pre, 4) if (price and pre) else 0,  # 涨跌额
        'O%d' % r: pre,                                 # 作收盘价(前收)
    }


def _surgical_write(path, sheet, kind, rows, qmap) -> tuple[int, int]:
    # 客户文件常带「只读」属性，就地写入前先清掉只读位
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
    except Exception:
        pass

    with zipfile.ZipFile(path, 'r') as zf:
        part = _sheet_part(zf, sheet)
        if not part:
            raise RuntimeError('找不到 worksheet 部件: ' + sheet)
        xml = zf.read(part).decode('utf-8')
        names = set(zf.namelist())
        ct_text = zf.read('[Content_Types].xml').decode('utf-8')
        rels_text = zf.read('xl/_rels/workbook.xml.rels').decode('utf-8')
        wb_text = zf.read('xl/workbook.xml').decode('utf-8')

    updated = skipped = 0
    for rownum, code in rows:
        q = qmap.get(code)
        if not q or not q.get('price'):
            skipped += 1
            continue
        hit = False
        for ref, val in _cells_for_row(kind, rownum, q).items():
            xml, n = _set_cell(xml, ref, _num(val))
            if n:
                hit = True
        if kind == 'hist':
            xml = _refresh_hist_formula_caches(xml, rownum, q)
        if hit:
            updated += 1
        else:
            skipped += 1

    replace = {
        part: xml.encode('utf-8'),
        'xl/workbook.xml': _force_workbook_recalc(wb_text).encode('utf-8'),
    }
    drop: set = set()
    # 删过公式格 → 丢弃公式/易失函数缓存并清掉其 Content_Types/rels 引用，避免 Excel 报修复
    ct_new, rels_new = ct_text, rels_text
    for stale in _STALE_CACHE_PARTS:
        if stale in names:
            ct_new, rels_new = _strip_part_refs(ct_new, rels_new, stale)
            drop.add(stale)
    if drop:
        replace['[Content_Types].xml'] = ct_new.encode('utf-8')
        replace['xl/_rels/workbook.xml.rels'] = rels_new.encode('utf-8')

    _rewrite_zip(path, replace, drop)
    return updated, skipped


def update_etf_files(paths, timeout: float = 8) -> dict:
    """逐个就地刷新 xlsm 的实时数据格。返回 {'results': [...], 'quotes': n}。

    单个文件失败不影响其它文件；不抛异常（UI 线程安全）。
    """
    from core.data_source import quotes_routed

    results: list[dict] = []
    plans: list[tuple] = []
    all_codes: set[str] = set()

    for path in paths:
        try:
            kind, sheet, rows = _detect(path)
        except Exception as e:
            logger.warning('[etf_updater] 读取失败 %s: %s', path, e)
            results.append({'path': path, 'ok': False, 'msg': f'读取失败: {e}'})
            continue
        if not kind:
            results.append({'path': path, 'ok': False,
                            'msg': '无法识别表结构（需含 ETF列表 或 实时数据 sheet），已跳过'})
            continue
        plans.append((path, kind, sheet, rows))
        all_codes.update(c for _, c in rows)

    if not plans:
        return {'results': results, 'quotes': 0}

    try:
        raw = quotes_routed(sorted(all_codes), timeout=timeout, bulk_tickflow=True) if all_codes else {}
    except Exception as e:
        logger.warning('[etf_updater] 取行情失败: %s', e)
        raw = {}
    qmap = {_norm_code(k): v for k, v in raw.items()}

    for path, kind, sheet, rows in plans:
        try:
            upd, skp = _surgical_write(path, sheet, kind, rows, qmap)
            results.append({'path': path, 'ok': True, 'updated': upd, 'skipped': skp})
        except PermissionError as e:
            logger.warning('[etf_updater] 文件被占用 %s: %s', path, e)
            results.append({'path': path, 'ok': False,
                            'msg': '文件可能正在 Excel 中打开，请先关闭后重试'})
        except Exception as e:
            logger.warning('[etf_updater] 写入失败 %s: %s', path, e)
            results.append({'path': path, 'ok': False, 'msg': f'写入失败: {e}'})

    return {'results': results, 'quotes': len(qmap)}
