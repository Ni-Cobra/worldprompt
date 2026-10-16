package dev.worldprompt;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.core.BlockPos;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;

/** Lifecycle and live-state regressions that do not contact a Core process. */
public final class WorldPromptRegressionTest {
    @GameTest
    public void failedStartHeartbeatPreservesPreviousHistory(GameTestHelper context) {
        try (PromptManager manager = manager(context)) {
            ServerPlayer player = context.makeMockServerPlayerInLevel();
            Job prior = job(context, player, payload("minecraft:stone", null));
            var saved = new AppliedEdit(prior.id, player.getUUID(), "player", prior.snapshot, prior.prompt,
                    prior.replay, prior.palette, "saved model", List.of(), List.of());
            manager.history(player.getUUID()).addFirst(saved);
            Job current = job(context, player, payload("minecraft:gold_block", null));
            current.mode = Job.Mode.START;
            current.heartbeat = CompletableFuture.completedFuture(remote(current, "failed"));
            current.pending = CompletableFuture.completedFuture(remote(current, "reconstructing"));
            manager.active.put(current.id, current);
            manager.tick();
            context.assertTrue(current.cancelling && !current.applied,
                    "A failed heartbeat stops START before recording a new edit.");
            context.assertTrue(manager.history(player.getUUID()).size() == 1
                            && manager.history(player.getUUID()).getFirst() == saved,
                    "Failure before application preserves the prior plan and undo snapshot.");
        }
        context.succeed();
    }

    @GameTest
    public void remoteCancellationStopsBeforeTheNextWrite(GameTestHelper context) {
        try (PromptManager manager = manager(context)) {
            ServerPlayer player = context.makeMockServerPlayerInLevel();
            Job current = job(context, player, payload("minecraft:gold_block", null));
            current.mode = Job.Mode.APPLY;
            current.heartbeat = CompletableFuture.completedFuture(remote(current, "cancelling"));
            manager.active.put(current.id, current);
            manager.tick();
            context.assertTrue(current.cancelling && !current.applied,
                    "Core cancellation stops reconstruction immediately.");
            context.assertTrue(context.getLevel().getBlockState(current.snapshot.position(0)).is(Blocks.DIRT),
                    "A cancelling heartbeat does not allow another block write.");
        }
        context.succeed();
    }

    @GameTest
    public void randomChoicesHonorAliasesAndDefaultedMatches(GameTestHelper context) {
        for (String state : List.of("minecraft:chain[axis=y]", "minecraft:oak_stairs[facing=east]")) {
            try (PromptManager manager = manager(context)) {
                ServerPlayer player = context.makeMockServerPlayerInLevel();
                // A positive delay keeps one operation per tick so the intermediate state is visible.
                Job current = job(context, player, payload(state, state), 1);
                current.mode = Job.Mode.APPLY;
                current.rebuildFrom = "local-saved-plan";
                manager.active.put(current.id, current);
                manager.tick();
                context.assertTrue(context.getLevel().getBlockState(current.snapshot.position(0))
                                .is(state.contains("chain") ? Blocks.IRON_CHAIN : Blocks.OAK_STAIRS),
                        "A random target resolves its block state, including legacy aliases.");
                manager.tick();
                context.assertTrue(context.getLevel().getBlockState(current.snapshot.position(0)).is(Blocks.GOLD_BLOCK),
                        "Dynamic replacement matches omitted properties using Minecraft defaults.");
            }
        }
        context.succeed();
    }

