"""Shared test setup.

Both fixtures are autouse because they defend against ambient state rather than
providing anything: without them a test can pass or fail based on what happens to
sit in the developer's working directory or how wide their terminal is.
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Run every test in an empty directory.

    The CLI picks up ./config.yaml when --config isn't passed, so a real config
    file in the repo root would otherwise silently retune the whole suite. A
    subdirectory of tmp_path rather than tmp_path itself, so that fixtures a test
    writes to tmp_path are never auto-discovered by accident — a test that wants
    discovery has to put the file in cwd on purpose.
    """
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


@pytest.fixture(autouse=True)
def wide_console(monkeypatch):
    """Stop Rich wrapping summary lines mid-phrase.

    Rich falls back to an 80-column width when stdout isn't a terminal, which is
    narrow enough to split `1 duplicate cluster(s)` across two lines and break a
    substring assertion for reasons that have nothing to do with the code.
    """
    monkeypatch.setenv("COLUMNS", "200")
