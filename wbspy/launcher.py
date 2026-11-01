"""PyInstaller 打包入口（不参与 pip 安装使用）。

    pyinstaller --onefile --name wbspy --collect-all wbspy wbspy/launcher.py
"""

import sys

from wbspy.cli import main

if __name__ == "__main__":
    sys.exit(main())
