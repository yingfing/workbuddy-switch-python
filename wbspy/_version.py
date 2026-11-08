"""版本号单一事实源。

pyproject.toml（dynamic = ["version"]）与运行时 __init__ 都从本文件读，
升版本只改这一处——历史上 pyproject 与 __init__ 各写一份、升版只改一半，
导致 0.2.5~0.2.12 运行时始终自报 0.2.4（更新弹窗永远显示 v0.2.4 → vX.Y.Z）。
"""

__version__ = "0.2.27"
