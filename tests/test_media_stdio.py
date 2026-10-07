import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.process_e2e
def test_media_real_subprocess_stdio_cli_and_inline_images():
    script = Path(__file__).resolve().parents[1] / "scripts" / "smoke_media_package.py"
    result = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", str(script), "--source-checkout"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["photo_sheet"] == "passed"
