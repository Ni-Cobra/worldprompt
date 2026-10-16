package dev.worldprompt;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.core.BlockPos;
import net.minecraft.core.component.DataComponents;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.network.chat.Component;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.server.network.Filterable;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.item.component.WritableBookContent;
import net.minecraft.world.item.component.WrittenBookContent;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.LecternBlock;
import net.minecraft.world.level.block.entity.LecternBlockEntity;
import net.minecraft.world.phys.AABB;

/** Prompt lecterns in a real level: detection, the Core payload, preservation and undo. No model calls. */
public final class LecternPromptGameTest {
    /** The flow needs under a hundred ticks; a stall fails by tick count, not by the machine's speed. */
    private static final int TICK_BUDGET = 1200;

    @GameTest
    public void captureFindsOnlyPromptBooksInsideTheSelection(GameTestHelper context) {
        ServerLevel level = context.getLevel();
        BlockPos low = context.absolutePos(new BlockPos(1, 1, 1));
        RegionBounds bounds = RegionBounds.between(low.getX(), low.getY(), low.getZ(),
                low.getX() + 4, low.getY() + 1, low.getZ(), 64);
        for (int x = 0; x <= 5; x++) for (int y = 0; y <= 1; y++)
            level.setBlock(low.offset(x, y, 0), Blocks.AIR.defaultBlockState(), 3);
        lectern(level, low, written(" Prompt ", List.of(page("Kitchen: stone counters"),
                new Filterable<>(Component.literal("raw second page"), Optional.of(Component.literal("****"))))));
        lectern(level, low.offset(1, 0, 0), named(writable(List.of("Bedroom", "", "Red bed")), "PROMPT"));
        lectern(level, low.offset(2, 0, 0), named(written("notes", List.of(page("Hall"))), "prompt"));
        lectern(level, low.offset(3, 0, 0), written("notes", List.of(page("Wrong title"))));
        lectern(level, low.offset(4, 0, 0), writable(List.of("Unnamed book and quill")));
        lectern(level, low.offset(0, 1, 0), named(writable(List.of("Wrong name")), "prompt notes"));
        level.setBlock(low.offset(1, 1, 0), Blocks.LECTERN.defaultBlockState(), 3);
        lectern(level, low.offset(2, 1, 0), named(writable(List.of(" ", "")), "prompt"));
        lectern(level, low.offset(3, 1, 0), writable(List.of("", "Prompt: Attic\nlow roof")));
        lectern(level, low.offset(4, 1, 0), written("notes", List.of(page("prompting is fun"))));
        lectern(level, low.offset(5, 0, 0), written("prompt", List.of(page("Outside the selection"))));

        RegionSnapshot snapshot = new RegionSnapshot(level, bounds);
        int ticks = 0;
        while (!snapshot.capture(level, 3)) ticks++;
        context.assertTrue(ticks > 1, "Detection runs inside the budgeted multi-tick capture.");
        snapshot.lecterns.validate();
        // Core's lecterns field is the mod's real output, so assert on it rather than on internal records.
        JsonArray found = new Gson().toJsonTree(snapshot.lecterns.transport()).getAsJsonArray();
        context.assertTrue(found.size() == 4,
                "Signed title, renamed books and text starting with the prompt word count: " + found);
        context.assertTrue(local(found, 0).equals("[0,0,0]") && local(found, 1).equals("[1,0,0]") && local(found, 2).equals("[2,0,0]")
                        && local(found, 3).equals("[3,1,0]"),
                "Local coordinates follow the plan's [x, y, z] axes: " + found);
        context.assertTrue(found.get(0).getAsJsonObject().get("world_position").toString()
                        .equals("[" + low.getX() + "," + low.getY() + "," + low.getZ() + "]"),
                "World coordinates are retained.");
        context.assertTrue(text(found, 0).equals("--- page 1 ---\nKitchen: stone counters\n\n--- page 2 ---\nraw second page"),
                "Written pages use raw, unfiltered text with page breaks: " + text(found, 0));
        context.assertTrue(text(found, 1).equals("--- page 1 ---\nBedroom\n\n--- page 3 ---\nRed bed"),
                "Book and quill pages skip blank pages but keep page numbers: " + text(found, 1));
        context.assertTrue(text(found, 2).equals("Hall"), "A single page has no page marker.");
        context.assertTrue(text(found, 3).equals("Attic\nlow roof"),
                "The leading prompt word is removed from a book detected by its text: " + text(found, 3));
        String world = "(" + low.getX() + ", " + low.getY() + ", " + low.getZ() + ")";
        List<String> summary = snapshot.lecterns.summary();
        context.assertTrue(summary.size() == 2 && summary.get(0).startsWith("4 prompt lecterns: " + world + ", ")
                        && summary.get(1).startsWith("Ignored empty prompt book: "),
                "Chat summary lists the detected lecterns and the empty book: " + summary);
        List<String> warnings = snapshot.lecterns.warnings();
        context.assertTrue(warnings.size() == 1 && warnings.getFirst().startsWith("4 lecterns hold books not marked as prompts"),
                "Books with text that are not prompts are warned about; empty ones are not: " + warnings);
        JsonArray payload = found;
        context.assertTrue(payload.size() == 4 && payload.get(1).getAsJsonObject().get("position").toString().equals("[1,0,0]")
                        && payload.get(1).getAsJsonObject().keySet().equals(java.util.Set.of("position", "world_position", "text")),
                "Payload entries carry local position, world position and text: " + payload);
        context.succeed();
    }

