package dev.worldprompt;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import dev.worldprompt.kernel.CoreLimits;
import dev.worldprompt.kernel.PromptRequest;
import dev.worldprompt.kernel.RegionBounds;
import dev.worldprompt.kernel.Versions;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import net.minecraft.ChatFormatting;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.core.BlockPos;
import net.minecraft.network.chat.Component;
import net.minecraft.resources.ResourceKey;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.sounds.SoundEvents;
import net.minecraft.world.level.ClipContext;
import net.minecraft.world.level.Level;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * The command entry point behind {@code /wp}: selections, delays and the player-facing commands. It wires
 * {@link EditHistory} (undo stack and region reservations), {@link CoreJobDriver} (the Core protocol),
 * {@link ReplayPlacer} (world writes) and {@link Feedback} (what the player sees and hears) together and
 * ticks them. All mutable state, snapshots, and world writes are confined to the server tick thread;
 * only {@link CoreClient}'s HTTP calls run elsewhere.
 */
final class PromptManager implements AutoCloseable {
    private static final Logger LOG = LoggerFactory.getLogger("worldprompt");
    /** Ticks between plan operations until a player runs /wp delay; 0 would place a whole build in one tick. */
    static final int DEFAULT_DELAY_TICKS = 1;
    private final MinecraftServer server;
    private final AdapterConfig config;
    private final CoreClient client;
    private final EditHistory edits;
    private final Feedback feedback;
    private final ReplayPlacer placer;
    private final CoreJobDriver driver;
    private final Map<UUID, Selection> selections = new HashMap<>();
    private final Map<UUID, Integer> delays = new HashMap<>();
    /** Running jobs by job ID, in submission order; a player may own several. */
    final Map<String, Job> active;
    /** Stopping jobs whose snapshot was already restored locally, awaiting Core's acknowledgement. */
    final Map<String, Job> cleanup;
    /** Server ticks seen by this manager; operation delays are counted against it. */
    private long ticks;

