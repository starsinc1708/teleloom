"""Deliver explicit platform secret mounts to the normal environment credential seam."""

import os
import re
import sys
from pathlib import Path


def main() -> None:
    directory = Path(os.environ.get("TELELOOM_CONTAINER_SECRET_DIR", "/run/secrets"))
    try:
        if directory.is_symlink() or directory.is_junction():
            raise ValueError
        if directory.exists():
            for path in directory.iterdir():
                if not re.fullmatch(r"TELELOOM_[A-Z][A-Z0-9_]*", path.name):
                    continue
                if path.is_symlink() or path.is_junction() or not path.is_file():
                    raise ValueError
                with path.open("rb") as stream:
                    raw = stream.read(65537)
                value = raw.decode("utf-8").rstrip("\r\n")
                if len(raw) > 65536 or not value or "\x00" in value:
                    raise ValueError
                os.environ[path.name] = value
    except (OSError, ValueError):
        sys.exit("Platform credential mount is unavailable or invalid.")
    os.execv(sys.executable, [sys.executable, "-m", "teleloom", *sys.argv[1:]])


if __name__ == "__main__":
    main()
