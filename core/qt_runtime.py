from __future__ import annotations

import os
import sys


def configure_qt_runtime() -> None:
    os.environ.setdefault('QT_API', 'pyside6')
    try:
        import six  # noqa: F401
    except Exception:
        return

    for importer in sys.meta_path:
        if (
            importer.__class__.__name__ == '_SixMetaPathImporter'
            and not hasattr(importer, '_path')
        ):
            # PySide6's support import hook asks inspect for modules imported by six.moves.
            # Python 3.12's module repr expects loader._path to exist on meta path importers.
            importer._path = None
