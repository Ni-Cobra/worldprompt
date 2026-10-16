package dev.worldprompt;

import com.google.gson.JsonObject;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.core.BlockPos;
import net.minecraft.core.component.DataComponents;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.server.permissions.PermissionSet;
import net.minecraft.world.item.Items;
import net.minecraft.world.item.component.WrittenBookContent;
import net.minecraft.world.level.block.Blocks;

/** Real command, HTTP, tick, world-write and undo flow; the planner is deterministic. */
public final class WorldPromptFlowTest {
    /** The scripted flow needs a few hundred ticks; a stall fails by tick count, not by the machine's speed. */
    private static final int TICK_BUDGET = 3000;
    /** Ticks a planning job is left alone, enough for many polls, to show repeats do not replay stage sounds. */
    private static final int QUIET_PLANNING_TICKS = 5;
    @GameTest(maxTicks = 100000)
    public void twoPlayersOrderedAnimationPartialFailureAndUndo(GameTestHelper context) throws Exception {
        java.util.concurrent.atomic.AtomicBoolean offlineCancelAllowed = new java.util.concurrent.atomic.AtomicBoolean();
        FakeCore core = new FakeCore(WorldPromptFlowTest::plan);
        core.tokenUsage = true;
        // The "offline" prompt's cancellation gets a 503 until the test lets Core answer it.
        core.cancelOutage = request -> request.get("prompt").getAsString().equals("offline") && !offlineCancelAllowed.get();
        AdapterConfig config = new AdapterConfig();
        core.connect(config);
        config.blocksPerTick = 1;
        config.pollMilliseconds = 10;
        config.selectionCubeSize = 4;
        PromptManager previous = WorldPrompt.manager;
        PromptManager manager = new PromptManager(context.getLevel().getServer(), config);
        WorldPrompt.manager = manager;
        boolean[] cleaned = {false};
        Runnable cleanup = () -> {
            if (!cleaned[0]) {
                cleaned[0] = true;
                WorldPrompt.manager = previous;
                manager.close();
                core.close();
            }
        };
        context.runBeforeTestEnd(cleanup);
        ServerPlayer alice = context.makeMockServerPlayerInLevel();
        ServerPlayer bob = context.makeMockServerPlayerInLevel();
        Chat alicePackets = new Chat(alice);
        Chat bobPackets = new Chat(bob);
        if (net.fabricmc.loader.api.FabricLoader.getInstance().isModLoaded("worldedit"))
            context.assertTrue(context.getLevel().getServer().getCommands().getDispatcher().getRoot()
                    .getChild("/undo") != null, "WorldEdit's separate //undo alias remains registered.");
        var tree = context.getLevel().getServer().getCommands().getDispatcher().getRoot();
        var wp = tree.getChild("wp");
        var dispatcher = context.getLevel().getServer().getCommands().getDispatcher();
        var noPermission = alice.createCommandSourceStack().withPermission(PermissionSet.NO_PERMISSIONS);
        var operator = alice.createCommandSourceStack().withPermission(PermissionSet.ALL_PERMISSIONS);
        for (String name : List.of("prompt", "undo", "rebuild")) {
            context.assertFalse(wp.getChild(name).canUse(noPermission), name + " requires operator permission.");
            context.assertTrue(wp.getChild(name).canUse(operator), name + " is available to operators.");
        }
        var suggestions = dispatcher.getCompletionSuggestions(dispatcher.parse("wp prompt ", operator)).join();
        context.assertTrue(suggestions.getList().stream().anyMatch(item -> item.getText().equals("double"))
                        && suggestions.getList().stream().anyMatch(item -> item.getText().equals("single")),
                "Workflow choices are real autocompleted command literals.");
        for (String name : List.of("undo", "cancel", "rebuild")) {
            var parsed = dispatcher.parse("wp " + name + " 0123abcd", operator);
            context.assertTrue(parsed.getExceptions().isEmpty() && !parsed.getReader().canRead()
                            && parsed.getContext().getArguments().containsKey("id"),
                    "/wp " + name + " accepts an optional job ID.");
        }
        context.assertTrue(wp != null && tree.getChild("worldprompt").getRedirect() == wp,
                "Full-name alias redirects to the shared wp command tree.");
        for (String name : List.of("pos1", "pos2", "select", "prompt", "delay", "undo", "rebuild", "book", "jobs", "cancel", "info"))
            context.assertTrue(wp.getChild(name) != null, "Namespaced command exists: " + name);
        for (String name : List.of("posA", "posB", "prompt", "reconstructiondelay", "rebuild", "jobs"))
            context.assertTrue(tree.getChild(name) == null, "WorldPrompt leaves generic roots free: " + name);
        if (net.fabricmc.loader.api.FabricLoader.getInstance().isModLoaded("worldedit"))
            context.assertTrue(tree.getChild("undo") != null
                    && tree.getChild("undo") != wp.getChild("undo"),
                    "WorldEdit retains its standalone undo command.");
        BlockPos a = context.absolutePos(new BlockPos(1, 2, 1));
        BlockPos b = context.absolutePos(new BlockPos(1, 2, 3));
        ServerPlayer spectator = context.makeMockServerPlayerInLevel();
        ServerPlayer distant = context.makeMockServerPlayerInLevel();
        spectator.setPos(a.getX() + 24, a.getY(), a.getZ());
        distant.setPos(a.getX() + 40, a.getY(), a.getZ());
        Chat spectatorPackets = new Chat(spectator), distantPackets = new Chat(distant);
        for (int i = 0; i < 3; i++) {
            context.getLevel().setBlock(a.offset(i, 0, 0), Blocks.DIRT.defaultBlockState(), 3);
            context.getLevel().setBlock(b.offset(i, 0, 0), Blocks.GOLD_BLOCK.defaultBlockState(), 3);
        }
        BlockPos lookedAt = a.offset(0, 1, 3);
        context.getLevel().setBlock(lookedAt, Blocks.STONE.defaultBlockState(), 3);
        alice.setPos(a.getX() + .5, a.getY(), a.getZ() + .5);
        alice.setYRot(0); alice.setXRot(0);
        command(context, alice, "wp select");
        for (int i = 0; i < 20; i++) manager.tick();
        alicePackets.drain();
        context.assertTrue(alicePackets.messages.stream().anyMatch(text -> text.contains("pos1 = " + (lookedAt.getX() - 2))
                        && text.contains("pos2 = " + (lookedAt.getX() + 1)) && text.contains("4 × 4 × 4")),
                "/wp select targets the looked-at block and prints both generated corners.");
        context.assertTrue(alicePackets.particles > 0 && alicePackets.lightDust && !alicePackets.fullDust,
                "An idle selection emits the lighter particle outline before prompting.");
        BlockPos farBlock = a.offset(0, 1, 40);
        for (int dz = 3; dz < 40; dz++)
            context.getLevel().setBlock(a.offset(0, 1, dz), Blocks.AIR.defaultBlockState(), 3);
        context.getLevel().setBlock(farBlock, Blocks.STONE.defaultBlockState(), 3);
        command(context, alice, "wp select 5 3 7");
        alicePackets.drain();
        context.assertTrue(alicePackets.messages.stream().anyMatch(text ->
                        text.contains("pos1 = " + (farBlock.getX() - 2))
                        && text.contains("pos2 = " + (farBlock.getX() + 2))
                        && text.contains("5 × 3 × 7")
                        && text.contains("around " + farBlock.toShortString())),
                "/wp select dimensions center on a block across loaded chunks: " + alicePackets.messages);
        int idleParticles = alicePackets.particles;
        manager.corner(alice, true, a);
        manager.corner(alice, false, a.offset(2_000_000, 1, 0));
        for (int i = 0; i < 20; i++) manager.tick();
        alicePackets.drain();
        context.assertTrue(alicePackets.particles == idleParticles,
                "Selections above the configured block limit do not emit particles.");
        select(context, alice, a);
        select(context, bob, b);
        command(context, alice, "wp delay 5");
        command(context, alice, "wp info");
        command(context, bob, "worldprompt delay 0");
        alice.setYRot(90); alice.setXRot(-30);
        double capturedX = alice.getX();
        command(context, alice, "wp prompt double alpha");
        command(context, bob, "worldprompt prompt single beta");
        alice.setPos(alice.getX() + 4, alice.getY(), alice.getZ());
        alice.setYRot(180); alice.setXRot(45);
        context.assertTrue(manager.active.size() == 2, "Distinct players own concurrent jobs.");
        Job alpha = activeJob(manager, alice);
        Job beta = activeJob(manager, bob);
        context.assertTrue(alpha.delayTicks == 5 && beta.delayTicks == 0, "Each player owns their delay.");
        context.assertTrue(alpha.prompt.effort().equals("high") && beta.prompt.effort().equals("normal"),
                "Explicit command arguments choose the intended workflow.");
        try {
            manager.submit(alice, "duplicate");
            context.fail("A second job over Alice's own active region was accepted.");
        } catch (IllegalArgumentException expected) {
            context.assertTrue(expected.getMessage().contains("intersects your active WorldPrompt job " + alpha.id.substring(0, 8)),
                    "A player's own concurrent jobs cannot overlap: " + expected.getMessage());
        }
        ServerPlayer overlap = context.makeMockServerPlayerInLevel();
        manager.corner(overlap, true, a.offset(2, 0, 0));
        manager.corner(overlap, false, a.offset(3, 0, 0));
        try {
            manager.submit(overlap, "intersecting job");
            context.fail("An intersecting job was accepted.");
        } catch (IllegalArgumentException expected) {
            context.assertTrue(expected.getMessage().contains("intersects active WorldPrompt job"),
                    "Overlap rejection explains the conflicting job.");
        }
        context.assertTrue(manager.active.size() == 2, "Rejected overlap does not reserve a job.");

        int[] stage = {0};
        boolean[] intermediate = {false};
        long[] firstOperationAt = {0}, secondOperationAt = {0};
        Job[] rebuilt = {null};
        int[] bassBaseline = {0};
        String[] cancelledId = {null}, bobJobId = {null}, blockedId = {null}, offlineId = {null};
        AppliedEdit[] priorUndo = {null};
        long[] quietPlanningAt = {-1};
        int[] quietSoundBaseline = {0};
        Map<String, Integer> instantApplyTicks = new java.util.HashMap<>();
        context.onEachTick(() -> {
            if (cleaned[0]) return;
            try {
                alicePackets.drain(); bobPackets.drain(); spectatorPackets.drain(); distantPackets.drain();
                for (Job job : manager.active.values()) {
                    if (job.delayTicks != 0) continue;
                    context.assertTrue(job.operation == 0 || job.operation == job.operationCount,
                            "A zero delay never leaves a partially replayed plan between ticks: " + job.operation + "/" + job.operationCount);
                    if (job.mode == Job.Mode.APPLY && !job.cancelling)
                        context.assertTrue(instantApplyTicks.merge(job.id, 1, Integer::sum) <= 1,
                                "A zero delay places the whole replay in a single tick despite blocksPerTick = 1.");
                }
                context.assertTrue(alicePackets.overlays.stream().noneMatch(text -> text.contains("tokens") || text.contains("Astra")),
                        "Actionbar excludes token accounting and model names.");
                context.assertTrue(alicePackets.messages.stream().noneMatch(text -> text.contains(" · 0 tokens")),
                        "Zero usage is never displayed.");
                TickBudget.within(context, TICK_BUDGET, () -> "stage " + stage[0]
                        + " active=" + manager.active.values().stream().map(job -> job.mode + ":" + job.status).toList());
                if (alpha.operation == 1) {
                    context.assertTrue(context.getLevel().getBlockState(a).is(Blocks.STONE),
                            "The first operation remains visible before its later air overwrite.");
                    intermediate[0] = true;
                    if (firstOperationAt[0] == 0) firstOperationAt[0] = context.getTick();
                }
                if (alpha.operation == 2 && secondOperationAt[0] == 0) secondOperationAt[0] = context.getTick();
                if (stage[0] == 0 && manager.active.isEmpty()) {
                    context.assertTrue(intermediate[0], "Observed authentic intermediate stone before carve.");
                    context.assertTrue(core.resultRequests.get(alpha.id) == 1 && core.resultRequests.get(beta.id) == 1,
                            "Each player downloads one complete payload.");
                    context.assertTrue(secondOperationAt[0] - firstOperationAt[0] >= 5,
                            "The 5-tick delay is preserved between operations.");
                    context.assertTrue(context.getLevel().getBlockState(a).isAir()
                            && context.getLevel().getBlockState(a.offset(1, 0, 0)).is(Blocks.STONE)
                            && context.getLevel().getBlockState(a.offset(2, 0, 0)).is(Blocks.DIRT),
                            "Alice's final carve and untouched far corner match the plan.");
                    context.assertTrue(context.getLevel().getBlockState(b).isAir()
                            && context.getLevel().getBlockState(b.offset(2, 0, 0)).is(Blocks.GOLD_BLOCK),
                            "Bob's isolated source and coordinates remain distinct.");
                    context.assertTrue(core.requests.get(alpha.id).get("prompt").getAsString().equals("alpha")
                            && core.requests.get(beta.id).get("prompt").getAsString().equals("beta"),
                            "The two captured prompts reach their own job IDs.");
                    JsonObject view = core.requests.get(alpha.id).getAsJsonObject("viewpoint");
                    context.assertTrue(view.getAsJsonArray("position").get(0).getAsDouble() == capturedX
                            && view.get("yaw").getAsDouble() == 90 && view.get("pitch").getAsDouble() == -30,
                            "Position and camera orientation are captured at /wp prompt, before later player movement.");
                    context.assertTrue(alicePackets.basses == 3 && bobPackets.basses == 3 && spectatorPackets.basses == 3,
                            "Alice's delayed operations each play a drum and Bob's instant replay plays one, for all nearby players.");
                    context.assertTrue(alicePackets.bassPackets.stream().anyMatch(sound -> sound.getX() == (double)(float)(b.getX() + 1.5)
                                    && sound.getY() == b.getY() + .5 && sound.getZ() == (double)(float)(b.getZ() + .5)),
                            "An instant replay's single drum plays at the selection center.");
                    context.assertTrue(distantPackets.basses == 0,
                            "Listeners beyond the normal volume-2 range receive no construction bass.");
                    context.assertTrue(spectatorPackets.bassPackets.stream().allMatch(sound -> sound.getVolume() == 2.0f && sound.getPitch() == 1.5f),
                            "In-range listeners receive the bass drum at volume 2 and pitch 1.5.");
                    context.assertTrue(alicePackets.poofs.isEmpty() && bobPackets.poofs.isEmpty() && spectatorPackets.poofs.isEmpty(),
                            "Block placement emits no poof particles.");
                    context.assertTrue(alicePackets.bassPackets.equals(spectatorPackets.bassPackets),
                            "Nearby and farther listeners receive identical sound positions and volumes.");
                    context.assertTrue(alicePackets.particles > idleParticles && alicePackets.whiteForced && alicePackets.fullDust,
                            "Dense white dust packets override both particle limiters and client particle settings.");
                    context.assertTrue(alicePackets.messages.stream().anyMatch(s -> s.contains("300") && s.contains("tokens")),
                            "Cumulative Double pass usage appears in chat.");
                    context.assertTrue(alicePackets.messages.stream().anyMatch(s -> s.contains("Configured provider: codex")
                                    && s.contains("gpt-6-astra") && s.contains("effort high") && s.contains("fast mode on")),
                            "/wp info fetches and displays Core model settings.");
                    context.assertTrue(alicePackets.messages.stream().anyMatch(s -> s.contains("gpt-6-astra")
                                    && s.contains("effort high") && s.contains("tokens")),
                            "Usage names the model and effort used for the job.");
                    var book = alice.getInventory().getNonEquipmentItems().stream()
                            .filter(stack -> stack.is(Items.WRITTEN_BOOK)).findFirst().orElseThrow();
                    WrittenBookContent content = book.get(DataComponents.WRITTEN_BOOK_CONTENT);
                    context.assertTrue(content.author().equals("gpt-6-astra")
                                    && content.title().raw().equals("WorldPrompt " + alpha.id.substring(0, 8))
                                    && content.getPages(false).getFirst().getString().contains("alpha")
                                    && content.getPages(false).get(1).getString().equals("Draft\n\nDraft: Stone then\ncarve")
                                    && content.getPages(false).get(2).getString().equals("Review (built)\n\nStone then carve"),
                            "A completed Double pass prompt gives its player a book signed by the model, holding the prompt "
                                    + "and the draft and review summaries.");
                    context.assertTrue(alicePackets.messages.stream().anyMatch(s -> s.contains("Summary book added")),
                            "Chat announces the summary book.");
                    context.assertTrue(alicePackets.messages.stream().filter(text -> text.contains("tokens")).count() == 1,
                            "Repeated usage and completion do not repeat token chat.");
                    var firstSound = alicePackets.bassPackets.stream().filter(sound -> sound.getX() == (double)(float)(a.getX() + 1.0)
                            && sound.getZ() == (double)(float)(a.getZ() + .5)).findFirst().orElseThrow();
                    // Sound packet getters convert through float; random GameTest
                    // origins near the world border lose sub-block precision.
                    context.assertTrue(firstSound.getPitch() == 1.5f && firstSound.getX() == (double)(float)(a.getX() + 1.0)
                            && firstSound.getY() == a.getY() + .5 && firstSound.getZ() == (double)(float)(a.getZ() + .5),
                            "Bass uses the operation center, not the moved player position, and pitch 1.5.");
                    context.assertTrue(alicePackets.coloredChat && alicePackets.coloredOverlay, "Chat and actionbar have colors.");
                    context.assertTrue(alicePackets.overlays.stream().anyMatch(text -> text.matches("^(Ooo|oOo|ooO|ooo) .*")),
                            "Actionbar uses the Minecraft-friendly spinner.");
                    // An untouched source corner was changed after the job. A
                    // fresh rebuild must restore it before replaying the plan.
                    context.getLevel().setBlock(a.offset(2, 0, 0), Blocks.EMERALD_BLOCK.defaultBlockState(), 3);
                    command(context, alice, "wp delay 2");
                    command(context, alice, "wp rebuild");
                    rebuilt[0] = activeJob(manager, alice);
                    context.assertTrue(rebuilt[0].delayTicks == 2, "Rebuild samples the player's current delay.");
                    try { manager.submit(alice, "duplicate during rebuild"); context.fail("Rebuild released its region reservation."); }
                    catch (IllegalArgumentException expected) { }
                    stage[0] = 10;
                } else if (stage[0] == 10 && manager.active.isEmpty()) {
                    context.assertTrue(context.getLevel().getBlockState(a).isAir()
                            && context.getLevel().getBlockState(a.offset(2, 0, 0)).is(Blocks.DIRT),
                            "Fresh rebuild undoes the latest snapshot, then replays the original operations.");
                    context.assertTrue(core.modelPosts.get() == 2 && core.rebuildPosts.get() == 0,
                            "Rebuild makes no extra model submission: " + core.modelPosts + "/" + core.rebuildPosts);
                    context.assertTrue(!core.requests.containsKey(rebuilt[0].id) && rebuilt[0].operation == 2,
                            "Rebuild runs both retained operations without contacting Core.");
                    command(context, alice, "wp undo");
                    command(context, bob, "wp undo");
                    assertRegion(context, a, Blocks.DIRT);
                    assertRegion(context, b, Blocks.GOLD_BLOCK);
                    context.assertTrue(manager.active.isEmpty(), "Undo restores synchronously without an animation job.");
                    stage[0] = 1;
                } else if (stage[0] == 1 && manager.active.isEmpty()) {
                    assertRegion(context, a, Blocks.DIRT);
                    assertRegion(context, b, Blocks.GOLD_BLOCK);
                    context.getLevel().setBlock(a.offset(2, 0, 0), Blocks.EMERALD_BLOCK.defaultBlockState(), 3);
                    command(context, alice, "wp delay 0");
                    command(context, alice, "wp rebuild");
                    rebuilt[0] = activeJob(manager, alice);
                    context.assertTrue(rebuilt[0].delayTicks == 0, "Another rebuild samples zero delay.");
                    stage[0] = 11;
                } else if (stage[0] == 11 && manager.active.isEmpty()) {
                    context.assertTrue(context.getLevel().getBlockState(a).isAir()
                            && context.getLevel().getBlockState(a.offset(2, 0, 0)).is(Blocks.EMERALD_BLOCK),
                            "Rebuild after completed undo skips redundant snapshot restoration.");
                    context.assertTrue(core.modelPosts.get() == 2 && core.rebuildPosts.get() == 0,
                            "Repeated rebuilds reuse the final plan without inference.");
                    command(context, alice, "wp undo");
                    stage[0] = 12;
                } else if (stage[0] == 12 && manager.active.isEmpty()) {
                    assertRegion(context, a, Blocks.DIRT);
                    command(context, alice, "wp prompt partial");
                    stage[0] = 2;
                } else if (stage[0] == 2 && manager.active.isEmpty()) {
                    context.assertTrue(context.getLevel().getBlockState(a).is(Blocks.DIRT),
                            "A corrupt later operation rejects the complete payload before any world write.");
                    context.assertTrue(!manager.history(alice.getUUID()).isEmpty(),
                            "Rejected result preserves the prior undo snapshot.");
                    command(context, alice, "wp rebuild");
                    stage[0] = 13;
                } else if (stage[0] == 13 && manager.active.isEmpty()) {
                    context.assertTrue(context.getLevel().getBlockState(a).isAir(),
                            "The prior valid plan remains locally rebuildable after rejecting a corrupt result.");
                    context.assertTrue(core.modelPosts.get() == 3 && core.rebuildPosts.get() == 0,
                            "Partial failure recovery does not call the model again.");
                    command(context, alice, "wp undo");
                    stage[0] = 3;
                } else if (stage[0] == 3 && manager.active.isEmpty()) {
                    assertRegion(context, a, Blocks.DIRT);
                    AppliedEdit undo = manager.history(alice.getUUID()).getFirst();
                    command(context, alice, "wp prompt blocked");
                    Job blocked = activeJob(manager, alice);
                    blockedId[0] = blocked.id;
                    priorUndo[0] = undo;
                    stage[0] = 4;
                } else if (stage[0] == 4 && core.requests.containsKey(blockedId[0])) {
                    if (!activeJob(manager, alice).backendStatus.equals("planning")) return;
                    context.assertTrue(activeJob(manager, alice).status.equals("gpt-6-astra · planning"),
                            "Planning status names the configured model.");
                    if (quietPlanningAt[0] < 0) {
                        quietPlanningAt[0] = context.getTick();
                        quietSoundBaseline[0] = alicePackets.otherSounds;
                        return;
                    }
                    if (context.getTick() - quietPlanningAt[0] < QUIET_PLANNING_TICKS) return;
                    context.assertTrue(alicePackets.otherSounds == quietSoundBaseline[0],
                            "Repeated planning polls animate status without replaying stage sounds.");
                    manager.disconnected(alice.getUUID());
                    stage[0] = 5;
                } else if (stage[0] == 5 && manager.active.isEmpty()) {
                    context.assertTrue(core.states.get(blockedId[0]).equals("failed"),
                            "Disconnect cancels the submitted backend job.");
                    context.assertTrue(manager.history(alice.getUUID()).getFirst() == priorUndo[0] && priorUndo[0].restored,
                            "Failure before application preserves the prior undo record.");
                    // Keep a gap after the first operation so the partial application is observable.
                    command(context, alice, "wp delay 20");
                    command(context, alice, "wp prompt offline");
                    offlineId[0] = activeJob(manager, alice).id;
                    stage[0] = 6;
                } else if (stage[0] == 6 && activeJob(manager, alice).operation == 1) {
                    manager.disconnected(alice.getUUID());
                    context.assertTrue(activeJob(manager, alice).applied,
                            "The offline fixture failed after a partial application.");
                    command(context, alice, "wp undo");
                    stage[0] = 7;
                } else if (stage[0] == 7 && manager.active.isEmpty()) {
                    assertRegion(context, a, Blocks.DIRT);
                    try {
                        manager.submit(alice, "must remain occupied");
                        context.fail("An unacknowledged stopping job released its region reservation.");
                    } catch (IllegalArgumentException expected) { }
                    offlineCancelAllowed.set(true);
                    stage[0] = 8;
                } else if (stage[0] == 8 && "failed".equals(core.states.get(offlineId[0]))) {
                    try { manager.submit(alice, "empty-operation"); }
                    catch (IllegalArgumentException pendingAcknowledgement) { return; }
                    context.assertTrue((activeJob(manager, alice) != null),
                            "Acknowledged cancellation releases the region after offline undo.");
                    bassBaseline[0] = alicePackets.basses;
                    stage[0] = 9;
                } else if (stage[0] == 9 && manager.active.isEmpty()) {
                    context.assertTrue(alicePackets.basses - bassBaseline[0] == 3,
                            "An empty JSON operation still produces exactly one reconstruction bass.");
                    command(context, alice, "wp delay 20");
                    command(context, alice, "wp rebuild");
                    cancelledId[0] = activeJob(manager, alice).id;
                    stage[0] = 20;
                } else if (stage[0] == 20 && activeJob(manager, alice).operation == 1) {
                    command(context, alice, "wp cancel");
                    assertRegion(context, a, Blocks.DIRT);
                    context.assertTrue(manager.history(alice.getUUID()).getFirst().restored,
                            "Cancelling a partially applied rebuild restores immediately.");
                    stage[0] = 21;
                } else if (stage[0] == 21 && manager.active.isEmpty()) {
                    context.assertTrue(!core.states.containsKey(cancelledId[0]),
                            "Cancelled rebuild stops locally without creating a backend job.");
                    command(context, alice, "wp prompt cancellable");
                    cancelledId[0] = activeJob(manager, alice).id;
                    stage[0] = 22;
                } else if (stage[0] == 22 && activeJob(manager, alice).operation == 1) {
                    command(context, alice, "wp cancel " + cancelledId[0].substring(0, 8));
                    context.assertTrue(context.getLevel().getBlockState(a).is(Blocks.STONE),
                            "Single pass reconstruction cancellation stops writes while retaining undo.");
                    stage[0] = 23;
                } else if (stage[0] == 23 && manager.active.isEmpty()) {
                    var undoSource = alice.createCommandSourceStack().withPermission(PermissionSet.ALL_PERMISSIONS);
                    var ids = dispatcher.getCompletionSuggestions(dispatcher.parse("wp undo ", undoSource)).join();
                    context.assertTrue(ids.getList().stream().anyMatch(item -> item.getText().equals(cancelledId[0].substring(0, 8))),
                            "/wp undo suggests the player's retained edit IDs.");
                    command(context, alice, "wp undo " + cancelledId[0]);
                    assertRegion(context, a, Blocks.DIRT);
                    command(context, alice, "wp prompt blocked");
                    command(context, bob, "wp prompt blocked");
                    cancelledId[0] = activeJob(manager, alice).id;
                    bobJobId[0] = activeJob(manager, bob).id;
                    stage[0] = 24;
                } else if (stage[0] == 24 && core.requests.containsKey(cancelledId[0]) && core.requests.containsKey(bobJobId[0])) {
                    command(context, alice, "wp cancel");
                    stage[0] = 25;
                } else if (stage[0] == 25 && !(activeJob(manager, alice) != null)) {
                    context.assertTrue(core.states.get(cancelledId[0]).equals("failed") && core.states.get(bobJobId[0]).equals("planning"),
                            "Cancelling planning stops only the requesting player job.");
                    command(context, alice, "wp prompt cancel during capture");
                    String capturing = activeJob(manager, alice).id;
                    command(context, alice, "wp cancel");
                    context.assertTrue(!(activeJob(manager, alice) != null) && !core.requests.containsKey(capturing),
                            "Cancellation before submission never starts an agent.");
                    command(context, bob, "worldprompt cancel");
                    stage[0] = 26;
                } else if (stage[0] == 26 && manager.active.isEmpty()) {
                    bassBaseline[0] = alicePackets.basses;
                    command(context, alice, "wp delay 0");
                    command(context, alice, "wp prompt invalid-palette");
                    cancelledId[0] = activeJob(manager, alice).id;
                    stage[0] = 27;
                } else if (stage[0] == 27 && manager.active.isEmpty()) {
                    context.assertTrue(core.states.get(cancelledId[0]).equals("completed"), "Unsupported states do not fail the job.");
                    context.assertTrue(alicePackets.basses - bassBaseline[0] == 1,
                            "An instant five-operation build plays one drum, not one per operation.");
                    context.assertTrue(context.getLevel().getBlockState(a).is(Blocks.STONE)
                            && context.getLevel().getBlockState(a.offset(1,0,0)).is(Blocks.IRON_CHAIN)
                            && context.getLevel().getBlockState(a.offset(2,0,0)).isAir(),
                            "Skipped writes preserve earlier blocks, chain aliases resolve, and later valid operations run.");
                    context.assertTrue(alicePackets.messages.stream().anyMatch(text -> text.contains("Unsupported block state"))
                            && alicePackets.messages.stream().anyMatch(text -> text.contains("2 placement(s) skipped")),
                            "Warnings and completion counts appear in chat.");
                    command(context, alice, "wp rebuild " + cancelledId[0].substring(0, 8));
                    stage[0] = 28;
                } else if (stage[0] == 28 && manager.active.isEmpty()) {
                    context.assertTrue(context.getLevel().getBlockState(a).is(Blocks.STONE)
                            && context.getLevel().getBlockState(a.offset(1,0,0)).is(Blocks.IRON_CHAIN),
                            "A saved palette with skipped entries remains rebuildable.");
                    command(context, alice, "wp undo");
                    assertRegion(context, a, Blocks.DIRT);
                    for (int y = 0; y < 2; y++) for (int z = 0; z < 4; z++) for (int x = 0; x < 4; x++)
                        context.getLevel().setBlock(a.offset(x, y, z), Blocks.AIR.defaultBlockState(), 3);
                    manager.corner(alice, true, a);
                    manager.corner(alice, false, a.offset(3, 1, 3));
                    manager.submit(alice, "random-palette");
                    stage[0] = 29;
                } else if (stage[0] == 29 && manager.active.isEmpty()) {
                    int replaced = 0, dirt = 0;
                    for (int y = 0; y < 2; y++) for (int z = 0; z < 4; z++) for (int x = 0; x < 4; x++) {
                        var state = context.getLevel().getBlockState(a.offset(x, y, z));
                        if (x == 1 && y == 0 && z == 1)
                            context.assertTrue(state.is(Blocks.GOLD_BLOCK) || state.is(Blocks.DIAMOND_BLOCK),
                                    "Single places exactly one randomly selected target state.");
                        else if (state.is(Blocks.COBBLESTONE) || state.is(Blocks.MOSSY_COBBLESTONE)) replaced++;
                        else if (state.is(Blocks.DIRT)) dirt++;
                        else context.fail("Unexpected block from random fill: " + state);
                    }
                    context.assertTrue(replaced > 0 && dirt > 0,
                            "Fabric chooses per block for fills and replacements, matching actual chosen states.");
                    command(context, alice, "wp undo");
                    for (int y = 0; y < 2; y++) for (int z = 0; z < 4; z++) for (int x = 0; x < 4; x++)
                        context.assertTrue(context.getLevel().getBlockState(a.offset(x, y, z)).isAir(),
                                "Undo restores the source after random placement.");
                    core.close();
                    config.token = "";
                    config.tokenFile = "/missing-core-token-for-offline-rebuild";
                    command(context, alice, "wp rebuild");
                    rebuilt[0] = activeJob(manager, alice);
                    context.assertTrue(rebuilt[0].reportedTokens == 0L, "Local rebuild consumes zero new tokens.");
                    // Region ownership is still enforced when Core is unavailable.
                    manager.corner(bob, true, a); manager.corner(bob, false, a.offset(1, 0, 0));
                    try { manager.submit(bob, "overlapping offline rebuild"); context.fail("Offline rebuild lost region ownership."); }
                    catch (IllegalArgumentException expected) { }
                    stage[0] = 30;
                } else if (stage[0] == 30 && manager.active.isEmpty()) {
                    context.assertTrue(rebuilt[0].operation == 3, "Offline rebuild completes every cached operation.");
                    context.assertTrue(context.getLevel().getBlockState(a.offset(1,0,1)).is(Blocks.GOLD_BLOCK)
                            || context.getLevel().getBlockState(a.offset(1,0,1)).is(Blocks.DIAMOND_BLOCK),
                            "Offline replay retains random single-block choices.");
                    command(context, alice, "wp undo");
                    command(context, alice, "wp delay 20");
                    command(context, alice, "wp rebuild");
                    stage[0] = 31;
                } else if (stage[0] == 31 && activeJob(manager, alice).operation == 1) {
                    command(context, alice, "wp cancel");
                    context.assertTrue(manager.active.isEmpty(), "Offline cancellation releases the player immediately.");
                    for (int y = 0; y < 2; y++) for (int z = 0; z < 4; z++) for (int x = 0; x < 4; x++)
                        context.assertTrue(context.getLevel().getBlockState(a.offset(x,y,z)).isAir(),
                                "Cancelling offline rebuild restores the entire original snapshot.");
                    command(context, alice, "wp delay 0");
                    command(context, alice, "wp rebuild");
                    stage[0] = 32;
                } else if (stage[0] == 32 && manager.active.isEmpty()) {
                    context.assertTrue(core.rebuildPosts.get() == 0, "No Core rebuild route was used.");
                    context.assertTrue(instantApplyTicks.size() >= 5, "Several zero-delay builds and rebuilds were observed.");
                    cleanup.run(); context.succeed();
                }
            } catch (Throwable error) {
                cleanup.run();
                context.fail(error.toString());
            }
        });
    }

