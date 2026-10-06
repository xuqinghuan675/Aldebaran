"""用户数据总导出 / 导入 / 重置。

导出单文件 JSON bundle，不含 API Key 和 intel.db。
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.paths import HOME as _HOME, CACHE_DIR as _CACHE, ANALYSIS_MEMORY_DIR as _MEM_DIR  # noqa: E402

_USER_FILES = {
    'watchlist':       _CACHE / 'watchlist.json',
    'watchlist_groups': _CACHE / 'watchlist_groups.json',
    'option_watchlist': _CACHE / 'option_watchlist.json',
    'holdings':        _HOME / 'portfolio.json',
    'position_watchlist': _HOME / 'position_watchlist.json',
    # virtual_portfolio 已废弃，保留仅为向后兼容，不再导出
    # 'virtual_portfolio': _HOME / 'virtual_portfolio.json',
    'tracking_tasks':  _HOME / 'tracking_tasks.json',
    'improvement_candidates': _HOME / 'improvement_candidates.json',
    'user_profile':    _HOME / 'user_profile.json',
    'predictions_cache': _CACHE / 'ai_predictions.json',
    'pnl_calendar': _CACHE / 'pnl_calendar.json',
    'holdings_pnl_calendar': _CACHE / 'holdings_pnl_calendar.json',
}
_TEXT_USER_FILES = {
    'tracking_events': _HOME / 'tracking_events.jsonl',
}
_RESET_ONLY_FILES = [
    _CACHE / 'watchlist_data.json',
]
_RESET_ONLY_GLOBS = [
    _CACHE / 'watchlist_fund_*.json',
]

_VERSION = 'v3.0'
_DICT_PAYLOAD_KEYS = {'user_profile', 'predictions_cache'}
_LIST_PAYLOAD_KEYS = set(_USER_FILES) - _DICT_PAYLOAD_KEYS


def _normalize_import_code(value) -> str:
    text = str(value or '').strip().upper()
    for suffix in ('.SS', '.SH', '.SZ', '.CSI'):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            break
    for prefix in ('SH', 'SZ', 'BJ'):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    digits = ''.join(ch for ch in text if ch.isdigit())
    if len(digits) in (6, 8):
        return digits
    return text


def _stable_item_key(key: str, item):
    if isinstance(item, dict):
        code = _normalize_import_code(item.get('code'))
        if key == 'watchlist' and code:
            return ('watchlist', code, str(item.get('group') or ''))
        if key in {'holdings', 'option_watchlist', 'position_watchlist'} and code:
            return (key, code)
        if key == 'tracking_tasks':
            task_id = str(item.get('id') or '').strip()
            if task_id:
                return ('tracking_task_id', task_id)
            return (
                'tracking_task',
                code,
                str(item.get('created_at') or '')[:10],
                str(item.get('direction') or ''),
                str(item.get('source') or ''),
            )
        if key in {'pnl_calendar', 'holdings_pnl_calendar'}:
            rec_date = str(item.get('date') or '').strip()
            if rec_date:
                return (key, rec_date)
        item_id = str(item.get('id') or '').strip()
        if item_id:
            return (key, 'id', item_id)
        if code:
            return (key, 'code', code)
        try:
            return (key, json.dumps(item, ensure_ascii=False, sort_keys=True))
        except Exception:
            return (key, str(item))
    return (key, str(item).strip())


def _merge_list_payload(key: str, current: list, incoming: list) -> list:
    merged = list(current)
    index = {}
    for pos, item in enumerate(merged):
        index[_stable_item_key(key, item)] = pos
    for item in incoming:
        item_key = _stable_item_key(key, item)
        if item_key in index:
            merged[index[item_key]] = item
        else:
            index[item_key] = len(merged)
            merged.append(item)
    return merged


def _merge_import_payload(key: str, current, incoming):
    if isinstance(current, list) and isinstance(incoming, list):
        return _merge_list_payload(key, current, incoming)
    if isinstance(current, dict) and isinstance(incoming, dict):
        merged = dict(current)
        merged.update(incoming)
        return merged
    return incoming


def _is_compatible_list_item(key: str, item) -> bool:
    if key == 'watchlist_groups':
        return isinstance(item, str) and bool(item.strip())
    if not isinstance(item, dict):
        return False
    code = _normalize_import_code(item.get('code'))
    if key == 'watchlist':
        return bool(code)
    if key in {'holdings', 'option_watchlist', 'position_watchlist'}:
        return bool(code)
    if key == 'tracking_tasks':
        if str(item.get('id') or '').strip():
            return True
        return bool(code and str(item.get('created_at') or '').strip())
    if key in {'pnl_calendar', 'holdings_pnl_calendar'}:
        return bool(str(item.get('date') or '').strip())
    if key == 'improvement_candidates':
        return bool(str(item.get('id') or '').strip())
    return True


def _prepare_import_payload(key: str, incoming):
    if key in _DICT_PAYLOAD_KEYS:
        if not isinstance(incoming, dict):
            return None, key
        return incoming, None
    if key in _LIST_PAYLOAD_KEYS:
        if not isinstance(incoming, list):
            return None, key
        filtered = []
        skipped_count = 0
        for item in incoming:
            if _is_compatible_list_item(key, item):
                filtered.append(item)
            else:
                skipped_count += 1
        if skipped_count and not filtered:
            return None, f'{key}: {skipped_count} incompatible item(s)'
        return (
            filtered,
            f'{key}: {skipped_count} incompatible item(s)' if skipped_count else None,
        )
    return incoming, None


def _load_current_json(fpath: Path):
    try:
        if fpath.exists():
            return json.loads(fpath.read_text(encoding='utf-8'))
    except Exception:
        pass
    return None


def _text_line_key(line: str):
    text = str(line or '').strip()
    if not text:
        return ('empty', '')
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            item_id = str(data.get('id') or '').strip()
            if item_id:
                return ('id', item_id)
    except Exception:
        pass
    return ('line', text)


def _merge_text_payload(current: str, incoming: str) -> str:
    merged = [line for line in str(current or '').splitlines() if line.strip()]
    index = {_text_line_key(line): pos for pos, line in enumerate(merged)}
    for line in str(incoming or '').splitlines():
        if not line.strip():
            continue
        line_key = _text_line_key(line)
        if line_key in index:
            merged[index[line_key]] = line
        else:
            index[line_key] = len(merged)
            merged.append(line)
    return '\n'.join(merged) + ('\n' if merged else '')


@dataclass
class ImportResult:
    success: bool
    imported: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    backup_path: str = ''


def export_user_data(target_path: Path) -> dict:
    """打包用户数据到单个 JSON 文件，返回各 key 的写入状态。"""
    bundle: dict = {
        'version': _VERSION,
        'exported_at': datetime.now().isoformat(timespec='seconds'),
        'data': {},
    }
    status = {}

    for key, fpath in _USER_FILES.items():
        if fpath.exists():
            try:
                bundle['data'][key] = json.loads(fpath.read_text(encoding='utf-8'))
                status[key] = 'ok'
            except Exception as e:
                status[key] = f'error: {e}'
        else:
            status[key] = 'missing'
    for key, fpath in _TEXT_USER_FILES.items():
        if fpath.exists():
            try:
                bundle['data'][key] = fpath.read_text(encoding='utf-8')
                status[key] = 'ok'
            except Exception as e:
                status[key] = f'error: {e}'
        else:
            status[key] = 'missing'

    # 分析记忆（每只股票一个 .md）
    memories: dict[str, str] = {}
    if _MEM_DIR.exists():
        for md in _MEM_DIR.glob('*.md'):
            try:
                memories[md.stem] = md.read_text(encoding='utf-8')
            except Exception:
                pass
    bundle['data']['analysis_memory'] = memories
    status['analysis_memory'] = f'{len(memories)} files'

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    except Exception as e:
        status['_write'] = f'error: {e}'
    else:
        status['_write'] = 'ok'

    return status


def import_user_data(source_path: Path) -> ImportResult:
    """从 JSON bundle 恢复用户数据，导入前备份现有文件。"""
    result = ImportResult(success=False)
    try:
        bundle = json.loads(source_path.read_text(encoding='utf-8'))
    except Exception as e:
        result.errors.append(f'读取文件失败: {e}')
        return result

    data = bundle.get('data', {})
    if not data:
        result.errors.append('bundle data 为空')
        return result

    # 备份现有数据
    backup_tag = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = _HOME / f'backup_{backup_tag}'
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        for key, fpath in _USER_FILES.items():
            if fpath.exists():
                shutil.copy2(fpath, backup_dir / fpath.name)
        for key, fpath in _TEXT_USER_FILES.items():
            if fpath.exists():
                shutil.copy2(fpath, backup_dir / fpath.name)
        if _MEM_DIR.exists():
            shutil.copytree(_MEM_DIR, backup_dir / 'analysis_memory', dirs_exist_ok=True)
        result.backup_path = str(backup_dir)
    except Exception as e:
        result.errors.append(f'备份失败（导入已中止）: {e}')
        return result

    # 写入各 key
    for key, fpath in _USER_FILES.items():
        if key not in data:
            result.skipped.append(key)
            continue
        try:
            incoming, skip_reason = _prepare_import_payload(key, data[key])
            if skip_reason:
                result.skipped.append(skip_reason)
            if incoming is None:
                continue
            fpath.parent.mkdir(parents=True, exist_ok=True)
            current = _load_current_json(fpath)
            payload = _merge_import_payload(key, current, incoming)
            fpath.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            result.imported.append(key)
        except Exception as e:
            result.errors.append(f'{key}: {e}')
    for key, fpath in _TEXT_USER_FILES.items():
        if key not in data:
            result.skipped.append(key)
            continue
        try:
            if not isinstance(data[key], str):
                result.skipped.append(key)
                continue
            fpath.parent.mkdir(parents=True, exist_ok=True)
            current = ''
            try:
                if fpath.exists():
                    current = fpath.read_text(encoding='utf-8')
            except Exception:
                current = ''
            payload = _merge_text_payload(current, str(data[key] or ''))
            fpath.write_text(payload, encoding='utf-8')
            result.imported.append(key)
        except Exception as e:
            result.errors.append(f'{key}: {e}')

    # 分析记忆
    memories: dict = data.get('analysis_memory', {})
    if memories:
        if not isinstance(memories, dict):
            result.skipped.append('analysis_memory')
            result.success = len(result.errors) == 0
            return result
        try:
            _MEM_DIR.mkdir(parents=True, exist_ok=True)
            for code, content in memories.items():
                (_MEM_DIR / f'{code}.md').write_text(content, encoding='utf-8')
            result.imported.append(f'analysis_memory ({len(memories)} files)')
        except Exception as e:
            result.errors.append(f'analysis_memory: {e}')

    result.success = len(result.errors) == 0
    return result


def reset_user_data() -> list[str]:
    """删除所有用户数据文件，返回已删除路径列表。"""
    deleted = []
    for key, fpath in _USER_FILES.items():
        if fpath.exists():
            try:
                fpath.unlink()
                deleted.append(str(fpath))
            except Exception:
                pass
    for key, fpath in _TEXT_USER_FILES.items():
        if fpath.exists():
            try:
                fpath.unlink()
                deleted.append(str(fpath))
            except Exception:
                pass
    for fpath in _RESET_ONLY_FILES:
        if fpath.exists():
            try:
                fpath.unlink()
                deleted.append(str(fpath))
            except Exception:
                pass
    for pattern in _RESET_ONLY_GLOBS:
        try:
            for fpath in pattern.parent.glob(pattern.name):
                if fpath.exists():
                    try:
                        fpath.unlink()
                        deleted.append(str(fpath))
                    except Exception:
                        pass
        except Exception:
            pass
    if _MEM_DIR.exists():
        try:
            shutil.rmtree(_MEM_DIR)
            deleted.append(str(_MEM_DIR))
        except Exception:
            pass
    return deleted
