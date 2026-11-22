"""Быстрые unit-тесты: версия, пути приложения и конфиг-пример."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import __version__ as version  # noqa: E402
import helper  # noqa: E402


def test_version_is_consistent():
    assert version.APP_NAME == "VHS-Streamer"
    assert version.__version__.count(".") >= 1
    assert version.__version__ in version.APP_TITLE
    assert "VHS-Streamer" in version.APP_FOOTER


def test_helper_paths_exist():
    app_dir = helper.app_dir()
    assert isinstance(app_dir, Path)
    assert app_dir.is_dir()
    assert (app_dir / "app.py").is_file()


def test_helper_resource_dir():
    resource = helper.resource_dir()
    assert isinstance(resource, Path)
    assert resource.is_dir()


def test_config_example_parses():
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "server" in data
    assert "mediamtx" in data
    users = (data.get("security") or {}).get("users")
    assert isinstance(users, list)
    assert users, "в примере конфига должен быть хотя бы один пользователь"


def test_config_users_have_roles():
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    roles = {str(item.get("role")) for item in data["security"]["users"]}
    assert roles <= {"admin", "viewer"}
    assert "admin" in roles


@pytest.mark.parametrize("name", ["README.md", "CHANGELOG.md", "LICENSE"])
def test_repository_documents_exist(name):
    assert (ROOT / name).is_file()
