"""Shared test setup."""

import logging

import pytest

from j1x0_cardprinter import cli, sender


@pytest.fixture(autouse=True)
def no_pacing_delays(monkeypatch):
    """The real pacing sleeps 50 ms per chunk and 2 s before the end-of-card commands; tests do not wait."""
    monkeypatch.setattr(sender, "SETTLE", 0)
    monkeypatch.setattr(sender, "END_DELAY", 0)
    monkeypatch.setattr(cli, "AFTERMATH_INTERVAL", 0)
    monkeypatch.setattr(cli, "AFTERMATH_POLLS", 0)


@pytest.fixture(autouse=True)
def restore_logging():
    """`-v` reconfigures the package logger (own handler, no propagation); undo that after each test."""
    package = logging.getLogger("j1x0_cardprinter")
    handlers, level, propagate = package.handlers[:], package.level, package.propagate
    yield
    package.handlers[:] = handlers
    package.setLevel(level)
    package.propagate = propagate