    /** The player's newest job that is still in the running set. */
    static Job activeJob(PromptManager manager, ServerPlayer player) {
        Job found = null;
        for (Job job : manager.active.values()) if (job.owner.equals(player.getUUID())) found = job;
        return found;
    }
    private static void select(GameTestHelper context, ServerPlayer player, BlockPos low) {
        player.setPos(low.getX() + .2, low.getY(), low.getZ() + .2);
        command(context, player, "wp pos1");
        command(context, player, "wp pos2 " + (low.getX() + 2) + " " + low.getY() + " " + low.getZ());
    }
    private static void command(GameTestHelper context, ServerPlayer player, String command) {
        try {
            int result = context.getLevel().getServer().getCommands().getDispatcher()
                    .execute(command, player.createCommandSourceStack().withPermission(PermissionSet.ALL_PERMISSIONS));
            context.assertTrue(result == 1, "Command succeeds: " + command);
        } catch (Exception e) { throw new IllegalStateException("Command failed: " + command + " (" + e + ")", e); }
    }
    private static void assertRegion(GameTestHelper context, BlockPos low,
                                     net.minecraft.world.level.block.Block expected) {
        for (int i = 0; i < 3; i++)
            context.assertTrue(context.getLevel().getBlockState(low.offset(i, 0, 0)).is(expected),
                    "Undo restores original block " + i);
    }

