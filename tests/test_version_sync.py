"""Core's three version locations, the mod's gradle version and the READMEs must agree."""
from pathlib import Path
import re

import worldprompt_core

ROOT = Path(__file__).resolve().parents[1]
VERSION = r"\d+\.\d+\.\d+"


def text(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def single(pattern, relative, flags=0):
    found = re.findall(pattern, text(relative), flags)
    assert len(found) == 1, f"{relative}: expected exactly one match for {pattern!r}, found {found}"
    return found[0]


def core_versions():
    return {
        "pyproject.toml": single(rf'^version = "({VERSION})"$', "pyproject.toml", re.M),
        "src/worldprompt_core/__init__.py": single(rf'^__version__ = "({VERSION})"$',
                                                  "src/worldprompt_core/__init__.py", re.M),
        "src/worldprompt_core/static/index.html": single(
            rf'<span id="footer-version">({VERSION})</span>', "src/worldprompt_core/static/index.html"),
    }


def mod_version():
    return single(rf"^version=({VERSION})$", "fabric/gradle.properties", re.M)


def test_core_version_locations_agree():
    versions = core_versions()
    assert len(set(versions.values())) == 1, versions
    assert worldprompt_core.__version__ in versions.values()


def test_mod_version_matches_the_fabric_readme_and_jar_name():
    mod = mod_version()
    readme = text("fabric/README.md")
    assert single(rf"^WorldPrompt ({VERSION}) targets Minecraft", "fabric/README.md", re.M) == mod
    jars = set(re.findall(rf"worldprompt-({VERSION})\.jar", readme))
    assert jars == {mod}, f"fabric/README.md jar names {jars} do not match gradle.properties {mod}"


def test_root_readme_current_versions_line_matches_the_sources():
    line = single(r"^Current versions: (.*)$", "README.md", re.M)
    mod = re.search(rf"\*\*mod ({VERSION})\*\*", line)
    core = re.search(rf"\*\*Core ({VERSION})\*\*", line)
    assert mod and core, f"README.md 'Current versions' line not understood: {line!r}"
    assert mod.group(1) == mod_version()
    assert core.group(1) == worldprompt_core.__version__


def test_fabric_readme_names_the_matching_core_version():
    pair = re.search(rf"Use Core ({VERSION}) with mod ({VERSION})", text("fabric/README.md"))
    assert pair, "fabric/README.md compatibility sentence not found"
    assert pair.group(1) == worldprompt_core.__version__
    assert pair.group(2) == mod_version()
