from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "provision.py"
SPEC = importlib.util.spec_from_file_location("provision", SCRIPT)
assert SPEC and SPEC.loader
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


def _write_config(config_dir: Path, default_provider: str) -> None:
    config_dir.mkdir()
    (config_dir / "users.yaml").write_text("users: []\n")
    (config_dir / "models.yaml").write_text("models: []\n")
    (config_dir / "tenants.yaml").write_text(
        yaml.safe_dump(
            {
                "tenants": [
                    {
                        "id": "acme-corp",
                        "name": "Acme Corporation",
                        "default_provider": default_provider,
                    }
                ]
            }
        )
    )


def test_load_config_accepts_gemini_vertex_default_provider(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    _write_config(config_dir, "gemini-vertex")
    monkeypatch.setattr(provision, "CONFIG", config_dir)

    tenants, _users, _models = provision.load_config()

    assert tenants[0]["default_provider"] == "gemini-vertex"


def test_load_config_accepts_existing_gemini_default_provider(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    _write_config(config_dir, "gemini")
    monkeypatch.setattr(provision, "CONFIG", config_dir)

    tenants, _users, _models = provision.load_config()

    assert tenants[0]["default_provider"] == "gemini"


def test_load_config_rejects_unrecognized_default_provider(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    _write_config(config_dir, "not-a-real-provider")
    monkeypatch.setattr(provision, "CONFIG", config_dir)

    with pytest.raises(SystemExit, match="not-a-real-provider"):
        provision.load_config()
