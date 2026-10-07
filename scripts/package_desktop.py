"""Package one built wheel as a portable UV desktop bundle, without owner state."""

import argparse
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from check_build_metadata import wheel_metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    if not wheel.is_file() or not wheel.name.startswith("teleloom-") or wheel.suffix != ".whl":
        parser.error("--wheel must identify a built teleloom wheel")
    build = wheel_metadata(wheel)
    uv = shutil.which("uv")
    if not uv:
        parser.error("uv is required to lock the bundle's dependencies")
    template = Path(__file__).resolve().parents[1] / "packaging" / "desktop"
    with tempfile.TemporaryDirectory(prefix="teleloom-desktop-") as temporary:
        root = Path(temporary)
        manifest = json.loads((template / "manifest.json").read_text(encoding="utf-8"))
        manifest["version"] = build["package_version"]
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        shutil.copy2(template / "server.py", root / "server.py")
        shutil.copy2(wheel, root / wheel.name)
        (root / "pyproject.toml").write_text(
            '[project]\nname = "teleloom-desktop"\nversion = '
            + json.dumps(build["package_version"])
            + '\nrequires-python = ">=3.12,<3.15"\ndependencies = ["teleloom=='
            + build["package_version"]
            + '"]\n\n[tool.uv.sources]\nteleloom = { path = '
            + json.dumps(wheel.name)
            + " }\n",
            encoding="utf-8",
        )
        subprocess.run([uv, "lock", "--directory", str(root)], check=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in ("manifest.json", "server.py", "pyproject.toml", "uv.lock", wheel.name):
                archive.write(root / name, name)
    print(json.dumps({"bundle": str(args.output.resolve()), "build": build}))


if __name__ == "__main__":
    main()
