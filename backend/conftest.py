import os
import tempfile

# 必须在任何 app 模块导入前指向隔离数据目录（app.db 在导入时定型 DATA_DIR）
_TMP = tempfile.mkdtemp(prefix="clickspan-test-")
os.environ["DATA_DIR"] = _TMP

import pytest  # noqa: E402

from app import seed  # noqa: E402
from app.db import DB_PATH  # noqa: E402


@pytest.fixture()
def fresh_db():
    if DB_PATH.exists():
        DB_PATH.unlink()
    seed.init_db()
    return DB_PATH
