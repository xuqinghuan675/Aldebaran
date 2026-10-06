"""Central writable runtime paths for source and packaged runs.

Priority:
1. ALDEBARAN_DATA_DIR, when set, is the application data root.
2. Frozen/compiled runs use AldebaranData next to the executable.
3. Source runs keep the existing developer root at ~/.aldebaran.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


_DATA_DIR_ENV = 'ALDEBARAN_DATA_DIR'
_INTELLIGENCE_CACHE_ENV = 'ALDEBARAN_INTELLIGENCE_CACHE_DIR'
_PACKAGED_DATA_DIR = 'AldebaranData'


def app_data_root() -> Path:
    override = os.environ.get(_DATA_DIR_ENV)
    if override:
        return Path(os.path.expandvars(override)).expanduser()

    executable = _packaged_executable_path()
    if executable is not None:
        return executable.resolve().parent / _PACKAGED_DATA_DIR

    return Path.home() / '.aldebaran'


def cache_root() -> Path:
    return app_data_root() / 'cache'


def intelligence_cache_root() -> Path:
    override = os.environ.get(_INTELLIGENCE_CACHE_ENV)
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    return cache_root() / 'intelligence'


def documents_cache_root() -> Path:
    return intelligence_cache_root() / 'documents'


def disclosure_documents_root(document_kind: str = 'announcement') -> Path:
    return documents_cache_root() / 'disclosures' / _document_kind_dir(document_kind)


def extracted_text_cache_root(document_kind: str = 'announcement') -> Path:
    return documents_cache_root() / 'extracted_text' / _document_kind_dir(document_kind)


def documents_metadata_root() -> Path:
    return documents_cache_root() / 'metadata'


def pdf_index_path() -> Path:
    return documents_metadata_root() / 'pdf_index.jsonl'


def ensure_app_data_dirs() -> None:
    root = app_data_root()
    cache = cache_root()
    intel_cache = intelligence_cache_root()
    user_data = root / 'data'

    for path in (
        root,
        cache,
        cache / 'kline',
        intel_cache,
        intel_cache / 'normalized',
        disclosure_documents_root('announcement'),
        disclosure_documents_root('periodic_report'),
        extracted_text_cache_root('announcement'),
        extracted_text_cache_root('periodic_report'),
        documents_metadata_root(),
        root / 'logs',
        root / 'config',
        user_data,
    ):
        path.mkdir(parents=True, exist_ok=True)

    feed_file = user_data / 'intel_feed.json'
    settings_file = user_data / 'intel_settings.json'
    if not feed_file.exists():
        feed_file.write_text('[]', encoding='utf-8')
    if not settings_file.exists():
        settings_file.write_text('{"sort_mode":"time"}', encoding='utf-8')


def ensure_data_dir() -> None:
    ensure_app_data_dirs()


def _packaged_executable_path() -> Path | None:
    # Nuitka onefile 实际运行进程位于解压缓存目录；sys.executable 和部分
    # 版本的 NUITKA_ONEFILE_BINARY 都可能指向该内部副本。sys.argv[0]
    # 保留用户真正启动的外层 EXE 路径，因此打包态优先采用它。
    argv0 = str(sys.argv[0] or '').strip() if sys.argv else ''
    argv0_path = Path(argv0) if argv0 else None
    onefile_path = os.environ.get('NUITKA_ONEFILE_BINARY')
    if onefile_path:
        if argv0_path is not None and argv0_path.suffix.lower() == '.exe':
            return argv0_path
        return Path(onefile_path)
    if getattr(sys, 'frozen', False):
        if argv0_path is not None and argv0_path.suffix.lower() == '.exe':
            return argv0_path
        return Path(sys.executable)
    if '__compiled__' in globals():
        if argv0_path is not None and argv0_path.suffix.lower() == '.exe':
            return argv0_path
        return Path(sys.executable)
    return None


def _document_kind_dir(document_kind: str) -> str:
    value = str(document_kind or '').strip().lower().replace('-', '_')
    if value in {'announcement', 'announcements'}:
        return 'announcements'
    if value in {'periodic_report', 'periodic_reports', 'periodic'}:
        return 'periodic_reports'
    return 'unknown'


BASE_DIR = app_data_root()
HOME = BASE_DIR
CACHE_DIR = BASE_DIR / 'cache'
USER_DATA_DIR = BASE_DIR / 'data'
LOG_DIR = BASE_DIR / 'logs'
CONFIG_DIR = BASE_DIR / 'config'
INTEL_FEED_FILE = USER_DATA_DIR / 'intel_feed.json'
INTEL_SETTINGS_FILE = USER_DATA_DIR / 'intel_settings.json'
ANALYSIS_MEMORY_DIR = USER_DATA_DIR / 'analysis_memory'
LOG_FILE = LOG_DIR / 'aldebaran_crash.log'