    /** What Core returns for each scripted prompt; any other prompt gets two operations that fill stone, then carve air. */
    private static FakeCore.Result plan(JsonObject request) {
        String prompt = request.get("prompt").getAsString();
        if (prompt.equals("random-palette")) {
            return new FakeCore.Result(List.of(4, 2, 4), List.of("minecraft:air", "minecraft:stone", "minecraft:gold_block"),
                    "Random palette and single", List.of(
                    new FakeCore.Op(Map.of("op", "fill", "min", List.of(0, 0, 0), "max", List.of(3, 1, 3),
                            "block", List.of("minecraft:stone", "minecraft:dirt")), 0, 32, 1),
                    new FakeCore.Op(Map.of("op", "single", "at", List.of(1, 0, 1),
                            "block", List.of("minecraft:gold_block", "minecraft:diamond_block")), 5, 1, 2),
                    new FakeCore.Op(Map.of("op", "replace", "min", List.of(0, 0, 0), "max", List.of(3, 1, 3),
                            "block", List.of("minecraft:cobblestone", "minecraft:mossy_cobblestone"),
                            "match", List.of("minecraft:stone"), "preserve_properties", false), new int[]{0, 32, 0}, true)));
        }
        if (prompt.equals("invalid-palette")) {
            var palette = List.of("minecraft:air", "minecraft:stone", "example:missing_block",
                    "minecraft:chain[axis=y]", "minecraft:stone[axis=y]");
            int[] positions = {0, 0, 1, 1, 2}, states = {1, 2, 3, 4, 0};
            List<FakeCore.Op> ops = new ArrayList<>();
            for (int i = 0; i < 5; i++) ops.add(new FakeCore.Op(Map.of("op", "fill", "min", List.of(positions[i], 0, 0),
                    "max", List.of(positions[i], 0, 0), "block", palette.get(states[i])), positions[i], 1, states[i]));
            return new FakeCore.Result(List.of(3, 1, 1), palette, "Mixed supported and unsupported states", ops);
        }
        // "partial" places its carve past the 3-block selection, so Core's payload is rejected as corrupt.
        int carveStart = prompt.equals("partial") ? 3 : 0;
        List<FakeCore.Op> ops = new ArrayList<>(List.of(
                new FakeCore.Op(Map.of("op", "fill", "min", List.of(0, 0, 0), "max", List.of(1, 0, 0), "block", "minecraft:stone"), 0, 2, 1),
                new FakeCore.Op(Map.of("op", "fill", "min", List.of(0, 0, 0), "max", List.of(0, 0, 0), "block", "minecraft:air"), carveStart, 1, 0)));
        if (prompt.equals("empty-operation")) ops.add(new FakeCore.Op(Map.of("op", "replace", "min", List.of(0, 0, 0), "max", List.of(2, 0, 0),
                "block", "minecraft:stone", "match", List.of("minecraft:diamond_block"), "preserve_properties", true)));
        return new FakeCore.Result(List.of(3, 1, 1), List.of("minecraft:air", "minecraft:stone"), "Stone then carve", ops);
    }
}
