"""Shared plumbing for the live integration suite (marker: integration)."""

from __future__ import annotations

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.failed:
        # read by the stack fixture teardown to decide whether to dump
        # subprocess stderr
        item.session._integration_failed = True