    @GameTest
    public void zeroDelayReplaysEveryOperationInOneTick(GameTestHelper context) {
        AdapterConfig config = new AdapterConfig();
        config.token = "unused-local-regression-token";
        config.blocksPerTick = 1;
        try (PromptManager manager = new PromptManager(context.getLevel().getServer(), config)) {
            ServerPlayer player = context.makeMockServerPlayerInLevel();
            BlockPos low = context.absolutePos(new BlockPos(1, 100, 3));
            for (int x = 0; x < 4; x++) context.getLevel().setBlock(low.offset(x, 0, 0), Blocks.DIRT.defaultBlockState(), 3);
            RegionSnapshot snapshot = new RegionSnapshot(context.getLevel(), RegionBounds.between(low.getX(), low.getY(), low.getZ(),
                    low.getX() + 3, low.getY(), low.getZ(), 4));
            while (!snapshot.capture(context.getLevel(), 4)) { }
            var definitions = List.<Map<String, Object>>of(
                    Map.of("op", "fill", "min", List.of(0, 0, 0), "max", List.of(3, 0, 0), "block", "minecraft:stone"),
                    Map.of("op", "single", "at", List.of(1, 0, 0), "block", "minecraft:gold_block"),
                    Map.of("op", "fill", "min", List.of(3, 0, 0), "max", List.of(3, 0, 0), "block", "minecraft:gold_block"));
            int[][] runs = {{0, 4, 0}, {1, 1, 1}, {3, 1, 1}};
            List<Map<String, Object>> packed = java.util.stream.IntStream.range(0, 3).mapToObj(i -> Map.<String, Object>of(
                    "index", i, "op", definitions.get(i).get("op"), "run_count", 1, "writes", runs[i][1], "dynamic_match", false,
                    "runs_base64", Base64.getEncoder().encodeToString(ByteBuffer.allocate(12).order(ByteOrder.LITTLE_ENDIAN)
                            .putInt(runs[i][0]).putInt(runs[i][1]).putInt(runs[i][2]).array()))).toList();
            ReplayPayload replay = ReplayPayload.parse(new Gson().toJsonTree(Map.of("result_version", 2, "run_encoding", "base64-u32le",
                    "dimensions", List.of(4, 1, 1), "palette", List.of("minecraft:stone", "minecraft:gold_block"),
                    "operation_count", 3, "plan", Map.of("version", 1, "operations", definitions),
                    "operations", packed)).getAsJsonObject());
            Job current = new Job(player, snapshot, PromptRequest.parse("instant", "single"), 0);
            current.replay = replay; current.finalPlan = replay.plan; current.operationCount = 3;
            current.palette.add(Blocks.STONE.defaultBlockState()); current.palette.add(Blocks.GOLD_BLOCK.defaultBlockState());
            current.mode = Job.Mode.APPLY;
            current.rebuildFrom = "local-saved-plan";
            manager.active.put(current.id, current);
            manager.tick();
            context.assertTrue(current.operation == 3 && current.mode == Job.Mode.FINISH,
                    "A zero delay completes every operation in one tick despite blocksPerTick = 1.");
            var expected = List.of(Blocks.STONE, Blocks.GOLD_BLOCK, Blocks.STONE, Blocks.GOLD_BLOCK);
            for (int x = 0; x < 4; x++)
                context.assertTrue(context.getLevel().getBlockState(low.offset(x, 0, 0)).is(expected.get(x)),
                        "Instant replay keeps authoring order and overwrites at block " + x);
            context.assertTrue(current.applied && manager.history(player.getUUID()).peekFirst() == current.edit && current.edit.snapshot == snapshot,
                    "Instant replay records the undo snapshot before writing.");
            manager.tick();
            context.assertTrue(manager.active.isEmpty(), "The instant rebuild finishes on the next tick.");
        }
        context.succeed();
    }

    private static CompletableFuture<JsonObject> failure(Throwable error) {
        return CompletableFuture.failedFuture(new java.util.concurrent.CompletionException(error));
    }

    @GameTest
    public void aSingleFailedHeartbeatDoesNotFailTheBuildButTheGracePeriodAndA404Do(GameTestHelper context) {
        try (PromptManager manager = manager(context)) {
            ServerPlayer player = context.makeMockServerPlayerInLevel();
            Job tolerant = job(context, player, payload("minecraft:stone", null), 1);
            tolerant.mode = Job.Mode.APPLY;
            tolerant.heartbeat = failure(new CoreClient.ApiException(503, "Core is restarting"));
            manager.active.put(tolerant.id, tolerant);
            manager.tick();
            context.assertTrue(!tolerant.cancelling, "One failed heartbeat inside the 15 s grace period keeps building.");

            tolerant.lastContactAt = System.currentTimeMillis() - CoreJobDriver.CONNECTION_GRACE_MS - 1000;
            tolerant.heartbeat = failure(new java.net.ConnectException("refused"));
            manager.tick();
            context.assertTrue(tolerant.cancelling && tolerant.failure.contains("Core connection failed"),
                    "Silence beyond the grace period stops the build: " + tolerant.failure);
        }
        try (PromptManager manager = manager(context)) {
            ServerPlayer player = context.makeMockServerPlayerInLevel();
            Job lost = job(context, player, payload("minecraft:stone", null), 1);
            lost.mode = Job.Mode.APPLY;
            lost.heartbeat = failure(new CoreClient.ApiException(404, "No job"));
            manager.active.put(lost.id, lost);
            manager.tick();
            context.assertTrue(lost.cancelling && lost.failure.contains("lost this job"), "A 404 fails immediately: " + lost.failure);
        }
        context.succeed();
    }

