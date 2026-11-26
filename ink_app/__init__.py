"""专色墨称量核定 Flask 应用包。"""

import sys as _sys


def _ensure_runtime_deps() -> None:
    """兜底：系统解释器没装依赖时，尝试加入可用的用户站点目录。

    正常 Docker / ``pip install -r requirements.txt`` 环境下 flask 可直接导入，
    本函数不改动任何路径；仅在一次性沙箱里把依赖装进非默认用户目录时生效。
    可用 ``INK_EXTRA_SITE`` 显式指定额外的 site-packages 目录。
    """
    try:
        import flask  # noqa: F401
        return
    except Exception:
        pass

    import glob
    import os
    import site

    candidates = []
    extra = os.environ.get("INK_EXTRA_SITE")
    if extra:
        candidates.append(extra)
    candidates.append(site.getusersitepackages())
    candidates.extend(sorted(glob.glob("/tmp/pyuser/lib/python*/site-packages")))
    for path in candidates:
        if path and os.path.isdir(path) and path not in _sys.path:
            site.addsitedir(path)
            if path not in _sys.path:
                _sys.path.insert(0, path)
            return


_ensure_runtime_deps()

from .app import create_app  # noqa: E402
from . import domain  # noqa: E402

__all__ = ["create_app", "domain"]
