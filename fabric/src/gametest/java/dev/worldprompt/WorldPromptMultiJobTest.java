package dev.worldprompt;

import com.google.gson.JsonObject;
import java.util.List;
import java.util.Map;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.core.BlockPos;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.Blocks;

/**
 * Several concurrent jobs and an undo stack for one player. This test drives its own manager
 * directly (the commands call the same methods), so it never swaps the global command target
 * used by the concurrently running command-flow test.
 */
public final class WorldPromptMultiJobTest {
    /** The flow needs under a hundred ticks; a stall fails by tick count, not by the machine's speed. */
    private static final int TICK_BUDGET = 1500;
    @GameTest(maxTicks = 20000)
    public void onePlayerRunsSeveralJobsWithAnOrderedUndoStack(GameTestHelper context) throws Exception {
        FakeCore core = new FakeCore(WorldPromptMultiJobTest::plan);
        core.model = "fixture-model"; core.reasoningEffort = "medium"; core.fastMode = false;
        AdapterConfig config = new AdapterConfig();
        core.connect(config);
        config.pollMilliseconds = 10;
        PromptManager manager = new PromptManager(context.getLevel().getServer(), config);
        boolean[] cleaned = {false};
        Runnable cleanup = () -> {
            if (!cleaned[0]) { cleaned[0] = true; manager.close(); core.close(); }
        };
        context.runBeforeTestEnd(cleanup);
        ServerPlayer alice = context.makeMockServerPlayerInLevel();
        Chat chat = new Chat(alice);
        // High above the structure, so construction sounds and poofs stay out of other tests' range.
        BlockPos base = context.absolutePos(new BlockPos(1, 100, 1));
        BlockPos r1 = base, r2 = base.offset(3, 0, 0), r3 = base.offset(6, 0, 0);
        for (int x = 0; x < 8; x++) context.getLevel().setBlock(base.offset(x, 0, 0), Blocks.DIRT.defaultBlockState(), 3);

        Job j1 = submit(manager, alice, r1, "hold");
        Job j2 = submit(manager, alice, r2, "hold");
        Job j3 = submit(manager, alice, r3, "hold");
        context.assertTrue(manager.ownJobs(alice.getUUID()).equals(List.of(j1, j2, j3)) && manager.active.size() == 3,
                "One player owns three concurrent jobs, listed oldest first.");
        manager.corner(alice, true, base.offset(1, 0, 0));
        manager.corner(alice, false, base.offset(3, 0, 0));
        expectRefusal(context, () -> manager.submit(alice, "overlapping own jobs"), "intersects your active WorldPrompt job " + id(j1));
        context.assertTrue(manager.active.size() == 3, "A rejected own overlap reserves nothing.");

        int[] stage = {0};
        int[] tick = {0}, windowStart = {0}, overlayStart = {0};
        Job[] jobs = new Job[8];
        AppliedEdit[] edits = new AppliedEdit[4];
        context.onEachTick(() -> {
            if (cleaned[0]) return;
            try {
                tick[0]++;
                manager.tick();
                chat.drain();
                TickBudget.within(context, TICK_BUDGET, () -> "stage " + stage[0]);
                if (stage[0] == 0 && List.of(j1, j2, j3).stream().allMatch(job -> "planning".equals(job.backendStatus))) {
                    windowStart[0] = tick[0]; overlayStart[0] = chat.overlays.size();
                    stage[0] = 1;
                } else if (stage[0] == 1 && tick[0] - windowStart[0] >= 20) {
                    int overlays = chat.overlays.size() - overlayStart[0];
                    context.assertTrue(overlays > 0 && overlays <= 11,
                            "Three jobs share one action bar update every other tick: " + overlays + " in 20 ticks.");
                    String bar = chat.overlays.getLast();
                    context.assertTrue(bar.contains(id(j3)) && bar.contains("+2 more") && !bar.contains(id(j1)),
                            "The action bar shows the newest running job plus the others' count: " + bar);
                    manager.jobs(alice.createCommandSourceStack(), 1);
                    stage[0] = 10;
                } else if (stage[0] == 10 && chat.messages.stream().anyMatch(text -> text.startsWith("WorldPrompt jobs ·"))) {
                    // /wp jobs reads Core asynchronously; cancel only after its listing arrived.
                    manager.cancel(alice, id(j1));
                    context.assertTrue(j1.cancelling && !j2.cancelling && !j3.cancelling, "Cancel with a short ID stops only that job.");
                    manager.cancel(alice, null);
                    context.assertTrue(j3.cancelling && !j2.cancelling, "Cancel without an ID stops the newest running job.");
                    core.states.put(j2.id, "ready");
                    stage[0] = 2;
                } else if (stage[0] == 2 && manager.ownJobs(alice.getUUID()).isEmpty()) {
                    context.assertTrue("failed".equals(core.states.get(j1.id)) && "failed".equals(core.states.get(j3.id))
                                    && "completed".equals(core.states.get(j2.id)),
                            "Core saw two cancellations and one completed application: " + core.states);
                    context.assertTrue(chat.messages.stream().anyMatch(text -> text.contains(id(j1) + " · Stopping your job"))
                                    && chat.messages.stream().anyMatch(text -> text.contains(id(j3) + " · Stopping your job")),
                            "Cancellation chat names each job: " + chat.messages);
                    for (Job job : List.of(j1, j2, j3))
                        context.assertTrue(chat.messages.stream().anyMatch(text -> text.startsWith(id(job) + " · "
                                        + alice.getGameProfile().name() + " · Single pass · fixture-model")),
                                "/wp jobs lists each of the player's jobs by short ID: " + chat.messages);
                    assertBlocks(context, r2, Blocks.STONE, "The released job built its own region.");
                    assertBlocks(context, r1, Blocks.DIRT, "A job cancelled while planning never writes.");
                    edits[0] = manager.history(alice.getUUID()).getFirst();
                    context.assertTrue(manager.history(alice.getUUID()).size() == 1 && edits[0].id.equals(j2.id),
                            "Only the applied job enters the undo stack.");
                    jobs[4] = submit(manager, alice, r2, "gold");
                    context.assertTrue(jobs[4].base.equals(List.of(edits[0].id)), "A later edit records the edit it was captured over.");
                    stage[0] = 3;
                } else if (stage[0] == 3 && manager.ownJobs(alice.getUUID()).isEmpty()) {
                    assertBlocks(context, r2, Blocks.GOLD_BLOCK, "The newer overlapping edit applied.");
                    edits[1] = manager.history(alice.getUUID()).getFirst();
                    context.assertTrue(edits[1].id.equals(jobs[4].id), "The newest edit is on top of the stack.");
                    expectRefusal(context, () -> manager.undo(alice, id(j2)), "Undo " + id(jobs[4]) + " first");
                    expectRefusal(context, () -> manager.rebuild(alice, id(j2)), "Undo " + id(jobs[4]) + " first");
                    assertBlocks(context, r2, Blocks.GOLD_BLOCK, "Refused undo and rebuild leave the newer edit intact.");
                    manager.undo(alice, null);
                    assertBlocks(context, r2, Blocks.STONE, "The first undo restores the newest edit's snapshot.");
                    manager.undo(alice, null);
                    assertBlocks(context, r2, Blocks.DIRT, "A repeated undo walks back to the older edit.");
                    expectRefusal(context, () -> manager.undo(alice, null), "already undone");
                    expectRefusal(context, () -> manager.undo(alice, j2.id), "already undone");
                    expectRefusal(context, () -> manager.rebuild(alice, id(jobs[4])), "no longer matches");
                    expectRefusal(context, () -> manager.rebuild(alice, null), "no longer matches");
                    manager.rebuild(alice, id(j2));
                    jobs[5] = manager.ownJobs(alice.getUUID()).getLast();
                    context.assertTrue(j2.id.equals(jobs[5].rebuildFrom), "Rebuild with an ID replays that edit.");
                    stage[0] = 4;
                } else if (stage[0] == 4 && manager.ownJobs(alice.getUUID()).isEmpty()) {
                    assertBlocks(context, r2, Blocks.STONE, "The older edit was rebuilt locally.");
                    edits[2] = manager.history(alice.getUUID()).getFirst();
                    context.assertTrue(edits[2].id.equals(jobs[5].id) && edits[2].aliases.equals(List.of(j2.id))
                                    && manager.history(alice.getUUID()).size() == 2,
                            "A rebuild replaces its source on the stack and keeps the source ID as an alias.");
                    expectRefusal(context, () -> manager.rebuild(alice, id(jobs[4])), "changed this region after");
                    manager.undo(alice, j2.id);
                    assertBlocks(context, r2, Blocks.DIRT, "Undo accepts the full ID of the edit a rebuild replaced.");
                    core.limit = 1;
                    jobs[6] = submit(manager, alice, r1, "hold");
                    stage[0] = 45;
                } else if (stage[0] == 45 && "planning".equals(jobs[6].backendStatus)) {
                    jobs[7] = submit(manager, alice, r3, "second");
                    stage[0] = 5;
                } else if (stage[0] == 5 && !manager.ownJobs(alice.getUUID()).contains(jobs[7])) {
                    windowStart[0] = tick[0];
                    stage[0] = 55;
                } else if (stage[0] == 55 && tick[0] - windowStart[0] >= 3) {
                    // Chat packets are flushed with the next connection tick.
                    context.assertTrue(chat.messages.stream().anyMatch(text -> text.contains(id(jobs[7]) + " · Not started")
                                    && text.contains("at most 1 active WorldPrompt job per player")),
                            "Core's per-player limit reaches chat with the rejected job's ID: " + chat.messages);
                    context.assertTrue(manager.ownJobs(alice.getUUID()).equals(List.of(jobs[6])), "The accepted job keeps running.");
                    core.limit = 0;
                    jobs[3] = submit(manager, alice, r3, "hold");
                    stage[0] = 6;
                } else if (stage[0] == 6 && "planning".equals(jobs[3].backendStatus) && "planning".equals(jobs[6].backendStatus)) {
                    manager.disconnected(alice.getUUID());
                    context.assertTrue(jobs[3].cancelling && jobs[6].cancelling, "Disconnect stops every job the player owns.");
                    stage[0] = 7;
                } else if (stage[0] == 7 && manager.ownJobs(alice.getUUID()).isEmpty()) {
                    context.assertTrue("failed".equals(core.states.get(jobs[3].id)) && "failed".equals(core.states.get(jobs[6].id)),
                            "Each disconnected job is cancelled in Core.");
                    cleanup.run();
                    context.succeed();
                }
            } catch (Throwable error) {
                cleanup.run();
                context.fail(error.toString());
            }
        });
    }

