"""Tests for the initial Python package foundation."""

import importlib


def test_collector_package_is_importable() -> None:
    package = importlib.import_module("services.collector")

    assert package.SERVICE_NAME == "Opportunity Radar collector"


def test_collector_entry_point_is_loadable() -> None:
    entry_point = importlib.import_module("services.collector.main")

    assert callable(entry_point.main)
