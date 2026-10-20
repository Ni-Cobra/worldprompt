"""The committed compiler recipe and wrapper must be redistributable and internally consistent."""
import hashlib
import json
from pathlib import Path
import re
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def test_compiler_recipe_has_verified_source_identity():
    recipe = json.loads((ROOT / 'mycelium.lock').read_text(encoding='utf-8'))
    assert recipe['schema'] == 2
    assert recipe['completion'] == 'locked'
    for key in ('revision', 'effective_tree'):
        assert re.fullmatch('[0-9a-f]{40}', recipe[key])
    if 'patch' in recipe:
        patch = (ROOT / recipe['patch']).resolve()
        assert patch.is_relative_to(ROOT)
        digest = hashlib.sha256(patch.read_bytes()).hexdigest()
        assert recipe['patch_sha256'] == digest
        assert patch.name == f'{digest}.patch'


def test_wrapper_is_java_17_and_carries_its_license():
    directory = ROOT / 'tools/mycelium'
    wrapper = directory / 'mycelium-wrapper.jar'
    expected = (directory / 'wrapper.sha256').read_text().split()[0]
    assert hashlib.sha256(wrapper.read_bytes()).hexdigest() == expected
    with zipfile.ZipFile(wrapper) as archive:
        assert b'dev.woflo.mycelium.source.Main' in archive.read('META-INF/MANIFEST.MF')
        assert archive.read('META-INF/licenses/Mycelium-MPL-2.0.txt')
        assert archive.read('META-INF/licenses/Gson-Apache-2.0.txt')
        classes = [archive.read(name) for name in archive.namelist() if name.endswith('.class')]
        assert classes
        assert max(int.from_bytes(data[6:8], 'big') for data in classes) <= 61


def test_wrapper_sources_and_hooks_accompany_the_binary():
    directory = ROOT / 'tools/mycelium'
    with zipfile.ZipFile(directory / 'mycelium-wrapper-sources.jar') as archive:
        assert archive.read('dev/woflo/mycelium/source/Main.java')
        for name in ('settings.gradle', 'consumer.gradle'):
            assert archive.read(f'consumer/{name}').replace(b'\r\n', b'\n') == (directory / name).read_bytes().replace(b'\r\n', b'\n')
        for name in ('mycelium', 'mycelium.cmd'):
            assert archive.read(f'consumer/{name}').replace(b'\r\n', b'\n') == (ROOT / name).read_bytes().replace(b'\r\n', b'\n')
