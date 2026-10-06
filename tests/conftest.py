"""Explicit platform exclusions for upstream tests that load real macOS frameworks.

Other macOS unit tests use fakes and continue to run on Linux/Windows.
"""
import sys

import pytest

_MACOS_FRAMEWORK_TESTS = {
    "test_permission_messages_name_the_app_that_started_arc_cua",
    "test_ocr_runs_only_when_needed",
    "test_a_restored_window_is_focused_by_clicking_an_inert_spot",
    "test_text_is_clicked_only_when_it_is_the_windows_own_title",
    "test_with_no_inert_spot_nothing_is_clicked",
}


def pytest_collection_modifyitems(items):
    if sys.platform == "darwin":
        return
    for item in items:
        if item.originalname in _MACOS_FRAMEWORK_TESTS:
            item.add_marker(pytest.mark.skip(reason="Upstream test requires macOS AppKit/ApplicationServices"))
