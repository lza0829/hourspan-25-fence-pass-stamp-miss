import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
FIXTURES = BACKEND.parent / "fixtures"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离库 + 真实夹具目录，FastAPI TestClient 驱动全链路。

    app.db 在导入时读取 DATA_DIR/FIXTURES_DIR，因此每个用例清掉 app.* 模块
    缓存后重新导入，保证库路径互不污染。
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("FIXTURES_DIR", str(FIXTURES))
    sys.path.insert(0, str(BACKEND))

    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