    @GameTest(maxTicks = 2400)
    public void promptSendsLecternsPreservesUntouchedBooksAndUndoRestoresOverwrittenOnes(GameTestHelper context) throws Exception {
        ServerLevel level = context.getLevel();
        // High above the structure: operation sounds and poofs stay out of the packet-counting flow test's range.
        BlockPos low = context.absolutePos(new BlockPos(1, 104, 4));
        // x0: prompt lectern the plan overwrites · x1: dirt the plan overwrites
        // x2: prompt lectern the plan never touches · x3: untouched dirt
        lectern(level, low, written("prompt", List.of(page("Overwritten room"))));
        level.setBlock(low.offset(1, 0, 0), Blocks.DIRT.defaultBlockState(), 3);
        lectern(level, low.offset(2, 0, 0), named(writable(List.of("Keep this room")), "Prompt"));
        level.setBlock(low.offset(3, 0, 0), Blocks.DIRT.defaultBlockState(), 3);
        FakeCore core = new FakeCore(LecternPromptGameTest::plan);
        AdapterConfig config = new AdapterConfig();
        core.connect(config);
        config.blocksPerTick = 2;
        config.pollMilliseconds = 10;
        PromptManager manager = new PromptManager(level.getServer(), config);
        boolean[] done = {false};
        Runnable cleanup = () -> { if (!done[0]) { done[0] = true; manager.close(); core.close(); } };
        context.runBeforeTestEnd(cleanup);
        ServerPlayer player = context.makeMockServerPlayerInLevel();
        Chat chat = new Chat(player);
        manager.corner(player, true, low);
        manager.corner(player, false, low.offset(3, 0, 0));
        manager.submit(player, "furnish the rooms");
        AABB around = new AABB(low.getX() - 3, low.getY() - 3, low.getZ() - 3, low.getX() + 7, low.getY() + 4, low.getZ() + 4);
        context.onEachTick(() -> {
            if (done[0]) return;
            try {
                manager.tick();
                chat.drain();
                TickBudget.within(context, TICK_BUDGET, () -> "lectern flow, chat so far: " + chat.messages);
                if (!chat.messages.stream().anyMatch(text -> text.contains("Build complete"))) return;
                String first = "(" + low.getX() + ", " + low.getY() + ", " + low.getZ() + ")";
                String second = "(" + (low.getX() + 2) + ", " + low.getY() + ", " + low.getZ() + ")";
                context.assertTrue(chat.messages.stream().anyMatch(text -> text.contains("2 prompt lecterns: " + first + ", " + second)),
                        "The player is told which prompt lecterns were detected: " + chat.messages);
                JsonArray sent = core.lastRequest.getAsJsonArray("lecterns");
                context.assertTrue(sent.size() == 2, "Both prompt lecterns reach Core: " + sent);
                JsonObject a = sent.get(0).getAsJsonObject(), b = sent.get(1).getAsJsonObject();
                context.assertTrue(a.get("position").toString().equals("[0,0,0]") && b.get("position").toString().equals("[2,0,0]")
                                && a.get("world_position").toString().equals("[" + low.getX() + "," + low.getY() + "," + low.getZ() + "]")
                                && a.get("text").getAsString().equals("Overwritten room")
                                && b.get("text").getAsString().equals("Keep this room"),
                        "Payload carries local and world coordinates with each book's text: " + sent);
                JsonArray origin = core.lastRequest.getAsJsonObject("viewpoint").getAsJsonArray("selection_origin");
                context.assertTrue(origin.get(0).getAsInt() == low.getX(), "Local positions are relative to the selection origin.");

                context.assertTrue(level.getBlockState(low).is(Blocks.STONE) && level.getBlockState(low.offset(1, 0, 0)).is(Blocks.STONE)
                        && level.getBlockState(low.offset(3, 0, 0)).is(Blocks.DIRT), "The plan was applied as written.");
                assertBook(context, level, low.offset(2, 0, 0), "Keep this room", "An untouched lectern keeps its block and book.");
                context.assertTrue(level.getEntitiesOfClass(ItemEntity.class, around).isEmpty(),
                        "Overwriting a lectern does not drop its book into the world.");

                manager.undo(player);
                assertBook(context, level, low, "Overwritten room", "Undo restores an overwritten lectern and its book.");
                assertBook(context, level, low.offset(2, 0, 0), "Keep this room", "Undo keeps the untouched lectern's book.");
                context.assertTrue(level.getBlockState(low.offset(1, 0, 0)).is(Blocks.DIRT), "Undo restores overwritten blocks.");
                context.assertTrue(level.getEntitiesOfClass(ItemEntity.class, around).isEmpty(),
                        "Undo leaves no duplicate book items.");
                cleanup.run();
                context.succeed();
            } catch (Throwable error) {
                cleanup.run();
                context.fail(error.toString());
            }
        });
    }

