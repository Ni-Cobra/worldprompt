"""Build, smoke-test and archive WorldPrompt Core for this Windows/Linux host."""
from __future__ import annotations

import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def required_glibc(bundle: Path) -> tuple[int, int]:
    """Find the highest glibc symbol required by any bundled ELF object."""
    required = (0, 0)
    for path in bundle.rglob("*"):
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            if handle.read(4) != b"\x7fELF":
                continue
        output = subprocess.run(["readelf", "--version-info", str(path)],
                                capture_output=True, text=True, check=True).stdout
        for major, minor in re.findall(r"GLIBC_(\d+)\.(\d+)", output):
            required = max(required, (int(major), int(minor)))
    return required


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-smoke", action="store_true", help="Build without executing the packaged verification")
    parser.add_argument("--bundle-root", type=Path,
                        help="Stage the unpacked bundle in this directory; archives still go to dist")
    parser.add_argument("--max-glibc", default=os.environ.get("WORLDPROMPT_MAX_GLIBC", ""),
                        help="Fail a Linux build when bundled ELF objects require a newer glibc")
    arguments = parser.parse_args()
    if sys.platform not in {"win32", "linux"}:
        parser.error("Executable releases currently target Windows and Linux only.")
    system = "windows" if sys.platform == "win32" else "linux"
    # Keep both native bundles when building from a shared Windows/WSL checkout.
    bundle_root = ROOT / "dist" / "linux" if system == "linux" else ROOT / "dist"
    if arguments.bundle_root:
        bundle_root = arguments.bundle_root.resolve()
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(bundle_root), "--workpath", str(ROOT / "build" / "pyinstaller" / system),
                    str(ROOT / "packaging" / "worldprompt-core.spec")], cwd=ROOT, check=True)
    bundle = bundle_root / "WorldPrompt-Core"
    # Ship only what runs Core, plus the license its MIT terms require; docs live on GitHub.
    shutil.copy2(ROOT / "LICENSE", bundle / "LICENSE")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    information = {"version": project["version"], "system": platform.system(),
                   "architecture": platform.machine(), "python": platform.python_version(),
                   "libc": dict(zip(("name", "version"), platform.libc_ver())),
                   "pyinstaller": version("pyinstaller")}
    if system == "linux":
        required = required_glibc(bundle)
        information["required_glibc"] = ".".join(map(str, required))
        if arguments.max_glibc:
            match = re.fullmatch(r"(\d+)\.(\d+)", arguments.max_glibc)
            if match is None:
                parser.error("--max-glibc must be a major.minor version")
            maximum = tuple(map(int, match.groups()))
            if required > maximum:
                raise RuntimeError(f"Linux bundle requires glibc {information['required_glibc']}, above {arguments.max_glibc}")
    print(f"Build info: {json.dumps(information)}")
    executable = bundle / ("WorldPrompt-Core.exe" if sys.platform == "win32" else "WorldPrompt-Core")
    if not arguments.skip_smoke:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke_core.py"), str(executable)], check=True)
    architecture = platform.machine().lower().replace("amd64", "x86_64")
    name = ROOT / "dist" / f"WorldPrompt-Core-{project['version']}-{system}-{architecture}"
    archive = shutil.make_archive(str(name), "zip" if system == "windows" else "gztar",
                                  root_dir=bundle.parent, base_dir=bundle.name)
    print(f"Executable: {executable}\nDistribution: {archive}")


if __name__ == "__main__":
    main()
