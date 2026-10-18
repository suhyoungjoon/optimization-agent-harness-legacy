"""레가시 형식 CLI: 내보내기 → 등록."""

import pytest

from core.storage.store import Store
from scripts import legacy as cli


def test_export_then_load(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEGACY_DATA_DIR", str(tmp_path / "legacy"))
    cli.main(["export", "--seed", "42", "--faults", "P4", "--name", "demo"])
    assert (tmp_path / "legacy" / "demo" / "truth.json").is_file()
    db = tmp_path / "h.db"
    cli.main(["load", "demo", "--db", str(db)])
    assert "dispatch-src-demo" in capsys.readouterr().out
    assert Store(db).get_dataset("dispatch-src-demo")["source"] == "demo"


def test_bad_names_and_missing_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGACY_DATA_DIR", str(tmp_path / "legacy"))
    with pytest.raises(SystemExit):
        cli.main(["export", "--seed", "1", "--name", "../x"])
    with pytest.raises(SystemExit):
        cli.main(["load", "missing", "--db", str(tmp_path / "h.db")])
