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
    jars = set(re.findall(rf"worldprompt-fabric-({VERSION})\.jar", readme))
    assert jars == {mod}, f"fabric/README.md jar names {jars} do not match gradle.properties {mod}"


def test_mod_version_matches_the_neoforge_readme():
    """Fabric and NeoForge are one component, so the NeoForge README tracks the same version."""
    mod = mod_version()
    neoforge = text("neoforge/README.md")
    jars = set(re.findall(rf"worldprompt-neoforge-({VERSION})\.jar", neoforge))
    assert jars == {mod}, f"neoforge/README.md jar names {jars} do not match gradle.properties {mod}"
    core = set(re.findall(rf"WorldPrompt Core ({VERSION})", neoforge))
    assert core == {worldprompt_core.__version__}, f"neoforge/README.md Core version {core} is stale"
    minecraft = single(r"for Minecraft (\d+\.\d+) and Java", "neoforge/README.md")
    assert minecraft == single(r"^minecraft_version=(\d+\.\d+)$", "fabric/gradle.properties", re.M)
    assert single(r"Install NeoForge (\d+\.\d+\.\d+\.\d+) or newer", "neoforge/README.md") == neoforge_version()


def neoforge_version():
    return single(r"^    version = '(\d+\.\d+\.\d+\.\d+)'$", "neoforge/build.gradle", re.M)


def test_the_lane_fixture_advertises_the_current_core_version():
    """The mod refuses a Core whose MAJOR.MINOR differs, so a stale lane fixture breaks the lane."""
    assert single(r'^CORE_VERSION = "(%s)"$' % VERSION, "tools/lane/lane.py", re.M) == worldprompt_core.__version__


def test_the_lane_guide_names_the_current_neoforge_jar():
    """docs/development.md lists this as a version location, so it is checked like the others."""
    jars = set(re.findall(rf"worldprompt-neoforge-({VERSION})\.jar", text("tools/lane/README.md")))
    assert jars == {mod_version()}, f"tools/lane/README.md jar names {jars} are stale"


def test_the_neoforge_version_the_readmes_require_matches_the_build():
    required = set(re.findall(r"NeoForge\]?\([^)]*\)? (\d+\.\d+\.\d+\.\d+) or newer", text("README.md")))
    required |= {single(r"Install NeoForge (\d+\.\d+\.\d+\.\d+) or newer", "neoforge/README.md")}
    assert required == {neoforge_version()}, f"README NeoForge versions {required} do not match neoforge/build.gradle"


def test_both_loader_jar_names_follow_the_shared_mod_version():
    """One mod, two loaders: each loader's own jar name carries the shared version.

    The archive names are what CI uploads and what the release workflow allows, so a
    rename that misses one of them ships a release with no matching file.
    """
    mod = mod_version()
    for loader in ("fabric", "neoforge"):
        archives = single(r"^base\.archivesName = '([\w-]+)'$", f"{loader}/build.gradle", re.M)
        jar = f"worldprompt-{loader}-{mod}.jar"
        assert archives == f"worldprompt-{loader}"
        assert jar in text("README.md"), f"README.md is missing the {loader} download {jar}"
        release = text(".github/workflows/release.yml")
        assert rf"^worldprompt-{loader}-VERSION\.jar$" in release, \
            f"release.yml does not allow a worldprompt-{loader}-<version>.jar asset"
    assert "worldprompt-${{ matrix.loader }}-${{ steps.mod_version.outputs.value }}.jar" \
        in text(".github/workflows/build.yml")


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
