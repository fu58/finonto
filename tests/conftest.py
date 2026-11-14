"""pytest 配置：把 server/ 放进 sys.path，并把数据目录指到仓库内。

为什么不用系统临时目录：很多企业终端策略会禁止写 ``%TEMP%``，
把测试数据放在 ``server/var-test`` 更稳，也方便出问题时直接看文件。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "server"

sys.path.insert(0, str(SERVER_DIR))
os.environ.setdefault("FINONTO_DATA_DIR", str(SERVER_DIR / "var-test"))
os.environ.setdefault("FINONTO_AUTO_BOOTSTRAP", "false")