    @GameTest
    public void submitStopsAfterTotalRetriesAndTreatsAMismatchedJobAsFatal(GameTestHelper context) {
        try (PromptManager manager = manager(context)) {
            ServerPlayer player = context.makeMockServerPlayerInLevel();
            Job flaky = job(context, player, payload("minecraft:stone", null));
            flaky.mode = Job.Mode.SUBMIT; flaky.submitted = true;
            flaky.pending = failure(new java.io.IOException("reset"));
            manager.active.put(flaky.id, flaky);
            manager.tick();
            context.assertTrue(!flaky.cancelling && flaky.submitRetries == 1 && flaky.retryAt > 0, "A first transient failure is retried.");
            flaky.submitRetries = CoreJobDriver.MAX_SUBMIT_RETRIES - 1;
            flaky.pending = failure(new java.io.IOException("reset"));
            manager.tick();
            context.assertTrue(flaky.cancelling && flaky.failure.contains("did not accept"),
                    "Total submit retries are bounded: " + flaky.failure);

            Job wrongId = job(context, player, payload("minecraft:stone", null));
            wrongId.mode = Job.Mode.SUBMIT; wrongId.submitted = true;
            wrongId.pending = CompletableFuture.completedFuture(new Gson().toJsonTree(Map.of("id", "someone-elses-job",
                    "owner_id", player.getUUID().toString(), "status", "queued")).getAsJsonObject());
            manager.active.put(wrongId.id, wrongId);
            manager.tick();
            context.assertTrue(wrongId.cancelling && wrongId.failure.contains("another job") && wrongId.submitRetries == 0,
                    "A mismatched job ID is fatal, not retried: " + wrongId.failure);

            Job wrongOwner = job(context, player, payload("minecraft:stone", null));
            wrongOwner.mode = Job.Mode.SUBMIT; wrongOwner.submitted = true;
            wrongOwner.pending = CompletableFuture.completedFuture(new Gson().toJsonTree(Map.of("id", wrongOwner.id,
                    "owner_id", java.util.UUID.randomUUID().toString(), "status", "queued")).getAsJsonObject());
            manager.active.put(wrongOwner.id, wrongOwner);
            manager.tick();
            context.assertTrue(wrongOwner.cancelling && wrongOwner.failure.contains("another owner"),
                    "A mismatched owner is fatal: " + wrongOwner.failure);
        }
        context.succeed();
    }

    @GameTest
    public void completionAcknowledgementRejectedByCoreFinishesLocallyAndKeepsUndo(GameTestHelper context) {
        try (PromptManager manager = manager(context)) {
            ServerPlayer player = context.makeMockServerPlayerInLevel();
            Job done = job(context, player, payload("minecraft:stone", null));
            var saved = new AppliedEdit(done.id, player.getUUID(), "player", done.snapshot, done.prompt,
                    done.replay, done.palette, "saved model", List.of(), List.of());
            manager.history(player.getUUID()).addFirst(saved);
            done.edit = saved; done.applied = true; done.submitted = true;
            done.mode = Job.Mode.FINISH;
            done.pending = failure(new CoreClient.ApiException(503, "Core is busy"));
            manager.active.put(done.id, done);
            manager.tick();
            context.assertTrue(manager.active.containsKey(done.id) && done.mode == Job.Mode.FINISH && done.pending == null,
                    "A transient error keeps retrying the acknowledgement.");
            done.retryAt = 0;
            done.pending = failure(new CoreClient.ApiException(404, "No job"));
            manager.tick();
            context.assertTrue(manager.active.isEmpty() && done.mode == Job.Mode.DONE,
                    "A 404 on the acknowledgement finishes the job locally.");
            context.assertTrue(manager.history(player.getUUID()).peekFirst() == saved, "The undo entry is kept.");
        }
        context.succeed();
    }

