import json
import os
import stat

import pytest

from storage import atomic_write_json


def test_roundtrip_and_private_mode(tmp_path):
    p = tmp_path / "sub" / "x.json"
    atomic_write_json(p, {"a": 1}, private=True)
    assert json.loads(p.read_text()) == {"a": 1}
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_failed_write_keeps_old_file(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"old": True})
    with pytest.raises(TypeError):
        atomic_write_json(p, {"bad": object()})  # not JSON-serialisable
    assert json.loads(p.read_text()) == {"old": True}
    assert [f.name for f in tmp_path.iterdir()] == ["x.json"]  # no temp left
