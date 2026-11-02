"""pytest 公共引导：保证项目根目录在 sys.path，使 `app.*` 可导入。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
