"""The installed-package workflow refuses checkout imports before starting an owner."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.process_e2e


def test_package_smoke_rejects_checkout_imports_before_starting_runtime(tmp_path):
    root = Path(__file__).resolve().parents[1]
    unused_owner = tmp_path / "unused-owner"
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "smoke_projection_package.py")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(root / "src"),
            "TELELOOM_DATA_DIR": str(unused_owner),
            "PYTHONIOENCODING": "utf-8",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode != 0, result.stdout
    assert "installed wheel" in result.stderr.lower()
    assert not unused_owner.exists()
