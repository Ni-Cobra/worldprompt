package dev.worldprompt;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.world.level.block.state.properties.Property;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.BlockStateProperties;
import net.minecraft.core.Direction;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

/** Property inheritance and agreement between the live block registry and Core's bundled schema. */
public final class BlockPropertyRegistryTest {
    @GameTest
    public void randomReplacementCarriesOnlyCompatibleUnspecifiedProperties(GameTestHelper context) {
        var source = Blocks.OAK_STAIRS.defaultBlockState()
                .setValue(BlockStateProperties.HORIZONTAL_FACING, Direction.WEST)
                .setValue(BlockStateProperties.HALF, net.minecraft.world.level.block.state.properties.Half.TOP);
        var target = Blocks.BIRCH_STAIRS.defaultBlockState();
        var inherited = ReplayPlacer.inheritProperties(source, target, Set.of());
        context.assertTrue(inherited.getValue(BlockStateProperties.HORIZONTAL_FACING) == Direction.WEST
                        && inherited.getValue(BlockStateProperties.HALF) == net.minecraft.world.level.block.state.properties.Half.TOP,
                "Compatible orientation survives a random replacement choice.");
        var explicit = ReplayPlacer.inheritProperties(source,
                target.setValue(BlockStateProperties.HORIZONTAL_FACING, Direction.EAST), Set.of("facing"));
        context.assertTrue(explicit.getValue(BlockStateProperties.HORIZONTAL_FACING) == Direction.EAST,
                "Explicit target properties override inherited source properties.");
        context.succeed();
    }

    /**
     * Core plans and validates against its bundled copy of the block registry. Fail when the live game
     * registry has drifted from it: a block or property that exists on one side only, different values,
     * or a different default state.
     */
    @GameTest
    public void liveBlockRegistryMatchesCoreBundledSchema(GameTestHelper context) throws Exception {
        Path schemas = schemaDirectory();
        JsonObject properties = read(schemas.resolve("block-properties-26.3.json"));
        JsonObject defaults = read(schemas.resolve("block-defaults-26.3.json"));
        List<String> problems = new ArrayList<>();
        Set<String> live = new TreeSet<>();
        for (Block block : BuiltInRegistries.BLOCK) {
            String id = BuiltInRegistries.BLOCK.getKey(block).toString();
            if (!id.startsWith("minecraft:")) continue;
            live.add(id);
            JsonObject expectedValues = properties.has(id) ? properties.getAsJsonObject(id) : null;
            JsonObject expectedDefaults = defaults.has(id) ? defaults.getAsJsonObject(id) : null;
            if (expectedValues == null || expectedDefaults == null) { problems.add(id + " is missing from Core's schema"); continue; }
            Set<String> names = new TreeSet<>();
            for (Property<?> property : block.getStateDefinition().getProperties()) {
                names.add(property.getName());
                JsonArray bundled = expectedValues.getAsJsonArray(property.getName());
                if (bundled == null) continue;
                Set<String> liveValues = new TreeSet<>(), coreValues = new TreeSet<>();
                property.getPossibleValues().forEach(value -> liveValues.add(valueName(property, value)));
                bundled.forEach(value -> coreValues.add(value.getAsString()));
                if (!liveValues.equals(coreValues))
                    problems.add(id + "[" + property.getName() + "] values: game " + liveValues + " vs Core " + coreValues);
                String liveDefault = valueOf(block.defaultBlockState(), property);
                if (!expectedDefaults.has(property.getName()) || !liveDefault.equals(expectedDefaults.get(property.getName()).getAsString()))
                    problems.add(id + "[" + property.getName() + "] default: game " + liveDefault + " vs Core "
                            + expectedDefaults.get(property.getName()));
            }
            if (!names.equals(new TreeSet<>(expectedValues.keySet())))
                problems.add(id + " properties: game " + names + " vs Core " + new TreeSet<>(expectedValues.keySet()));
            if (!names.equals(new TreeSet<>(expectedDefaults.keySet())))
                problems.add(id + " default properties: game " + names + " vs Core " + new TreeSet<>(expectedDefaults.keySet()));
        }
        for (String id : properties.keySet()) if (!live.contains(id)) problems.add(id + " is in Core's schema but not in the game");
        for (String id : defaults.keySet()) if (!live.contains(id)) problems.add(id + " has defaults in Core's schema but is not in the game");
        context.assertTrue(live.contains("minecraft:crimson_stem"), "Vanilla registry was read");
        context.assertTrue(problems.isEmpty(), problems.size() + " difference(s) between the game's block registry and Core's bundled schema; "
                + "regenerate it with scripts/import_block_report.py. First: " + problems.subList(0, Math.min(10, problems.size())));
        context.succeed();
    }

    private static <T extends Comparable<T>> String valueName(Property<T> property, Object value) {
        return property.getName(property.getValueClass().cast(value));
    }
    private static <T extends Comparable<T>> String valueOf(BlockState state, Property<T> property) {
        return property.getName(state.getValue(property));
    }
    private static JsonObject read(Path file) throws Exception {
        return JsonParser.parseString(Files.readString(file)).getAsJsonObject();
    }
    /** Gradle passes -Dworldprompt.schemas; otherwise search upward from the run directory for the Core checkout. */
    private static Path schemaDirectory() {
        String configured = System.getProperty("worldprompt.schemas");
        if (configured != null && !configured.isBlank()) return Path.of(configured);
        for (Path dir = Path.of("").toAbsolutePath(); dir != null; dir = dir.getParent()) {
            Path candidate = dir.resolve("src/worldprompt_core/schemas");
            if (Files.isDirectory(candidate)) return candidate;
        }
        throw new IllegalStateException("Core's schemas directory was not found; pass -Dworldprompt.schemas=<path to src/worldprompt_core/schemas>.");
    }
}