    @GameTest
    public void stoppingJobIsReleasedLocallyWhenCoreNeverConfirmsTheCancel(GameTestHelper context) {
        try (PromptManager manager = manager(context)) {
            ServerPlayer player = context.makeMockServerPlayerInLevel();
            Job stuck = job(context, player, payload("minecraft:stone", null));
            stuck.mode = Job.Mode.WAIT; stuck.submitted = true; stuck.cancelling = true; stuck.failure = "Cancelled";
            stuck.cancelStartedAt = System.currentTimeMillis();
            stuck.retryAt = Long.MAX_VALUE;
            stuck.cancelRequest = failure(new CoreClient.ApiException(503, "Core is down"));
            manager.active.put(stuck.id, stuck);
            manager.tick();
            context.assertTrue(manager.active.containsKey(stuck.id), "A recent cancel keeps retrying while Core is unreachable.");
            stuck.cancelStartedAt = System.currentTimeMillis() - CoreJobDriver.CANCEL_GIVE_UP_MS - 1000;
            manager.tick();
            context.assertTrue(manager.active.isEmpty() && stuck.mode == Job.Mode.DONE,
                    "After the bounded wait the job finishes locally, which releases its region.");
        }
        context.succeed();
    }

    @GameTest
    public void undoRefusesBeforeTheFirstWriteWhenAnyChunkIsUnloaded(GameTestHelper context) {
        try (PromptManager manager = manager(context)) {
            ServerPlayer player = context.makeMockServerPlayerInLevel();
            var level = context.getLevel();
            BlockPos start = context.absolutePos(new BlockPos(1, 100, 1));
            int chunk = start.getX() >> 4;
            while (level.hasChunkAt(new BlockPos(chunk << 4, start.getY(), start.getZ())) && chunk - (start.getX() >> 4) < 400) chunk++;
            context.assertTrue(!level.hasChunkAt(new BlockPos(chunk << 4, start.getY(), start.getZ())), "Found an unloaded chunk.");
            // Two blocks wide: the first lies in a loaded chunk, the second in the unloaded neighbor.
            BlockPos loaded = new BlockPos((chunk << 4) - 1, start.getY(), start.getZ());
            context.assertTrue(level.hasChunkAt(loaded) && !level.hasChunkAt(loaded.east()), "One half is loaded, the other is not.");
            // The snapshot would turn the loaded half into stone if the restore started before checking.
            BlockState before = level.getBlockState(loaded);
            RegionSnapshot snapshot = new RegionSnapshot(level, RegionBounds.between(loaded.getX(), loaded.getY(), loaded.getZ(),
                    loaded.getX() + 1, loaded.getY(), loaded.getZ(), 2));
            snapshot.palette.add(Blocks.STONE.defaultBlockState());
            snapshot.captured = snapshot.blocks.length;
            Job prior = job(context, player, payload("minecraft:stone", null));
            var edit = new AppliedEdit(prior.id, player.getUUID(), "player", snapshot, prior.prompt,
                    prior.replay, prior.palette, "saved model", List.of(), List.of());
            manager.history(player.getUUID()).addFirst(edit);
            try { manager.undo(player); context.fail("Undo ignored an unloaded chunk."); }
            catch (IllegalStateException expected) {
                context.assertTrue(expected.getMessage().contains("unloaded chunks"), "Clear message: " + expected.getMessage());
            }
            context.assertTrue(level.getBlockState(loaded) == before && !before.is(Blocks.STONE) && !edit.restored,
                    "Nothing was restored in the loaded half.");
        }
        context.succeed();
    }

    @GameTest
    public void cancellingARebuildNeedsTheUndoPermission(GameTestHelper context) {
        try (PromptManager manager = manager(context)) {
            ServerPlayer player = context.makeMockServerPlayerInLevel();
            Job rebuild = job(context, player, payload("minecraft:stone", null), 1);
            rebuild.mode = Job.Mode.APPLY; rebuild.rebuildFrom = "local-saved-plan";
            manager.active.put(rebuild.id, rebuild);
            try { manager.cancel(player, null); context.fail("A player without the undo node cancelled a rebuild."); }
            catch (IllegalArgumentException expected) {
                context.assertTrue(expected.getMessage().contains("worldprompt.command.undo"), expected.getMessage());
            }
            context.assertTrue(!rebuild.cancelling && !rebuild.cancelRestored, "The refused cancel changed nothing.");
        }
        context.succeed();
    }

