from __future__ import annotations

import sys
from unittest.mock import patch

from sa_home_bot.nodectl import _build_parser


def test_nodectl_devlab_reviews_parses_args():
    parser = _build_parser()
    args = parser.parse_args(["devlab", "reviews", "--model", "model-a", "--since", "2026-01-01"])
    assert args.command == "devlab"
    assert args.devlab_command == "reviews"
    assert args.model == "model-a"
    assert args.since == "2026-01-01"


def test_nodectl_devlab_reviews_defaults():
    parser = _build_parser()
    args = parser.parse_args(["devlab", "reviews"])
    assert args.command == "devlab"
    assert args.devlab_command == "reviews"
    assert args.model == ""
    assert args.since == ""


def test_nodectl_devlab_reviews_help(capsys):
    parser = _build_parser()
    with patch.object(sys, "argv", ["nodectl", "devlab", "reviews", "--help"]):
        try:
            parser.parse_args(["devlab", "reviews", "--help"])
        except SystemExit:
            pass
    out = capsys.readouterr().out
    assert "reviews" in out
    assert "--model" in out
    assert "--since" in out
