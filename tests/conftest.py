from datetime import timedelta
from time import monotonic

import pytest

from teleloom.models import utcnow

pytest_plugins = ["tests.release_fixtures"]


@pytest.fixture(autouse=True)
def stable_job_clock(monkeypatch):
    # Host clock corrections must not add real rate-limit waits to fake-API tests.
    origin, started = utcnow(), monotonic()
    monkeypatch.setattr(
        "teleloom.jobs.utcnow", lambda: origin + timedelta(seconds=monotonic() - started)
    )


@pytest.fixture(autouse=True)
def isolated_fake_session_locks(tmp_path, monkeypatch):
    # Fake credentials are reused in SDK fixtures; each test gets a real isolated lock namespace.
    monkeypatch.setattr("teleloom.session_state.lock_directory", lambda: tmp_path / "session-locks")