    private static String local(JsonArray lecterns, int index) {
        return lecterns.get(index).getAsJsonObject().get("position").toString();
    }
    private static String text(JsonArray lecterns, int index) {
        return lecterns.get(index).getAsJsonObject().get("text").getAsString();
    }
    private static void assertBook(GameTestHelper context, ServerLevel level, BlockPos pos, String text, String message) {
        context.assertTrue(level.getBlockState(pos).is(Blocks.LECTERN) && level.getBlockState(pos).getValue(LecternBlock.HAS_BOOK)
                && level.getBlockEntity(pos) instanceof LecternBlockEntity lectern && lectern.hasBook()
                && List.of(text).equals(PromptLecterns.promptPages(lectern.getBook())), message);
    }

    private static Filterable<Component> page(String text) { return Filterable.passThrough(Component.literal(text)); }

    private static ItemStack written(String title, List<Filterable<Component>> pages) {
        ItemStack book = new ItemStack(Items.WRITTEN_BOOK);
        book.set(DataComponents.WRITTEN_BOOK_CONTENT, new WrittenBookContent(Filterable.passThrough(title), "tester", 0, pages, true));
        return book;
    }

    private static ItemStack writable(List<String> pages) {
        ItemStack book = new ItemStack(Items.WRITABLE_BOOK);
        book.set(DataComponents.WRITABLE_BOOK_CONTENT, new WritableBookContent(pages.stream().map(Filterable::passThrough).toList()));
        return book;
    }

    private static ItemStack named(ItemStack book, String name) {
        book.set(DataComponents.CUSTOM_NAME, Component.literal(name));
        return book;
    }

    private static void lectern(ServerLevel level, BlockPos pos, ItemStack book) {
        level.setBlock(pos, Blocks.LECTERN.defaultBlockState().setValue(LecternBlock.HAS_BOOK, true), 3);
        ((LecternBlockEntity) level.getBlockEntity(pos)).setBook(book);
    }

    /** One job: a plan that overwrites local x 0..1 with stone and leaves x 2..3 untouched. */
    private static FakeCore.Result plan(JsonObject request) {
        return new FakeCore.Result(List.of(4, 1, 1), List.of("minecraft:air", "minecraft:stone"), "Stone over the first lectern",
                List.of(new FakeCore.Op(Map.of("op", "fill", "min", List.of(0, 0, 0), "max", List.of(1, 0, 0),
                        "block", "minecraft:stone"), 0, 2, 1)));
    }
}
