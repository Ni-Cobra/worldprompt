package dev.worldprompt;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import dev.worldprompt.mycelium.block_catalog.v2.model.BlockCatalog;
import dev.worldprompt.mycelium.bridges.block_catalog.MinecraftBlockCatalog;
import dev.worldprompt.mycelium.bridges.block_catalog.conformance.CatalogCases;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.core.BlockPos;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.world.level.block.Blocks;

/** Real registry, wire capture and placement tests with an actually registered non-vanilla block. */
public final class BlockCatalogGameTest {
    @GameTest
    public void liveCatalogIncludesNativeDomainsDefaultsAndModNamespaces(GameTestHelper context) {
        var port = new MinecraftBlockCatalog();
        for (var test : CatalogCases.cases(port)) test.run();
        var snapshot = port.capture();
        String id = CatalogFixtureBlock.ID.toString();
        context.assertTrue(snapshot.normalize(id, Map.of()).equals(Map.of("powered", "false", "label", "amber")),
                "A registered mod block supplies its native default, not a vanilla fallback.");
        context.assertTrue(snapshot.normalize(id, Map.of("powered", "true")).equals(Map.of("powered", "true", "label", "amber")),
                "A mod block's explicit native property is accepted.");
        var capture = BlockCatalogTransport.encode(snapshot);
        context.assertTrue(capture.live() && capture.unusableError() == null,
                "A normal server's registry produces a catalog Core can use.");
        var wire = new Gson().toJsonTree(capture.transport()).getAsJsonObject();
        var entry = wire.getAsJsonObject("blocks").getAsJsonObject(id);
        context.assertTrue(entry.getAsJsonObject("properties").getAsJsonArray("powered").size() == 2
                        && entry.getAsJsonObject("defaults").get("powered").getAsString().equals("false"),
                "The actual protocol representation retains the native domain and default.");
        assertNativeOrderReachesTheWire(context, snapshot, wire);
        context.succeed();
    }

    /**
     * block.catalog@2 law C6, end to end on the real registry: the game declares domains that are not in
     * alphabetical order, and WorldPrompt's own protocol encoding must not reorder them either. Checked
     * over whatever the running game actually declares, so no block or property is named here.
     */
    private static void assertNativeOrderReachesTheWire(GameTestHelper context, BlockCatalog snapshot, JsonObject wire) {
        int checked = 0;
        JsonObject blocks = wire.getAsJsonObject("blocks");
        for (Map.Entry<String, BlockCatalog.Entry> block : snapshot.blocks().entrySet()) {
            JsonObject json = blocks.getAsJsonObject(block.getKey());
            if (json == null) continue;
            for (Map.Entry<String, List<String>> property : block.getValue().properties().entrySet()) {
                List<String> declared = property.getValue();
                List<String> alphabetical = new ArrayList<>(declared);
                alphabetical.sort(null);
                if (alphabetical.equals(declared)) continue;
                List<String> sent = new ArrayList<>();
                json.getAsJsonObject("properties").getAsJsonArray(property.getKey())
                        .forEach(value -> sent.add(value.getAsString()));
                context.assertTrue(declared.equals(sent), "The wire keeps the registry's order for "
                        + block.getKey() + "/" + property.getKey() + ": expected " + declared + " but sent " + sent);
                checked++;
            }
        }
        context.assertTrue(checked > 0,
                "The game declares at least one property domain out of alphabetical order, so this proves something.");
    }

    @GameTest(maxTicks = 100000, environment = "worldprompt-test:catalog")
    public void submittedCatalogAndModdedPlacementRoundTrip(GameTestHelper context) throws Exception {
        String id = CatalogFixtureBlock.ID.toString();
        String state = id + "[label=blue,powered=true]";
        FakeCore core = new FakeCore(request -> new FakeCore.Result(List.of(1, 1, 1), List.of(state), "Modded probe",
                List.of(new FakeCore.Op(Map.of("op", "fill", "min", List.of(0, 0, 0),
                        "max", List.of(0, 0, 0), "block", state), 0, 1, 0))));
        AdapterConfig config = new AdapterConfig();
        core.connect(config);
        config.pollMilliseconds = 10;
        PromptManager manager = new PromptManager(context.getLevel().getServer(), config);
        context.runBeforeTestEnd(() -> { manager.close(); core.close(); });
        var player = context.makeMockServerPlayerInLevel();
        BlockPos pos = context.absolutePos(new BlockPos(1, 2, 1));
        context.getLevel().setBlock(pos, Blocks.GOLD_BLOCK.defaultBlockState(), 3);
        manager.corner(player, true, pos);
        manager.corner(player, false, pos);
        manager.submit(player, "Place the registered test block");
        context.onEachTick(() -> {
            manager.tick();
            TickBudget.within(context, 3000, () -> "modded catalog round trip: "
                    + manager.active.values().stream().map(job -> job.mode + ":" + job.status).toList());
            if (!manager.active.isEmpty()) return;
            context.assertTrue(core.lastRequest != null && core.modelPosts.get() == 1,
                    "The normal job path submitted exactly one immutable request.");
            var entry = core.lastRequest.getAsJsonObject("block_catalog").getAsJsonObject("blocks").getAsJsonObject(id);
            context.assertTrue(entry != null && entry.getAsJsonObject("defaults").get("powered").getAsString().equals("false"),
                    "The live mod block catalog crossed the actual HTTP submit path.");
            var placed = context.getLevel().getBlockState(pos);
            context.assertTrue(placed.getBlock() instanceof CatalogFixtureBlock && placed.getValue(CatalogFixtureBlock.POWERED)
                            && placed.getValue(CatalogFixtureBlock.LABEL) == CatalogFixtureBlock.Label.BLUE,
                    "The returned modded block state was placed by the normal replay path.");
            manager.undo(player);
            context.assertTrue(context.getLevel().getBlockState(pos).is(Blocks.GOLD_BLOCK),
                    "Undo restores the original state after modded placement.");
            context.succeed();
        });
    }
}