    PromptManager(MinecraftServer server, AdapterConfig config) {
        // Capture the native snapshot before opening the HTTP client's resources. Entries Core's
        // protocol cannot carry are left out and logged here, once per server host.
        BlockCatalogTransport.Capture catalog = BlockCatalogTransport.capture();
        String excluded = catalog.exclusionWarning();
        if (excluded != null) LOG.warn(excluded);
        String unusable = catalog.unusableError();
        if (unusable != null) LOG.error(unusable);
        this.server = server; this.config = config; client = new CoreClient(config);
        edits = new EditHistory(config); active = edits.active; cleanup = edits.cleanup;
        feedback = new Feedback(server, config, edits);
        placer = new ReplayPlacer(server, config, edits, feedback);
        driver = new CoreJobDriver(server, config, client, edits, placer, feedback, catalog.transport());
    }
    void corner(ServerPlayer player, boolean first, BlockPos pos) {
        Selection old = selections.get(player.getUUID());
        ResourceKey<Level> dimension = player.level().dimension();
        if (old == null || !old.dimension().equals(dimension)) old = new Selection(dimension, null, null);
        selections.put(player.getUUID(), first ? new Selection(dimension, pos.immutable(), old.b())
                : new Selection(dimension, old.a(), pos.immutable()));
        tell(player, (first ? "pos1" : "pos2") + " = " + pos.toShortString() + " · " + dimension.identifier());
    }
    void selectLookedAt(ServerPlayer player) {
        selectLookedAt(player, config.selectionCubeSize, config.selectionCubeSize, config.selectionCubeSize);
    }
    void selectLookedAt(ServerPlayer player, int sizeX, int sizeY, int sizeZ) {
        BlockPos center = lookedAtLoadedBlock(player);
        if (center == null)
            throw new IllegalArgumentException("Look at a block in a loaded chunk.");
        selectAround(player, center, sizeX, sizeY, sizeZ);
    }
    /** Clip one loaded chunk at a time so distant targeting never loads a new chunk. */
    private static BlockPos lookedAtLoadedBlock(ServerPlayer player) {
        ServerLevel level = player.level();
        Vec3 eye = player.getEyePosition();
        Vec3 direction = player.getLookAngle();
        double limit = 2.0 * CoreLimits.MAX_WORLD_COORDINATE;
        if (direction.y > 0) limit = Math.min(limit, (level.getMaxY() + 1.0 - eye.y) / direction.y);
        if (direction.y < 0) limit = Math.min(limit, (level.getMinY() - eye.y) / direction.y);
        double distance = 0;
        while (distance < limit) {
            Vec3 probe = eye.add(direction.scale(distance + 1e-6));
            BlockPos position = BlockPos.containing(probe);
            if (!level.hasChunkAt(position)) return null;
            int chunkX = Math.floorDiv(position.getX(), 16);
            int chunkZ = Math.floorDiv(position.getZ(), 16);
            double end = limit;
            if (direction.x > 0) end = Math.min(end, ((chunkX + 1) * 16.0 - eye.x) / direction.x);
            if (direction.x < 0) end = Math.min(end, (chunkX * 16.0 - eye.x) / direction.x);
            if (direction.z > 0) end = Math.min(end, ((chunkZ + 1) * 16.0 - eye.z) / direction.z);
            if (direction.z < 0) end = Math.min(end, (chunkZ * 16.0 - eye.z) / direction.z);
            if (end <= distance) return null;
            Vec3 finish = eye.add(direction.scale(Math.max(distance, end - 1e-6)));
            HitResult hit = level.clip(new ClipContext(probe, finish, ClipContext.Block.OUTLINE,
                    ClipContext.Fluid.NONE, player));
            if (hit instanceof BlockHitResult block && hit.getType() == HitResult.Type.BLOCK)
                return block.getBlockPos();
            distance = end;
        }
        return null;
    }
    void selectAround(ServerPlayer player, BlockPos center, int sizeX, int sizeY, int sizeZ) {
        RegionBounds bounds;
        try {
            bounds = RegionBounds.around(center.getX(), center.getY(), center.getZ(),
                    sizeX, sizeY, sizeZ, config.maxSelectionBlocks);
        } catch (ArithmeticException error) {
            throw new IllegalArgumentException("The automatic selection is outside valid block coordinates.");
        }
        withinBuildHeight(player.level(), bounds, "The automatic selection extends outside this dimension's build height.");
        BlockPos first = new BlockPos(bounds.minX(), bounds.minY(), bounds.minZ());
        BlockPos second = new BlockPos(bounds.maxX(), bounds.maxY(), bounds.maxZ());
        selections.put(player.getUUID(), new Selection(player.level().dimension(), first, second));
        tell(player, "pos1 = " + first.toShortString() + " · pos2 = " + second.toShortString()
                + " · " + sizeX + " × " + sizeY + " × " + sizeZ
                + " around " + center.toShortString() + " · " + player.level().dimension().identifier());
    }
    /** The one build-height check for every region a player can ask for, whether picked by corners or by size. */
    private static RegionBounds withinBuildHeight(ServerLevel level, RegionBounds bounds, String outsideMessage) {
        if (!bounds.withinHeight(level.getMinY(), level.getMaxY())) throw new IllegalArgumentException(outsideMessage);
        return bounds;
    }
    void delay(ServerPlayer player, int delayTicks) {
        delays.put(player.getUUID(), delayTicks);
        tell(player, "Reconstruction delay: " + delayTicks + (delayTicks == 1 ? " tick" : " ticks") + " between JSON operations"
                + (delayTicks == 0 ? " · instant, whole build in one tick" : "") + " (for future prompts and rebuilds).");
    }
    void submit(ServerPlayer player, String text) {
        submit(player, text, "single");
    }
    /** Give the summary book of a retained edit; without an ID, of the newest one. */
    void book(ServerPlayer player, String id) {
        AppliedEdit edit = id != null ? edits.findEdit(player.getUUID(), id) : history(player.getUUID()).stream().findFirst()
                .orElseThrow(() -> new IllegalArgumentException("No WorldPrompt edit is available for /wp book."));
        Feedback.giveBook(player, edit);
    }
    void submit(ServerPlayer player, String text, String effort) {
        Selection selection = selections.get(player.getUUID());
        if (selection == null || !selection.complete())
            throw new IllegalArgumentException("Set both /wp pos1 and /wp pos2 first, in the same dimension.");
        if (!selection.dimension().equals(player.level().dimension()))
            throw new IllegalArgumentException("Return to the selected dimension or set both corners again.");
        RegionBounds b = withinBuildHeight(player.level(), selection.bounds(config.maxSelectionBlocks),
                "Selection extends outside this dimension's build height.");
        PromptRequest prompt = PromptRequest.parse(text, effort);
        edits.ensureRegionFree(player.level().dimension(), b, player.getUUID(), false);
        Job job = new Job(player, new RegionSnapshot(player.level(), b), prompt, delays.getOrDefault(player.getUUID(), DEFAULT_DELAY_TICKS));
        // The snapshot will contain these edits' blocks, so they cannot be undone while this edit stands.
        job.base = edits.liveEdits(job.snapshot.dimension, b, null).stream().map(edit -> edit.id).toList();
        int others = ownJobs(player.getUUID()).size();
        active.put(job.id, job);
        feedback.stageSound(job);
        tell(player, "WorldPrompt " + Feedback.shortId(job.id) + " · " + Feedback.passLabel(prompt.effort()) + " · capturing " + b.volume() + " blocks"
                + (others > 0 ? " · " + others + " other job" + (others == 1 ? "" : "s") + " running." : "."));
    }
    void rebuild(ServerPlayer player) {
        rebuild(player, null);
    }
    /** Rebuild the newest applied edit, or the edit named by its short or full job ID. */
    void rebuild(ServerPlayer player, String id) {
        AppliedEdit previous = id == null ? edits.newestEdit(player.getUUID(), false, "/wp rebuild") : edits.findEdit(player.getUUID(), id);
        if (edits.building(previous))
            throw new IllegalArgumentException(Feedback.shortId(previous.id) + " is still building. Use /wp cancel " + Feedback.shortId(previous.id) + " first.");
        if (server.getLevel(previous.snapshot.dimension) == null)
            throw new IllegalArgumentException("The saved edit's dimension is unavailable.");
        // Rebuild first restores the original snapshot unless the edit is already undone.
        if (previous.restored) edits.ensureRebuildBase(previous, player.getUUID());
        else edits.ensureNoDependents(previous, player.getUUID());
        edits.ensureRegionFree(previous.snapshot.dimension, previous.snapshot.bounds, player.getUUID(), false);
        // The rebuild restores the snapshot first; refuse now rather than fail after it has started.
        previous.snapshot.ensureLoaded(server.getLevel(previous.snapshot.dimension));
        Job job = new Job(player, previous.snapshot, previous.prompt, delays.getOrDefault(player.getUUID(), DEFAULT_DELAY_TICKS));
        job.base = previous.base;
        job.mode = Job.Mode.START; job.status = "Preparing local reconstruction";
        job.replay = previous.replay;
        job.finalPlan = previous.replay.plan;
        job.operationCount = previous.replay.operations.size();
        job.palette.addAll(previous.palette);
        job.modelLabel = previous.modelLabel;
        job.rebuildFrom = previous.id; job.edit = previous;
        active.put(job.id, job);
        feedback.stageSound(job);
        tell(player, Feedback.shortId(job.id) + " · Rebuilding " + Feedback.shortId(previous.id) + " · using the saved plan · no new model call.");
    }
    void undo(ServerPlayer player) {
        undo(player, null);
    }
    /** Without an ID, undo walks back through the player's stack, newest edit first. */
    void undo(ServerPlayer player, String id) {
        AppliedEdit edit;
        if (id == null) edit = edits.newestEdit(player.getUUID(), true, "/wp undo");
        else {
            edit = edits.findEdit(player.getUUID(), id);
            if (edit.restored)
                throw new IllegalArgumentException(Feedback.shortId(edit.id) + " is already undone. Use /wp rebuild " + Feedback.shortId(edit.id) + " to replay it.");
            if (edits.building(edit))
                throw new IllegalArgumentException(Feedback.shortId(edit.id) + " is still building. Use /wp cancel " + Feedback.shortId(edit.id) + " first.");
        }
        RegionSnapshot snapshot = edit.snapshot;
        if (server.getLevel(snapshot.dimension) == null) throw new IllegalArgumentException("The snapshot's dimension is unavailable.");
        edits.ensureNoDependents(edit, player.getUUID());
        edits.ensureRegionFree(snapshot.dimension, snapshot.bounds, player.getUUID(), true);
        Job previous = active.get(edit.id);
        if (previous != null && previous.cancelling) {
            // A network outage must never make a partial edit's snapshot unusable. The stopping
            // job keeps reserving its region separately while local undo runs without the core.
            active.remove(previous.id, previous);
            cleanup.put(previous.id, previous);
        }
        placer.restore(snapshot, edit);
        feedback.sound(player, SoundEvents.NOTE_BLOCK_FLUTE, 0.45f, 1.2f);
        tell(player, Feedback.shortId(edit.id) + " · Selection restored · /wp rebuild " + Feedback.shortId(edit.id) + " available", ChatFormatting.GREEN);
        if (ownJobs(player.getUUID()).stream().noneMatch(job -> active.containsKey(job.id)))
            player.sendOverlayMessage(Feedback.message("Selection restored", ChatFormatting.GREEN));
    }
    void cancel(ServerPlayer player) {
        cancel(player, null);
    }
    /** Without an ID, cancel the newest job that is not already stopping. */
    void cancel(ServerPlayer player, String id) {
        cancel(player, id, WorldPrompt.allowed("undo").test(player.createCommandSourceStack()));
    }
    /** The command entry point: the undo node is checked against the source that ran the command. */
    void cancel(CommandSourceStack source, String id) throws com.mojang.brigadier.exceptions.CommandSyntaxException {
        cancel(source.getPlayerOrException(), id, WorldPrompt.allowed("undo").test(source));
    }
    private void cancel(ServerPlayer player, String id, boolean mayUndo) {
        Job job;
        if (id == null) {
            List<Job> own = ownJobs(player.getUUID());
            if (own.isEmpty()) { tell(player, "No active WorldPrompt job to cancel."); return; }
            job = own.getLast();
            for (Job candidate : own.reversed()) if (!candidate.cancelling) { job = candidate; break; }
        } else job = edits.findJob(player.getUUID(), id);
        // Cancelling a rebuild restores its original snapshot, which is what /wp undo does.
        if (job.rebuildFrom != null && !job.cancelRestored && !mayUndo)
            throw new IllegalArgumentException("Cancelling a rebuild restores its original blocks, which needs the "
                    + WorldPrompt.PERMISSION_PREFIX + "undo permission.");
        job.userCancelled = true;
        job.status = "Stopping your job";
        if (!job.cancelling) {
            job.cancelling = true; job.failure = "Cancelled";
            job.cancelStartedAt = System.currentTimeMillis();
            job.status = "Stopping your job";
            tell(player, Feedback.shortId(job.id) + " · Stopping your job", ChatFormatting.YELLOW);
        }
        // Stop world writes immediately; keep the owner occupied until the core
        // confirms its worker has stopped, even during a network outage.
        if (job.rebuildFrom != null && !job.cancelRestored) {
            try {
                placer.restore(job.snapshot, job.edit);
                job.cancelRestored = true;
                job.cancelRestoreFailed = false;
                job.failure = "Rebuild cancelled · snapshot restored";
                tell(player, Feedback.shortId(job.id) + " · Rebuild snapshot restored", ChatFormatting.GREEN);
            } catch (RuntimeException error) {
                job.cancelRestoreFailed = true;
                job.failure = "Rebuild cancelled; restoration failed: " + CoreJobDriver.safeMessage(error)
                        + " · /wp undo " + Feedback.shortId(job.edit.id) + " is available";
                tell(player, Feedback.shortId(job.id) + " · " + job.failure, ChatFormatting.RED);
            }
        } else if (job.rebuildFrom == null && job.applied) {
            job.failure = "Cancelled · /wp undo " + Feedback.shortId(job.id) + " is available";
        }
        driver.cancel(job, System.currentTimeMillis());
    }
    /** Unfinished jobs of one player, oldest submission first. */
    List<Job> ownJobs(UUID owner) { return edits.ownJobs(owner); }
    Deque<AppliedEdit> history(UUID owner) { return edits.history(owner); }
    /** The player's retained edit IDs, newest first. */
    List<String> editIds(UUID owner) { return edits.editIds(owner); }
    void info(CommandSourceStack source) {
        client.request("GET", "/api/world/info", null).whenComplete((result, error) -> server.execute(() -> {
            if (error != null) {
                Throwable cause = CoreClient.cause(error);
                String detail = cause instanceof CoreClient.ApiException api && Versions.MISMATCH.equals(api.code) ? api.getMessage()
                        : cause instanceof CoreClient.ApiException api && api.status == 404 ? "Update Core to use /wp info."
                        : "Core unavailable: " + CoreJobDriver.safeMessage(error);
                source.sendFailure(Feedback.message(detail, ChatFormatting.RED));
                if (!(CoreClient.cause(error) instanceof CoreClient.ApiException)) source.sendSystemMessage(Feedback.setupHelp());
                return;
            }
            List<String> lines = infoLines(result, config.maxSelectionBlocks);
            if (lines == null) {
                LOG.warn("WorldPrompt Core returned an unreadable /api/world/info reply: {}", result);
                source.sendFailure(Feedback.message("Core replied, but the reply to /wp info could not be read. "
                        + "Core and this mod may be different versions; update both.", ChatFormatting.RED));
                return;
            }
            source.sendSuccess(() -> Feedback.message(lines.getFirst(), ChatFormatting.AQUA), false);
            for (String line : lines.subList(1, lines.size())) source.sendSuccess(() -> Feedback.message(line, ChatFormatting.GRAY), false);
        }));
    }
    /** The /wp info lines (headline first), or null when the reply is not shaped like Core's info document. */
    static List<String> infoLines(JsonObject result, int maxSelectionBlocks) {
        try {
            // Core sends "job_limits": false when its job limits are switched off and the values are unreachable.
            boolean limited = !str(result, "job_limits", "true").equals("false");
            return List.of("WorldPrompt Core " + str(result, "version", "unknown") + " · mod " + Versions.MOD
                            + " · " + (str(result, "stopping", "false").equals("true") ? "stopping" : "connected"),
                    "Configured provider: " + str(result, "provider", "unknown") + " · " + CoreJobDriver.modelLabel(result),
                    "Jobs: " + str(result, "active_jobs", "?") + " active · " + str(result, "queued_jobs", "?") + " queued · "
                            + (limited ? str(result, "world_concurrency", "?") + " planners · capacity " + str(result, "world_max_pending", "?")
                                    + " · per player " + str(result, "world_max_jobs_per_player", "?") : "no job limits"),
                    limited ? "Timeouts: Single pass " + str(result, "timeout_seconds", "?") + "s · Double pass "
                            + str(result, "high_timeout_seconds", "?") + "s per pass" : "Timeouts: none",
                    "Selection limit: " + maxSelectionBlocks + " blocks in this mod · "
                            + str(result, "max_selection_blocks", "?") + " in Core");
        } catch (RuntimeException unreadable) {
            return null;
        }
    }
    /**
     * Adds Core's unfinished jobs to {@code mine} or {@code others} and removes the ones it listed from
     * {@code local}. Returns false, leaving all three untouched, when the reply is malformed.
     */
    static boolean collectRemoteJobs(JsonObject result, UUID viewer, Map<String, Job> local, List<String> mine, List<String> others) {
        List<String> ownLines = new ArrayList<>(), otherLines = new ArrayList<>();
        Map<String, Job> remaining = new LinkedHashMap<>(local);
        try {
            for (JsonElement item : result.getAsJsonArray("jobs")) {
                JsonObject remote = item.getAsJsonObject();
                String id = str(remote, "id", "?");
                Job job = remaining.remove(id);
                String state = job == null ? CoreJobDriver.remoteStage(remote) : job.status;
                (str(remote, "owner_id", "").equals(String.valueOf(viewer)) ? ownLines : otherLines).add(Feedback.shortId(id) + " · "
                        + str(remote, "owner_name", "unknown") + " · "
                        + Feedback.passLabel(str(remote, "effort", "normal")) + " · " + CoreJobDriver.modelLabel(remote) + " · " + state);
            }
        } catch (RuntimeException unreadable) {
            return false;
        }
        mine.addAll(ownLines); others.addAll(otherLines);
        local.keySet().retainAll(remaining.keySet());
        return true;
    }
    void jobs(CommandSourceStack source, int page) {
        UUID viewer = source.getPlayer() == null ? null : source.getPlayer().getUUID();
        client.request("GET", "/api/world/jobs", null).whenComplete((result, error) -> server.execute(() -> {
            // The requesting player's jobs come first; Core lists several jobs per owner.
            List<String> lines = new ArrayList<>(), others = new ArrayList<>();
            Map<String, Job> local = new LinkedHashMap<>();
            active.values().forEach(job -> local.put(job.id, job));
            cleanup.values().forEach(job -> local.put(job.id, job));
            if (error != null) lines.add("Core unavailable; only this server's local jobs are shown.");
            else if (!collectRemoteJobs(result, viewer, local, lines, others)) {
                LOG.warn("WorldPrompt Core returned an unreadable /api/world/jobs reply: {}", result);
                lines.add("Core's job list could not be read (Core and this mod may be different versions); only this server's local jobs are shown.");
            }
            for (Job job : local.values()) (job.owner.equals(viewer) ? lines : others).add(Feedback.shortId(job.id) + " · " + job.ownerName + " · "
                    + (job.rebuildFrom == null ? Feedback.passLabel(job.prompt.effort()) : "Rebuild of " + Feedback.shortId(job.rebuildFrom))
                    + " · " + job.modelLabel + " · " + job.status);
            lines.addAll(others);
            int pages = Math.max(1, (lines.size() + 7) / 8);
            int selectedPage = Math.min(page, pages);
            source.sendSuccess(() -> Component.literal("WorldPrompt jobs · page " + selectedPage + "/" + pages
                    + (pages > 1 ? " · /wp jobs <page>" : "")).withStyle(ChatFormatting.AQUA), false);
            if (lines.isEmpty()) source.sendSuccess(() -> Feedback.message("No current jobs.", ChatFormatting.GRAY), false);
            for (int i = (selectedPage - 1) * 8; i < Math.min(lines.size(), selectedPage * 8); i++) {
                String line = lines.get(i);
                source.sendSuccess(() -> Feedback.message(line, ChatFormatting.GRAY), false);
            }
        }));
    }
    void disconnected(UUID owner) {
        for (Job job : ownJobs(owner))
            driver.fail(job, "Player disconnected; job stopped." + (job.applied ? " /wp undo " + Feedback.shortId(job.id) + " remains available after reconnect." : ""));
    }
    void tick() {
        ticks++;
        List<Job> jobs = new ArrayList<>(active.values());
        jobs.addAll(cleanup.values());
        for (Job job : jobs) {
            try {
                if (job.mode == Job.Mode.DONE) continue;
                driver.advance(job, ticks);
                if (job.mode != Job.Mode.DONE && active.get(job.id) == job) { feedback.stageSound(job); feedback.jobOutline(job, ticks); }
            } catch (Exception e) { driver.fail(job, CoreJobDriver.safeMessage(e)); }
        }
        feedback.actionBars(ticks);
        feedback.selectionOutlines(ticks, selections);
    }
    private static String str(JsonObject object, String key, String fallback) { return CoreJobDriver.str(object, key, fallback); }
    private static void tell(ServerPlayer player, String text) { Feedback.tell(player, text); }
    private static void tell(ServerPlayer player, String text, ChatFormatting color) { Feedback.tell(player, text, color); }
    @Override public void close() {
        driver.cancelAllForShutdown();
        edits.clear(); selections.clear(); delays.clear(); client.close();
    }
}
