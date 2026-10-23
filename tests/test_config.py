import os
from pathlib import Path

import pytest

import cc_bridge as cc


def test_import_has_no_runtime_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_BRIDGE_HOME", str(tmp_path / "uncreated"))
    bridge = cc.Bridge()
    assert bridge.home == (tmp_path / "uncreated").resolve()
    assert not bridge.home.exists()


def test_default_home(monkeypatch):
    monkeypatch.delenv("CC_BRIDGE_HOME", raising=False)
    expected = (Path(os.environ["LOCALAPPDATA"]) / "cc-bridge"
                if os.name == "nt" and os.environ.get("LOCALAPPDATA") else Path.home() / ".cc-bridge")
    assert cc.runtime_home() == expected.resolve()


def test_discovery_prefers_path(fake_claude, monkeypatch):
    monkeypatch.setenv("CC_BRIDGE_CLAUDE_PATH", "missing")
    assert cc.discover_claude_exe() == fake_claude.resolve()


def test_discovery_fallback_and_missing(fake_claude, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda _: None)
    monkeypatch.setenv("CC_BRIDGE_CLAUDE_PATH", str(fake_claude))
    assert cc.discover_claude_exe() == fake_claude.resolve()
    monkeypatch.setenv("CC_BRIDGE_CLAUDE_PATH", "missing")
    with pytest.raises(cc.BridgeError, match="existing executable"):
        cc.discover_claude_exe()
    monkeypatch.delenv("CC_BRIDGE_CLAUDE_PATH")
    with pytest.raises(cc.BridgeError, match="not found"):
        cc.discover_claude_exe()


def test_environment_preserves_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    assert cc.build_claude_env({"task_id": "example"})["ANTHROPIC_BASE_URL"] == "https://example.invalid"
