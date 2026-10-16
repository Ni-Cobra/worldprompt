# Build on the destination operating system: PyInstaller is not a cross compiler.
from pathlib import Path
import sys
import sysconfig
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH).parent
data = [(str(root / "src" / "worldprompt_core" / directory), "worldprompt_core/" + directory)
        for directory in ("static", "schemas")]
for distribution in ("numpy", "pillow", "pyinstaller", *(("pystray", "six") if sys.platform == "win32" else ())):
    data += copy_metadata(distribution)
python_license = next((path for path in (
    Path(sys.base_prefix) / "LICENSE.txt", Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
) if path.is_file()), None)
if python_license is None:
    raise SystemExit("Python's LICENSE.txt must be available when building a distributable.")
data.append((str(python_license), "licenses/python"))

analysis = Analysis(
    [str(root / "packaging" / "core_launcher.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=data,
    hiddenimports=["worldprompt_core.world_worker"] + (["pystray._win32"] if sys.platform == "win32" else []),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tkinter"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
# Explorer and the taskbar show this; Linux executables carry no icon.
icon = str(root / "packaging" / "worldprompt-core.ico") if sys.platform == "win32" else None
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="WorldPrompt-Core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=sys.platform != "win32",
    icon=icon,
)
console_executables = []
if sys.platform == "win32":
    console_executables.append(EXE(pyz, analysis.scripts, [], exclude_binaries=True,
        name="WorldPrompt-Core-Console", console=True, strip=False, upx=False, icon=icon))
collection = COLLECT(
    executable,
    *console_executables,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="WorldPrompt-Core",
)
