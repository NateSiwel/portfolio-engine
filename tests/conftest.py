"""Shared test setup.

Tests run from the project root (the price cache's stock_data/ path is
relative) and hit yfinance over the network when a cache is cold.
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture(autouse=True, scope="session")
def project_root_cwd():
    os.chdir(PROJECT_ROOT)
    yield


@pytest.fixture(scope="session")
def fixtures_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """stock_data_cache pointed at an empty temp dir with clean memory state."""
    import stock_data_cache as sdc

    monkeypatch.setattr(sdc, "CACHE_DIR", str(tmp_path))
    sdc._frames.clear()
    sdc._metas.clear()
    sdc._series.clear()
    yield sdc
    sdc._frames.clear()
    sdc._metas.clear()
    sdc._series.clear()
