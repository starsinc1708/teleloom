"""Install a built wheel outside checkout, then run the existing package smoke."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--wheel", type=Path, help="Exact teleloom wheel to install")
    source.add_argument("--wheel-dir", type=Path, default=Path("dist"))
    parser.add_argument(
        "--inventory", type=Path, help="Write the installed release's schema/skill inventory"
    )
    parser.add_argument(
        "--inventory-only", action="store_true", help="Collect inventory without behavioral smoke"
    )
    args = parser.parse_args()
    if args.inventory_only and args.inventory is None:
        parser.error("--inventory-only requires --inventory")
    if args.wheel:
        wheel = args.wheel.resolve()
    else:
        wheels = sorted(args.wheel_dir.resolve().glob("teleloom-*.whl"))
        if len(wheels) != 1:
            parser.error("Expected exactly one teleloom wheel; use --wheel for a specific artifact")
        wheel = wheels[0]
    if not wheel.is_file() or wheel.suffix != ".whl":
        parser.error("--wheel must identify an existing .whl file")
    uv = shutil.which("uv")
    if uv is None:
        parser.error("uv is required to create the isolated environment")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TELELOOM_")
        and key not in {"TYPESAFE_API_KEY", "PYTHONPATH", "PYTHONHOME"}
    }
    wheel_hash = hashlib.sha256(wheel.read_bytes()).hexdigest()

    def measured_run(stage, *arguments, **options):
        started, exit_code = perf_counter(), 1
        try:
            result = subprocess.run(*arguments, **options)
            exit_code = result.returncode
            return result
        except subprocess.CalledProcessError as error:
            exit_code = error.returncode
            raise
        finally:
            print(
                json.dumps(
                    {
                        "stage": stage,
                        "exit": exit_code,
                        "seconds": round(perf_counter() - started, 6),
                        "workers": 0,
                        "wheel_sha256": wheel_hash,
                    }
                )
            )

    with tempfile.TemporaryDirectory(prefix="teleloom-installed-wheel-") as directory:
        root = Path(directory)
        environment = root / "environment"
        measured_run(
            "installed_venv",
            [uv, "venv", "--python", sys.executable, str(environment)],
            cwd=root,
            env=env,
            check=True,
        )
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        measured_run(
            "installed_install",
            [uv, "pip", "install", "--python", str(python), str(wheel)],
            cwd=root,
            env=env,
            check=True,
        )
        if not args.inventory_only:
            for name in ("projection", "media"):
                smoke = root / f"smoke_{name}_package.py"
                shutil.copy2(Path(__file__).with_name(smoke.name), smoke)
                # -I rejects path/user-site contamination; cwd/script are outside checkout.
                measured_run(
                    f"{name}_smoke",
                    [str(python), "-I", "-X", "utf8", str(smoke)],
                    cwd=root,
                    env=env,
                    check=True,
                )
        if args.inventory:
            probe = root / "release_probe.py"
            shutil.copy2(Path(__file__).with_name(probe.name), probe)
            inventory = measured_run(
                "installed_inventory",
                [str(python), "-I", "-X", "utf8", str(probe), "inventory"],
                cwd=root,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            args.inventory.write_text(inventory.stdout, encoding="utf-8")
        cleanup_started = perf_counter()
    print(
        json.dumps(
            {
                "stage": "installed_cleanup",
                "exit": 0,
                "seconds": round(perf_counter() - cleanup_started, 6),
                "workers": 0,
                "wheel_sha256": wheel_hash,
            }
        )
    )


if __name__ == "__main__":
    main()
