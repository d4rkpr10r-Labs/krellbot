"""A frozen build identifies the exact clean source it was built from."""

import json
import subprocess
import sys

from krellbot.release_channel import version_payload
from scripts.freeze import _stage_build_identity
from scripts.release_archive import _source_commit


def test_clean_frozen_source_identity_survives_into_runtime_and_manifest(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "source").write_text("source", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "source"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "source"], check=True)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    bundle = tmp_path / "bundle"
    _stage_build_identity(bundle, repo_root=repo)
    assert _source_commit(bundle) == commit
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle / "_internal"), raising=False)
    assert version_payload()["source_commit"] == commit


def test_dirty_checkout_does_not_claim_a_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "source").write_text("source", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "source"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "source"], check=True)
    (repo / "source").write_text("modified", encoding="utf-8")
    bundle = tmp_path / "bundle"
    _stage_build_identity(bundle, repo_root=repo)
    assert _source_commit(bundle) is None


def test_malformed_build_identity_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "krellbot-build.json"
    path.write_text(json.dumps({"source_commit": "not-a-commit"}), encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert "source_commit" not in version_payload()
