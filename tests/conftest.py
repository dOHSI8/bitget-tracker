import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point every persisted file at a temp dir *before* main.py is imported (test
# modules import it at collection time), so tests never touch real state.
_STATE = Path(tempfile.mkdtemp(prefix="bt-state-"))
for _var, _name in (("SETTINGS_PATH", "settings.json"), ("COOKIES_PATH", "cookies.json"),
                    ("TRADERS_PATH", "traders.json"), ("CREDENTIALS_PATH", "credentials.json"),
                    ("HISTORY_PATH", "history.json")):
    os.environ[_var] = str(_STATE / _name)
os.environ["TRADERS"] = "Tester:123"
os.environ.pop("ADMIN_TOKEN", None)
os.environ.pop("COOKIE_SYNC_TOKEN", None)
os.chdir(ROOT)  # StaticFiles(directory="static") is relative


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import main
    # No `with` block: skip lifespan so the Playwright poller never starts.
    return TestClient(main.app)
