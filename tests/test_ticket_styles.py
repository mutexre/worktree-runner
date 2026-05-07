"""Tests for ticket style matching and canonicalization."""

from __future__ import annotations

import re

import pytest

from wt import (
    _STYLE_JIRA,
    _STYLE_LINEAR,
    _STYLE_GITHUB,
    _custom_style,
    _resolve_style,
)


class TestJiraStyle:
    def test_matches_uppercase(self):
        m = _STYLE_JIRA.regex.search("feature/SPLAT-12-login")
        assert m
        assert _STYLE_JIRA.canonicalize(m) == "SPLAT-12"

    def test_rejects_lowercase(self):
        assert _STYLE_JIRA.regex.search("feature/splat-12-login") is None

    def test_rejects_single_letter_prefix(self):
        assert _STYLE_JIRA.regex.search("S-12") is None

    def test_prefix_extraction(self):
        m = _STYLE_JIRA.regex.search("PROJ-99")
        assert _STYLE_JIRA.prefix_of(m) == "PROJ"

    def test_multi_match(self):
        matches = list(_STYLE_JIRA.regex.finditer("SPLAT-1 and SPLAT-2"))
        assert len(matches) == 2


class TestLinearStyle:
    def test_matches_uppercase(self):
        m = _STYLE_LINEAR.regex.search("SPLAT-12")
        assert m
        assert _STYLE_LINEAR.canonicalize(m) == "SPLAT-12"

    def test_matches_lowercase(self):
        m = _STYLE_LINEAR.regex.search("eng-42")
        assert m
        assert _STYLE_LINEAR.canonicalize(m) == "ENG-42"

    def test_matches_mixed_case(self):
        m = _STYLE_LINEAR.regex.search("feature/Eng-7-fix")
        assert m
        assert _STYLE_LINEAR.canonicalize(m) == "ENG-7"

    def test_prefix_uppercased(self):
        m = _STYLE_LINEAR.regex.search("eng-42")
        assert _STYLE_LINEAR.prefix_of(m) == "ENG"


class TestGithubStyle:
    def test_matches_hash_number(self):
        m = _STYLE_GITHUB.regex.search("#123")
        assert m
        assert _STYLE_GITHUB.canonicalize(m) == "#123"

    def test_matches_bare_number_in_branch(self):
        m = _STYLE_GITHUB.regex.search("123-fix-bug")
        assert m
        assert _STYLE_GITHUB.canonicalize(m) == "#123"

    def test_no_prefix_of(self):
        assert _STYLE_GITHUB.prefix_of is None

    def test_expand_digits(self):
        assert _STYLE_GITHUB.expand_digits("42") == ["#42"]


class TestCustomStyle:
    def test_two_group_regex(self):
        style = _custom_style(r"TASK_(\w+)-(\d+)")
        m = style.regex.search("TASK_foo-99")
        assert m
        assert style.canonicalize(m) == "FOO-99"
        assert style.prefix_of(m) == "FOO"

    def test_single_group_regex(self):
        style = _custom_style(r"(BUG\d+)")
        m = style.regex.search("BUG42")
        assert m
        assert style.canonicalize(m) == "BUG42"
        assert style.prefix_of is None


class TestResolveStyle:
    def test_none_config(self):
        s = _resolve_style(None)
        assert s is _STYLE_LINEAR

    def test_missing_key(self):
        s = _resolve_style({"targets": {}})
        assert s is _STYLE_LINEAR

    def test_builtin_jira(self):
        s = _resolve_style({"ticket_style": "jira"})
        assert s is _STYLE_JIRA

    def test_builtin_github(self):
        s = _resolve_style({"ticket_style": "github"})
        assert s is _STYLE_GITHUB

    def test_custom_regex(self):
        s = _resolve_style({"ticket_style": r"TASK_(\w+)-(\d+)"})
        assert s.regex.pattern == r"TASK_(\w+)-(\d+)"
