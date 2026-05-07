"""Tests for config loading and target/group resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from wt import _load_config, _resolve_target, RunSpec
from tests.conftest import write_wt_yaml


class TestLoadConfig:
    def test_returns_none_if_missing(self, git_repo):
        assert _load_config(git_repo) is None

    def test_loads_valid_yaml(self, git_repo):
        write_wt_yaml(git_repo, {"targets": {"run": "python app.py"}})
        cfg = _load_config(git_repo)
        assert cfg["targets"]["run"] == "python app.py"

    def test_invalid_yaml_exits(self, git_repo):
        (git_repo / ".wt.yaml").write_text(": bad: yaml: [")
        with pytest.raises(SystemExit):
            _load_config(git_repo)

    def test_non_dict_exits(self, git_repo):
        (git_repo / ".wt.yaml").write_text("- just\n- a\n- list\n")
        with pytest.raises(SystemExit):
            _load_config(git_repo)


class TestResolveTarget:
    def test_no_config_falls_back_to_make(self):
        spec = _resolve_target(None, "build")
        assert spec.commands == [("build", "make build")]
        assert not spec.is_group

    def test_single_target(self):
        cfg = {"targets": {"run": "python app.py"}}
        spec = _resolve_target(cfg, "run")
        assert spec.commands == [("run", "python app.py")]
        assert not spec.is_group

    def test_unknown_target_falls_to_make(self):
        cfg = {"targets": {"run": "python app.py"}}
        spec = _resolve_target(cfg, "build")
        assert spec.commands == [("build", "make build")]

    def test_group_resolution(self):
        cfg = {
            "targets": {"server": "python manage.py runserver", "worker": "celery -A app"},
            "groups": {"run": ["server", "worker"]},
        }
        spec = _resolve_target(cfg, "run")
        assert spec.is_group
        assert len(spec.commands) == 2
        assert spec.commands[0] == ("server", "python manage.py runserver")
        assert spec.commands[1] == ("worker", "celery -A app")

    def test_group_with_unknown_member_exits(self):
        cfg = {
            "targets": {"server": "python manage.py runserver"},
            "groups": {"run": ["server", "ghost"]},
        }
        with pytest.raises(SystemExit):
            _resolve_target(cfg, "run")

    def test_group_non_list_exits(self):
        cfg = {
            "targets": {"server": "python manage.py runserver"},
            "groups": {"run": "server"},
        }
        with pytest.raises(SystemExit):
            _resolve_target(cfg, "run")

    def test_target_takes_precedence_over_group(self):
        """If a name exists in both targets and groups, groups win (checked first)."""
        cfg = {
            "targets": {"run": "python app.py", "server": "python manage.py runserver"},
            "groups": {"run": ["server"]},
        }
        spec = _resolve_target(cfg, "run")
        assert spec.is_group