    @GameTest
    public void evictedEditsCountAsCommittedAndAreNotReferencedByObject(GameTestHelper context) {
        try (PromptManager manager = manager(context)) {
            ServerPlayer player = context.makeMockServerPlayerInLevel();
            Job first = job(context, player, payload("minecraft:stone", null));
            Job second = job(context, player, payload("minecraft:gold_block", null));
            var older = new AppliedEdit(first.id, player.getUUID(), "player", first.snapshot, first.prompt,
                    first.replay, first.palette, "model", List.of(), List.of());
            var newer = new AppliedEdit(second.id, player.getUUID(), "player", second.snapshot, second.prompt,
                    second.replay, second.palette, "model", List.of(older.id), List.of());
            older.restored = true; newer.restored = true;
            manager.history(player.getUUID()).addFirst(older);
            manager.history(player.getUUID()).addFirst(newer);
            try { manager.rebuild(player, newer.id); context.fail("Rebuild ignored an undone dependency."); }
            catch (IllegalArgumentException expected) {
                context.assertTrue(expected.getMessage().contains("has since been undone"), expected.getMessage());
            }
            manager.history(player.getUUID()).remove(older);
            manager.rebuild(player, newer.id);
            context.assertTrue(manager.active.size() == 1,
                    "Once the dependency left the history it counts as committed, so the rebuild starts.");
        }
        context.succeed();
    }

    private static PromptManager manager(GameTestHelper context) {
        AdapterConfig config = new AdapterConfig();
        config.token = "unused-local-regression-token";
        return new PromptManager(context.getLevel().getServer(), config);
    }

    private static Job job(GameTestHelper context, ServerPlayer player, ReplayPayload replay) {
        return job(context, player, replay, 0);
    }

    private static Job job(GameTestHelper context, ServerPlayer player, ReplayPayload replay, int delayTicks) {
        // Keep emitted sounds/particles outside the packet-observation test's range.
        BlockPos pos = context.absolutePos(new BlockPos(1, 100, 1));
        context.getLevel().setBlock(pos, Blocks.DIRT.defaultBlockState(), 3);
        RegionSnapshot snapshot = new RegionSnapshot(context.getLevel(),
                RegionBounds.between(pos.getX(), pos.getY(), pos.getZ(), pos.getX(), pos.getY(), pos.getZ(), 1));
        snapshot.capture(context.getLevel(), 1);
        Job job = new Job(player, snapshot, PromptRequest.parse("regression", "single"), delayTicks);
        job.replay = replay;
        job.finalPlan = replay.plan;
        job.operationCount = replay.operations.size();
        job.palette.add(Blocks.GOLD_BLOCK.defaultBlockState());
        job.lastContactAt = System.currentTimeMillis();
        return job;
    }

    private static JsonObject remote(Job job, String status) {
        return new Gson().toJsonTree(Map.of("id", job.id, "owner_id", job.owner.toString(),
                "status", status, "message", "Fixture stopped the job")).getAsJsonObject();
    }

    private static ReplayPayload payload(String state, String match) {
        var first = Map.<String, Object>of("op", "single", "at", List.of(0, 0, 0), "block", List.of(state));
        var second = Map.<String, Object>of("op", "replace", "min", List.of(0, 0, 0), "max", List.of(0, 0, 0),
                "block", "minecraft:gold_block", "match", List.of(match == null ? state : match), "preserve_properties", false);
        List<Map<String, Object>> definitions = match == null ? List.of(first) : List.of(first, second);
        String runs = Base64.getEncoder().encodeToString(ByteBuffer.allocate(12)
                .order(ByteOrder.LITTLE_ENDIAN).putInt(0).putInt(1).putInt(0).array());
        List<Map<String, Object>> packed = java.util.stream.IntStream.range(0, definitions.size())
                .mapToObj(i -> Map.<String, Object>of("index", i, "op", definitions.get(i).get("op"),
                        "run_count", 1, "writes", 1, "dynamic_match", i > 0, "runs_base64", runs)).toList();
        return ReplayPayload.parse(new Gson().toJsonTree(Map.of("result_version", 2, "run_encoding", "base64-u32le",
                "dimensions", List.of(1, 1, 1), "palette", List.of("minecraft:gold_block"),
                "operation_count", definitions.size(), "plan", Map.of("version", 1, "operations", definitions),
                "operations", packed)).getAsJsonObject());
    }
}
