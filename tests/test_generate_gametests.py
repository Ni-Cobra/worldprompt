"""The NeoForge GameTest generator owns its output tree: no stale copies, no needless rewrites."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LF = chr(10)
FABRIC_GAMETESTS = ROOT / 'fabric/src/gametest/java'


def generator():
    spec = importlib.util.spec_from_file_location('generate_gametests', ROOT / 'tools/generate_gametests.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # dataclasses resolves annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def sources(tmp_path):
    """A small stand-in for the Fabric GameTest tree, so the test owns its inputs."""
    root = tmp_path / 'gametest'
    (root / 'dev' / 'worldprompt').mkdir(parents=True)
    write_test(root, 'KeptGameTest.java', 'keptScenario')
    write_test(root, 'RemovedGameTest.java', 'removedScenario')
    return root


def write_test(root, name, method, package='dev.worldprompt'):
    target = root / 'dev' / 'worldprompt' / name if package == 'dev.worldprompt' else root / name
    target.write_text(
        f'package {package};\n\n'
        'import net.fabricmc.fabric.api.gametest.v1.GameTest;\n'
        'import net.minecraft.gametest.framework.GameTestHelper;\n\n'
        f'public final class {name.removesuffix(".java")} {{\n'
        '    @GameTest\n'
        f'    public void {method}(GameTestHelper context) {{\n'
        '        context.succeed();\n'
        '    }\n'
        '}\n', encoding='utf-8')


def generated(output_dir):
    return {path.relative_to(output_dir).as_posix() for path in output_dir.rglob('*.java')}


def test_a_deleted_or_renamed_fabric_test_does_not_survive_in_the_neoforge_output(tmp_path):
    module = generator()
    root, output = sources(tmp_path), tmp_path / 'out'
    assert module.generate(root, output) == 2
    assert 'dev/worldprompt/RemovedGameTest.java' in generated(output)
    registry = (output / 'dev/worldprompt/WorldPromptGameTests.java').read_text(encoding='utf-8')
    assert 'removed_scenario' in registry

    # Delete one and rename another, as ordinary test maintenance does.
    (root / 'dev' / 'worldprompt' / 'RemovedGameTest.java').unlink()
    (root / 'dev' / 'worldprompt' / 'KeptGameTest.java').unlink()
    write_test(root, 'RenamedGameTest.java', 'keptScenario')

    assert module.generate(root, output) == 1
    assert generated(output) == {'dev/worldprompt/RenamedGameTest.java',
                                 'dev/worldprompt/WorldPromptGameTests.java'}
    registry = (output / 'dev/worldprompt/WorldPromptGameTests.java').read_text(encoding='utf-8')
    assert 'removed_scenario' not in registry and 'KeptGameTest' not in registry
    assert 'renamed_game_kept_scenario' in registry


def test_an_unchanged_second_generation_rewrites_nothing(tmp_path):
    module = generator()
    root, output = sources(tmp_path), tmp_path / 'out'
    module.generate(root, output)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(output.rglob('*.java'))}
    module.generate(root, output)
    after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(output.rglob('*.java'))}
    assert after == before, 'a repeat generation must not touch the build inputs it already wrote'


def test_empty_directories_left_by_pruning_are_removed(tmp_path):
    """Pruning the last generated file in a package leaves no empty directory behind."""
    module = generator()
    root, output = sources(tmp_path), tmp_path / 'out'
    nested = root / 'dev' / 'worldprompt' / 'legacy'
    nested.mkdir(parents=True)
    write_test(root / 'dev' / 'worldprompt' / 'legacy', 'OldGameTest.java', 'oldScenario',
               package='dev.worldprompt.legacy')
    module.generate(root, output)
    generated = output / 'dev' / 'worldprompt' / 'legacy' / 'OldGameTest.java'
    assert generated.is_file()
    (nested / 'OldGameTest.java').unlink()
    module.generate(root, output)
    assert not generated.exists() and not generated.parent.exists()


def test_a_whole_package_of_stale_tests_is_pruned_without_crashing(tmp_path):
    """Removing a package of several tests is ordinary maintenance.

    Each removal walks up to tidy the directories it emptied, and two removals share one
    directory, so the walk must not trip over a directory an earlier removal already took.
    """
    module = generator()
    root, output = sources(tmp_path), tmp_path / 'out'
    nested = root / 'dev' / 'worldprompt' / 'legacy' / 'deep'
    nested.mkdir(parents=True)
    for name, method in (('OneGameTest.java', 'one'), ('TwoGameTest.java', 'two')):
        write_test(nested, name, method, package='dev.worldprompt.legacy.deep')
    module.generate(root, output)
    generated = output / 'dev' / 'worldprompt' / 'legacy' / 'deep'
    assert len(list(generated.glob('*.java'))) == 2
    for name in ('OneGameTest.java', 'TwoGameTest.java'):
        (nested / name).unlink()
    module.generate(root, output)                       # must not raise
    assert not generated.exists(), 'the emptied package directory is gone'
    assert not generated.parent.exists(), 'and so is the parent it emptied'
    assert output.is_dir() and (output / 'dev' / 'worldprompt').is_dir()


def test_a_source_named_after_the_generated_registry_is_refused(tmp_path):
    """The registry is written last, so a same-named source would vanish silently."""
    module = generator()
    root, output = sources(tmp_path), tmp_path / 'out'
    write_test(root, 'WorldPromptGameTests.java', 'collides')
    with pytest.raises(ValueError, match='named after the generated registry'):
        module.generate(root, output)


def test_the_output_directory_must_be_separate_from_the_sources(tmp_path):
    module = generator()
    root = sources(tmp_path)
    for output in (root, root / 'dev', root.parent):
        with pytest.raises(ValueError, match='separate'):
            module.generate(root, output)


def test_only_generated_java_under_the_output_tree_is_ever_removed(tmp_path):
    """Pruning must not reach other build outputs that happen to share the directory."""
    module = generator()
    root, output = sources(tmp_path), tmp_path / 'out'
    module.generate(root, output)
    keep = output / 'dev' / 'worldprompt' / 'notes.txt'
    keep.write_text('not mine\n', encoding='utf-8')
    module.generate(root, output)
    assert keep.is_file()


def test_a_foreign_java_file_is_left_alone_and_reported(tmp_path, capsys):
    """The generator prunes only what it wrote, so a file it never wrote is never deleted.

    Deleting on sight would mean a mistyped --output-dir destroys a source tree. A file
    it does not own is reported instead, so a genuinely stale one is still visible.
    """
    module = generator()
    root, output = sources(tmp_path), tmp_path / 'out'
    module.generate(root, output)
    foreign = output / 'dev' / 'worldprompt' / 'HandWritten.java'
    foreign.write_text('class HandWritten {}\n', encoding='utf-8')
    module.generate(root, output)
    assert foreign.is_file()
    assert 'not written by this generator' in capsys.readouterr().out


def test_a_hand_written_tree_survives_a_mistaken_output_dir_on_every_run(tmp_path):
    """A mistyped --output-dir must never delete a hand-written source tree.

    Not just on the first run: ownership is recorded, so a file this generator never
    wrote is never a pruning candidate, however many times it is pointed there.
    """
    module = generator()
    root = sources(tmp_path)
    handwritten = tmp_path / 'neoforge-gametest'
    (handwritten / 'dev' / 'worldprompt').mkdir(parents=True)
    kept = handwritten / 'dev' / 'worldprompt' / 'NeoForgeTestPermissionHandler.java'
    kept.write_text('class NeoForgeTestPermissionHandler {}\n', encoding='utf-8')
    for run in range(3):
        module.generate(root, handwritten)
        assert kept.is_file(), f'the hand-written source was deleted on run {run + 1}'


def test_an_output_directory_without_a_manifest_prunes_nothing_on_that_run(tmp_path, capsys):
    """Upgrading mid-stream must not break a build, and must not guess at ownership.

    An output directory from before the manifest existed holds generated files this
    run cannot prove it owns, so they survive and are reported rather than deleted.
    """
    module = generator()
    root, output = sources(tmp_path), tmp_path / 'out'
    module.generate(root, output)
    (output / module.MANIFEST).unlink()                   # as an older generator left it
    orphan = output / 'dev' / 'worldprompt' / 'LongGoneGameTest.java'
    orphan.write_text('class LongGoneGameTest {}' + LF, encoding='utf-8')
    module.generate(root, output)
    assert orphan.is_file(), 'ownership was never recorded, so nothing may be pruned'
    assert 'not written by this generator' in capsys.readouterr().out
    assert (output / module.MANIFEST).is_file()
    # Ownership is known from here, so a renamed test is pruned as usual.
    (root / 'dev' / 'worldprompt' / 'KeptGameTest.java').unlink()
    write_test(root, 'RenamedGameTest.java', 'keptScenario')
    module.generate(root, output)
    assert not (output / 'dev' / 'worldprompt' / 'KeptGameTest.java').exists()


def test_a_manifest_can_only_name_java_files_inside_the_output_tree(tmp_path):
    """The manifest is an ordinary file in a build directory, so it is not trusted.

    A corrupt, hand-edited or copied-in manifest must not be able to turn the generator
    into a delete-anything tool.
    """
    module = generator()
    root, output = sources(tmp_path), tmp_path / 'out'
    module.generate(root, output)
    sibling = tmp_path / 'PRECIOUS.java'
    sibling.write_text('class PRECIOUS {}' + LF, encoding='utf-8')
    secret = tmp_path / 'notes.txt'
    secret.write_text('keep me' + LF, encoding='utf-8')
    (output / module.MANIFEST).write_text(
        '# hostile' + LF
        + '../PRECIOUS.java' + LF                         # escapes upward
        + str(sibling) + LF                               # absolute
        + '../notes.txt' + LF                             # not a Java file
        + 'dev/worldprompt/KeptGameTest.java' + LF,       # legitimately owned
        encoding='utf-8')
    (root / 'dev' / 'worldprompt' / 'KeptGameTest.java').unlink()
    write_test(root, 'RenamedGameTest.java', 'keptScenario')
    module.generate(root, output)
    assert sibling.is_file(), 'a manifest entry outside the output tree was acted on'
    assert secret.is_file()
    # The one legitimate entry still prunes, so the guard is not simply refusing everything.
    assert not (output / 'dev' / 'worldprompt' / 'KeptGameTest.java').exists()


def test_an_unreadable_manifest_owns_nothing(tmp_path):
    """A corrupt manifest must fail closed: no ownership, so nothing is pruned."""
    module = generator()
    root, output = sources(tmp_path), tmp_path / 'out'
    module.generate(root, output)
    stale = output / 'dev' / 'worldprompt' / 'RemovedGameTest.java'
    assert stale.is_file()
    (root / 'dev' / 'worldprompt' / 'RemovedGameTest.java').unlink()   # it is now genuinely stale
    (output / module.MANIFEST).write_bytes(bytes([0xff, 0xfe, 0x00]) + b'binary garbage')
    module.generate(root, output)                                      # must not raise
    assert stale.is_file(), 'a corrupt manifest must not be read as ownership'
    assert (output / module.MANIFEST).is_file(), 'and it is replaced for next time'


def test_a_failed_generation_claims_no_ownership(tmp_path):
    """A run that fails validation must not record files it may not own."""
    module = generator()
    root, output = sources(tmp_path), tmp_path / 'out'
    (root / 'dev' / 'worldprompt' / 'BrokenGameTest.java').write_text(
        'package dev.worldprompt;\n'
        'import net.fabricmc.fabric.api.gametest.v1.GameTest;\n'
        '@GameTest\n'
        'public final class BrokenGameTest { }\n', encoding='utf-8')
    with pytest.raises(ValueError):
        module.generate(root, output)
    assert not (output / module.MANIFEST).exists()


def test_the_real_fabric_tests_generate_and_name_the_stand_in_structure(tmp_path):
    module = generator()
    output = tmp_path / 'out'
    assert module.generate(FABRIC_GAMETESTS, output) > 0
    registry = (output / 'dev/worldprompt/WorldPromptGameTests.java').read_text(encoding='utf-8')
    # The comment that explains why NeoForge carries its own empty structure.
    assert module.EMPTY_STRUCTURE_RESOURCE in registry
    assert 'net.fabricmc.' not in registry

    # The Fabric-only permission listener is removed and the NeoForge check injected,
    # which is the adaptation the output depends on most.
    permission = (output / 'dev/worldprompt/PermissionNodeGameTest.java').read_text(encoding='utf-8')
    assert 'PermissionCheckEvent' not in permission and 'net.fabricmc.' not in permission
    assert 'NeoForgeTestPermissionHandler.verifyNativeDefaults(context);' in permission
    assert permission.count('context.succeed();') == 1
