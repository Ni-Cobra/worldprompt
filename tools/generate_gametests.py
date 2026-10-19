#!/usr/bin/env python3
"""Copy Fabric GameTest bodies and generate NeoForge 26.3 test-instance registration."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path


TEST_METHOD = re.compile(
    r"@GameTest(?:\s*\(([^)]*)\))?\s*"
    r"public\s+void\s+(\w+)\s*\(\s*GameTestHelper\s+\w+\s*\)"
)
ANNOTATION = re.compile(r"(?m)^\s*@GameTest(?:\s*\([^)]*\))?\s*\r?\n")
FABRIC_GAMETEST_IMPORT = re.compile(
    r"(?m)^import\s+net\.fabricmc\.fabric\.api\.gametest\.v1\.GameTest;\s*\r?\n"
)
FABRIC_WORLD_EDIT = (
    'net.fabricmc.loader.api.FabricLoader.getInstance().isModLoaded("worldedit")'
)
FABRIC_EMPTY_STRUCTURE = "fabric-gametest-api-v1:empty"
TEST_MOD_ID = "worldprompt"


@dataclass(frozen=True)
class TestMetadata:
    environment: str = "minecraft:default"
    max_ticks: int = 20
    setup_ticks: int = 0
    required: bool = True
    rotation: str = "NONE"
    manual_only: bool = False
    max_attempts: int = 1
    required_successes: int = 1
    sky_access: bool = False
    padding: int = 1


def parse_metadata(arguments: str | None, path: Path) -> TestMetadata:
    values: dict[str, str] = {}
    if arguments:
        for argument in arguments.split(","):
            if "=" not in argument:
                raise ValueError(f"unrecognized @GameTest argument {argument!r} in {path}")
            name, value = (part.strip() for part in argument.split("=", 1))
            if name in values:
                raise ValueError(f"duplicate @GameTest argument {name!r} in {path}")
            values[name] = value

    supported = {
        "maxTicks", "setupTicks", "required", "rotation", "manualOnly", "maxAttempts",
        "requiredSuccesses", "skyAccess", "padding", "environment", "dimension", "structure",
    }
    unknown = set(values) - supported
    if unknown:
        raise ValueError(f"unsupported @GameTest arguments {sorted(unknown)} in {path}")

    def integer(name: str, default: int) -> int:
        try:
            result = int(values.get(name, str(default)))
        except ValueError as error:
            raise ValueError(f"@GameTest {name} must be an integer in {path}") from error
        if result < 0:
            raise ValueError(f"@GameTest {name} cannot be negative in {path}")
        return result

    def boolean(name: str, default: bool) -> bool:
        value = values.get(name, str(default).lower())
        if value not in {"true", "false"}:
            raise ValueError(f"@GameTest {name} must be true or false in {path}")
        return value == "true"

    def string(name: str, default: str) -> str:
        value = values.get(name, f'"{default}"')
        if not (value.startswith('"') and value.endswith('"')):
            raise ValueError(f"@GameTest {name} must be a string literal in {path}")
        return value[1:-1]

    environment = string("environment", "minecraft:default")
    dimension = string("dimension", "minecraft:overworld")
    structure = string("structure", FABRIC_EMPTY_STRUCTURE)
    if environment not in {"minecraft:default", "worldprompt-test:catalog"}:
        raise ValueError(f"custom GameTest environment {environment!r} needs a NeoForge environment mapping")
    if dimension != "minecraft:overworld":
        raise ValueError(f"custom GameTest dimension {dimension!r} needs a NeoForge dimension mapping")
    if structure != FABRIC_EMPTY_STRUCTURE:
        raise ValueError(f"custom GameTest structure {structure!r} needs a NeoForge resource mapping")

    rotation = values.get("rotation", "Rotation.NONE").rsplit(".", 1)[-1]
    if rotation not in {"NONE", "CLOCKWISE_90", "CLOCKWISE_180", "COUNTERCLOCKWISE_90"}:
        raise ValueError(f"unsupported @GameTest rotation {rotation!r} in {path}")
    return TestMetadata(
        environment=environment,
        max_ticks=integer("maxTicks", 20),
        setup_ticks=integer("setupTicks", 0),
        required=boolean("required", True),
        rotation=rotation,
        manual_only=boolean("manualOnly", False),
        max_attempts=integer("maxAttempts", 1),
        required_successes=integer("requiredSuccesses", 1),
        sky_access=boolean("skyAccess", False),
        padding=integer("padding", 1),
    )


def camel_to_snake(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"([A-Z])([A-Z][a-z])", r"\1_\2", value)
    return value.lower()


def remove_permission_event(source: str, path: Path) -> str:
    if path.name != "PermissionNodeGameTest.java":
        return source
    source = re.sub(
        r"(?m)^import\s+(?:me\.lucko\.fabric\.api\.permissions\.v0\.PermissionCheckEvent|"
        r"net\.fabricmc\.fabric\.api\.util\.TriState);\s*\r?\n",
        "",
        source,
    )
    source = source.replace(
        "private static final Map<UUID, Map<String, Boolean>> GRANTS = new ConcurrentHashMap<>();",
        "static final Map<UUID, Map<String, Boolean>> GRANTS = new ConcurrentHashMap<>();",
    )
    start = source.find("\n    static {\n        PermissionCheckEvent.EVENT.register(")
    end = source.find("\n    @GameTest", start)
    if start < 0 or end < 0:
        raise ValueError(f"expected Fabric permission listener block in {path}")
    return source[:start] + source[end:]


def generated_registry(tests: list[tuple[str, str, TestMetadata]]) -> str:
    registrations = []
    for class_name, method_name, metadata in tests:
        environment = "catalogEnvironment" if metadata.environment == "worldprompt-test:catalog" else "environment"
        test_id = camel_to_snake(class_name.removesuffix("Test")) + "_" + camel_to_snake(method_name)
        registrations.append(
            f'''        register(event, {environment}, "{test_id}", new TestData<>({environment}, Level.OVERWORLD,
                EMPTY_STRUCTURE, {metadata.max_ticks}, {metadata.setup_ticks}, {str(metadata.required).lower()},
                Rotation.{metadata.rotation}, {str(metadata.manual_only).lower()}, {metadata.max_attempts},
                {metadata.required_successes}, {str(metadata.sky_access).lower()}, {metadata.padding}),
                {class_name}.class, "{method_name}");'''
        )
    body = "\n".join(registrations)
    return f'''package dev.worldprompt;

import net.minecraft.core.Holder;
import net.minecraft.gametest.framework.GameTestInstance;
import net.minecraft.gametest.framework.TestData;
import net.minecraft.gametest.framework.TestEnvironmentDefinition;
import net.minecraft.resources.Identifier;
import net.minecraft.world.level.Level;
import net.minecraft.world.level.block.Rotation;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.event.RegisterGameTestsEvent;

/** Generated from the Fabric GameTest method annotations and bodies. */
@EventBusSubscriber(modid = "{TEST_MOD_ID}")
public final class WorldPromptGameTests {{
    private static final String MOD_ID = "{TEST_MOD_ID}";
    private static final Identifier EMPTY_STRUCTURE = Identifier.fromNamespaceAndPath(MOD_ID, "empty");

    private WorldPromptGameTests() {{
    }}

    @SubscribeEvent
    public static void register(RegisterGameTestsEvent event) {{
        Holder<TestEnvironmentDefinition<?>> environment = event.registerEnvironment(
                Identifier.fromNamespaceAndPath(MOD_ID, "default"),
                new TestEnvironmentDefinition.AllOf());
        // Separate environment => separate sequential batch. Its real block-write sounds must not reach
        // the original distance/sound scenario's mock listeners in the nearby GameTest grid.
        Holder<TestEnvironmentDefinition<?>> catalogEnvironment = event.registerEnvironment(
                Identifier.fromNamespaceAndPath("worldprompt-test", "catalog"),
                new TestEnvironmentDefinition.AllOf());
{body}
    }}

    private static void register(RegisterGameTestsEvent event,
                                 Holder<TestEnvironmentDefinition<?>> environment,
                                 String name,
                                 TestData<Holder<TestEnvironmentDefinition<?>>> data,
                                 Class<?> testClass,
                                 String methodName) {{
        Identifier id = Identifier.fromNamespaceAndPath(MOD_ID, name);
        event.registerTest(id, testData -> new RegisteredMethodGameTestInstance(testData, testClass, methodName), data);
    }}
}}
'''


def generate(source_root: Path, output_dir: Path) -> int:
    source_root = source_root.resolve()
    output_dir = output_dir.resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"Fabric GameTest source directory does not exist: {source_root}")

    tests: list[tuple[str, str, TestMetadata]] = []
    sources = sorted(source_root.rglob("*.java"))
    if not sources:
        raise ValueError(f"no Java sources found under {source_root}")

    for path in sources:
        original = path.read_text(encoding="utf-8")
        methods = list(TEST_METHOD.finditer(original))
        annotation_count = len(ANNOTATION.findall(original))
        if annotation_count != len(methods):
            raise ValueError(
                f"found {annotation_count} @GameTest annotations but parsed {len(methods)} methods in {path}"
            )
        for method in methods:
            tests.append((path.stem, method.group(2), parse_metadata(method.group(1), path)))

        source = remove_permission_event(original, path)
        source = FABRIC_GAMETEST_IMPORT.sub("", source)
        source = ANNOTATION.sub("", source)
        source = source.replace(FABRIC_WORLD_EDIT,
                                'net.neoforged.fml.ModList.get().isLoaded("worldedit")')
        if path.name == "PermissionNodeGameTest.java":
            succeed = "        context.succeed();"
            if source.count(succeed) != 1:
                raise ValueError(f"expected one permission GameTest success point in {path}")
            source = source.replace(
                succeed,
                "        NeoForgeTestPermissionHandler.verifyNativeDefaults(context);\n" + succeed,
                1,
            )
        if "net.fabricmc." in source or "@GameTest" in source:
            raise ValueError(f"unconverted Fabric GameTest dependency remains in {path}")
        relative = path.relative_to(source_root)
        target = output_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8", newline="\n")

    if not tests:
        raise ValueError(f"no @GameTest methods found under {source_root}")
    if any(metadata.environment == "worldprompt-test:catalog" for _, _, metadata in tests):
        environment = source_root.parent / "resources/data/worldprompt-test/test_environment/catalog.json"
        if json.loads(environment.read_text(encoding="utf-8")) != {"type": "minecraft:all_of", "definitions": []}:
            raise ValueError("the catalog GameTest environment changed; update its native NeoForge mapping")
    if len({(class_name, method_name) for class_name, method_name, _ in tests}) != len(tests):
        raise ValueError("duplicate @GameTest methods found")

    registry = output_dir / "dev" / "worldprompt" / "WorldPromptGameTests.java"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(generated_registry(tests), encoding="utf-8", newline="\n")
    return len(tests)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True,
                        help="Fabric GameTest Java source directory")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="generated NeoForge GameTest Java source directory")
    args = parser.parse_args()
    count = generate(args.source_root, args.output_dir)
    print(f"Generated NeoForge registrations for {count} Fabric GameTest methods.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