    private static Job submit(PromptManager manager, ServerPlayer player, BlockPos low, String prompt) {
        manager.corner(player, true, low);
        manager.corner(player, false, low.offset(1, 0, 0));
        manager.submit(player, prompt);
        return manager.ownJobs(player.getUUID()).getLast();
    }
    private static String id(Job job) { return job.id.substring(0, 8); }
    private static void expectRefusal(GameTestHelper context, Runnable action, String expected) {
        try {
            action.run();
            context.fail("Expected refusal containing: " + expected);
        } catch (IllegalArgumentException refused) {
            context.assertTrue(refused.getMessage().contains(expected), "Refusal explains itself: " + refused.getMessage());
        }
    }
    private static void assertBlocks(GameTestHelper context, BlockPos low, Block expected, String message) {
        for (int x = 0; x < 2; x++)
            context.assertTrue(context.getLevel().getBlockState(low.offset(x, 0, 0)).is(expected), message);
    }

    /** Every prompt builds the same two-block fill, of gold for the "gold" prompt and of stone otherwise. */
    private static FakeCore.Result plan(JsonObject request) {
        String block = request.get("prompt").getAsString().equals("gold") ? "minecraft:gold_block" : "minecraft:stone";
        return new FakeCore.Result(List.of(2, 1, 1), List.of("minecraft:air", block), "Fill", List.of(new FakeCore.Op(
                Map.of("op", "fill", "min", List.of(0, 0, 0), "max", List.of(1, 0, 0), "block", block), 0, 2, 1)));
    }
}
