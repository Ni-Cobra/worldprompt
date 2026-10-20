package dev.worldprompt;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import dev.worldprompt.kernel.RegionBounds;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import net.minecraft.ChatFormatting;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerPlayer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Walks each {@link Job} through its protocol with Core: capture, SUBMIT, WAIT, RESULT, PALETTE, START,
 * APPLY and FINISH, plus the cancel loop and the retry rules of every stage. Core replies arrive as
 * futures completed on {@link CoreClient}'s threads; this class only looks at them from the server tick
 * thread, one {@link #advance} per job per tick, so a job never changes under another thread.
 */
final class CoreJobDriver {
    private static final Logger LOG = LoggerFactory.getLogger("worldprompt");
    /** Core's 409 code when the player already has `world_max_jobs_per_player` unfinished jobs. */
    static final String PLAYER_JOB_LIMIT = "player_job_limit";
    /** Refused connections tolerated while submitting, about ten seconds with the 2 s retry. */
    static final int UNREACHABLE_ATTEMPTS = 5;
    /** Total failed submission attempts (any cause) before the job is stopped; about 30 s with the 2 s retry. */
    static final int MAX_SUBMIT_RETRIES = 15;
    /** How long a Core that stays silent keeps a running build alive; Core's own lease is longer. */
    static final long CONNECTION_GRACE_MS = 15_000;
    /** After a cancel or failure, how long to keep asking Core to stop before releasing the region locally. */
    static final long CANCEL_GIVE_UP_MS = 60_000;
    /** How long closing the manager waits for Core to answer the shutdown cancellations. */
    static final long SHUTDOWN_CANCEL_WAIT_MS = 2_000;
    private final MinecraftServer server;
    private final AdapterConfig config;
    private final CoreClient client;
    private final EditHistory edits;
    private final ReplayPlacer placer;
    private final Feedback feedback;
    private final Map<String, Object> blockCatalog;

    CoreJobDriver(MinecraftServer server, AdapterConfig config, CoreClient client, EditHistory edits,
                  ReplayPlacer placer, Feedback feedback, Map<String, Object> blockCatalog) {
        this.server = server; this.config = config; this.client = client;
        this.edits = edits; this.placer = placer; this.feedback = feedback;
        // Registry contents are frozen before this server host starts. Keep the same immutable snapshot for
        // retries, so a resubmitted job ID sends byte-identical data. Null when this server has none to send.
        this.blockCatalog = blockCatalog;
    }

    /**
     * The job request. {@code block_catalog} is optional in the protocol: when this server has no
     * catalog Core can carry, the key is left out and Core uses its bundled vanilla registry, rather
     * than being sent a partial snapshot presented as this server's own.
     */
    private Map<String, Object> submission(Job job) {
        Map<String, Object> request = new LinkedHashMap<>();
        request.put("request_id", job.id);
        request.put("owner_id", job.owner.toString());
        request.put("owner_name", job.ownerName);
        request.put("prompt", job.prompt.prompt());
        request.put("effort", job.prompt.effort());
        request.put("source", job.snapshot.transport());
        // Both remaining fields are optional in the protocol; send neither as a JSON null.
        if (job.viewpoint != null) request.put("viewpoint", job.viewpoint);
        request.put("lecterns", job.snapshot.lecterns.transport());
        if (blockCatalog != null) request.put("block_catalog", blockCatalog);
        return Collections.unmodifiableMap(request);
    }

    /** One tick of one job. {@code ticks} is the manager's tick counter, passed through to the placer. */
    void advance(Job job, long ticks) {
        long now = System.currentTimeMillis();
        if (job.cancelling) { cancel(job, now); return; }
        switch (job.mode) {
            case CAPTURE -> {
                if (job.snapshot.capture(job.level(server), config.blocksPerTick)) {
                    job.snapshot.lecterns.validate();
                    job.snapshot.lecterns.summary().forEach(line -> feedback.tell(job, line, ChatFormatting.GRAY));
                    job.snapshot.lecterns.warnings().forEach(line -> feedback.tell(job, line, ChatFormatting.YELLOW));
                    job.mode = Job.Mode.SUBMIT; job.status = "Preparing reconstruction";
                }
            }
            case SUBMIT -> {
                if (job.pending == null && now >= job.retryAt) {
                    job.submitted = true;
                    job.pending = client.requestLazy("POST", "/api/world/jobs", () -> submission(job));
                }
                if (finished(job.pending)) {
                    try { acceptRemote(job, take(job)); job.mode = Job.Mode.WAIT; }
                    catch (RuntimeException e) {
                        Throwable cause = CoreClient.cause(e);
                        if (cause instanceof CoreMismatchException) {
                            // Core answered for another job or owner: not transient, and not safe to keep talking to.
                            fail(job, cause.getMessage());
                        } else if (cause instanceof CoreClient.TokenException) {
                            unreachable(job, cause.getMessage());
                        } else if (cause instanceof CoreClient.ApiException api && permanent(api)) {
                            finish(job, false, PLAYER_JOB_LIMIT.equals(api.code) ? "Not started · " + api.getMessage() : api.getMessage());
                        } else if (cause instanceof java.net.ConnectException) {
                            // Nothing listens on Core's port: allow a restart, then stop instead of retrying forever.
                            if (++job.refusedConnections >= UNREACHABLE_ATTEMPTS)
                                unreachable(job, "Cannot reach WorldPrompt Core. Start Core on this computer, then prompt again.");
                            else submitRetry(job, now, "Core unreachable; retrying");
                        } else submitRetry(job, now, "Submission interrupted; retrying the same job ID");
                    }
                }
            }
            case WAIT -> {
                heartbeat(job, now);
                if (job.cancelling) return;
                if ("ready".equals(job.backendStatus)) { job.mode = Job.Mode.RESULT; job.status = "Preparing reconstruction"; }
            }
            case RESULT -> {
                heartbeat(job, now);
                if (job.cancelling) return;
                if (job.resultPending == null) job.resultPending = client.result(path(job) + "/result");
                if (finished(job.resultPending)) {
                    ReplayPayload result = job.resultPending.join();
                    job.resultPending = null;
                    RegionBounds b = job.snapshot.bounds;
                    if (result.width != b.width() || result.height != b.height() || result.length != b.length())
                        throw new IllegalArgumentException("Core result dimensions differ from the captured selection.");
                    job.replay = result;
                    job.operationCount = result.operations.size();
                    job.finalPlan = result.plan;
                    updateUsage(job, result.modelSettings);
                    job.mode = Job.Mode.PALETTE;
                }
            }
            case PALETTE -> {
                heartbeat(job, now);
                if (job.cancelling) return;
                placer.parsePalette(job);
                if (job.palette.size() == job.replay.palette.size()) {
                    job.mode = Job.Mode.START; job.status = "Preparing reconstruction";
                }
            }
            case START -> {
                if (job.rebuildFrom != null) {
                    if (!job.edit.restored) placer.restore(job.snapshot, job.edit);
                    edits.markApplied(job);
                    job.mode = Job.Mode.APPLY; job.status = "Building · 0/" + job.operationCount;
                } else {
                    heartbeat(job, now);
                    if (job.cancelling) return;
                    if (job.pending == null) job.pending = application(job, "reconstructing", "Building");
                    if (finished(job.pending)) {
                        updateUsage(job, unwrap(take(job))); job.lastContactAt = now;
                        edits.markApplied(job);
                        job.mode = Job.Mode.APPLY; job.status = "Building · 0/" + job.operationCount;
                    }
                }
            }
            case APPLY -> { heartbeat(job, now); apply(job, now, ticks); }
            case FINISH -> {
                if (job.rebuildFrom != null) {
                    finish(job, true, "Rebuild complete · /wp undo and /wp rebuild available");
                    return;
                }
                if (job.progressAck != null && !job.progressAck.isDone()) return;
                if (job.pending == null && now >= job.retryAt) job.pending = application(job, "completed", "Build complete");
                if (finished(job.pending)) {
                    String undoHint = job.applied ? " · /wp undo and /wp rebuild available" : "";
                    try { updateUsage(job, unwrap(take(job))); finish(job, true, "Build complete" + undoHint); }
                    catch (RuntimeException e) {
                        Throwable cause = CoreClient.cause(e);
                        // Core refusing the acknowledgement (job gone, lease expired, already finished) cannot
                        // change on retry. The blocks are placed, so finish locally and keep the undo entry.
                        if (!(cause instanceof CoreClient.ApiException api && permanent(api))) {
                            retry(job, now, "Edit applied; waiting for core completion acknowledgement");
                            return;
                        }
                        LOG.warn("WorldPrompt job {}: Core rejected the completion acknowledgement: {}", job.id, api.getMessage());
                        finish(job, true, "Build complete" + undoHint + " · Core could not record the completion (" + api.getMessage() + ")");
                    }
                    ServerPlayer player = feedback.owner(job);
                    if (config.summaryBook && job.edit != null && player != null) Feedback.giveBook(player, job.edit);
                }
            }
            case DONE -> { }
        }
    }

    private void heartbeat(Job job, long now) {
        if (job.rebuildFrom != null) return;
        if (job.heartbeat == null && now >= job.nextPoll) {
            job.heartbeat = client.request("GET", path(job), null);
            job.nextPoll = now + config.pollMilliseconds;
        }
        if (finished(job.heartbeat)) {
            CompletableFuture<JsonObject> current = job.heartbeat; job.heartbeat = null;
            try {
                JsonObject remote = unwrap(current.join());
                if (!job.id.equals(str(remote, "id", "")) || !job.owner.toString().equals(str(remote, "owner_id", "")))
                    throw new CoreMismatchException("Core heartbeat returned another owner's job.");
                updateUsage(job, remote);
                job.lastContactAt = now;
                String status = str(remote, "status", "unknown");
                if (List.of("failed", "cancelling", "completed").contains(status)) {
                    if (!status.equals("completed")) fail(job, str(remote, "message", "Core job failed."));
                    else if (job.mode != Job.Mode.FINISH) fail(job, "Core job completed before this adapter finished reconstruction.");
                    return;
                }
                job.backendStatus = status;
                if (job.mode == Job.Mode.WAIT) job.status = remoteStage(remote);
                if (job.mode == Job.Mode.APPLY && (job.progressAck == null || job.progressAck.isDone()))
                    job.progressAck = application(job, "reconstructing", job.status);
            } catch (RuntimeException e) {
                Throwable cause = CoreClient.cause(e);
                if (cause instanceof CoreMismatchException) fail(job, cause.getMessage());
                else if (cause instanceof CoreClient.ApiException api && api.status == 404) fail(job, "Core lost this job, possibly after a restart.");
                else if (job.mode == Job.Mode.WAIT) job.status = "Core connection interrupted · retrying";
                // A single failed poll is no reason to abandon a build. Core is only presumed gone after the
                // grace period without any answer, which also bounds what happens during result download.
                else if (now - job.lastContactAt > CONNECTION_GRACE_MS)
                    fail(job, "Core connection failed during reconstruction. Original snapshot retained.");
            }
        }
    }

    private void apply(Job job, long now, long ticks) {
        if (job.delayTicks > 0) { applyStep(job, now, ticks, config.blocksPerTick); return; }
        // A zero delay places the whole remaining replay in this tick, ignoring blocksPerTick:
        // each step finishes one complete operation, and the last step enters FINISH.
        while (job.mode == Job.Mode.APPLY && !job.cancelling) applyStep(job, now, ticks, Long.MAX_VALUE);
    }
    private void applyStep(Job job, long now, long ticks, long budget) {
        if (job.cancelling) return;
        if (job.rebuildFrom == null && !connectionFresh(job, now)) return;
        placer.step(job, ticks, budget);
    }
    private boolean connectionFresh(Job job, long now) {
        // Stop before the service ownership lease expires, even if HTTP is hanging.
        if (now - job.lastContactAt <= CONNECTION_GRACE_MS) return true;
        fail(job, "Core heartbeat was lost; reconstruction stopped. Original snapshot retained.");
        return false;
    }

    /** One step of a stopping job's conversation with Core, also used right when the player cancels. */
    void cancel(Job job, long now) {
        if (!job.submitted) { finish(job, false, job.failure); return; }
        // A Core that never confirms must not hold the region forever; its lease expiry stops the job.
        if (job.cancelStartedAt > 0 && now - job.cancelStartedAt > CANCEL_GIVE_UP_MS) {
            LOG.warn("WorldPrompt job {}: Core did not confirm the cancellation within {} s", job.id, CANCEL_GIVE_UP_MS / 1000);
            finish(job, false, job.failure + " · Core did not confirm the stop; it ends the job when its lease expires");
            return;
        }
        // Let an ambiguous in-flight submission resolve before requesting cancellation of its stable ID.
        if (job.pending != null) {
            if (!job.pending.isDone()) return;
            try { take(job); } catch (RuntimeException ignored) { }
        }
        if (job.cancelRequest == null && now >= job.retryAt) {
            job.cancelRequest = client.request("POST", path(job) + "/cancel", Map.of("message", job.failure));
            job.retryAt = now + config.pollMilliseconds;
        }
        if (finished(job.cancelRequest)) {
            CompletableFuture<JsonObject> current = job.cancelRequest; job.cancelRequest = null;
            try {
                JsonObject remote = unwrap(current.join());
                updateUsage(job, remote);
                if (List.of("failed", "completed").contains(str(remote, "status", ""))) finish(job, false, job.failure);
            } catch (RuntimeException e) {
                Throwable cause = CoreClient.cause(e);
                if (cause instanceof CoreClient.ApiException api && api.status == 404) finish(job, false, job.failure);
            }
        }
    }
    /** Stop the job: world writes end now, and the cancel loop keeps asking Core until it confirms or gives up. */
    void fail(Job job, String message) {
        if (job.mode == Job.Mode.DONE || job.cancelling) return;
        job.cancelling = true; job.failure = message;
        job.cancelStartedAt = System.currentTimeMillis();
        job.status = job.rebuildFrom == null ? "Failed · stopping core job" : "Rebuild failed";
        feedback.failed(job, message);
    }
    private void finish(Job job, boolean success, String message) {
        if (success && !job.blockWarnings.isEmpty()) message += " · " + job.blockWarnings.size()
                + " block warning(s) · " + job.skippedWrites + " placement(s) skipped";
        job.mode = Job.Mode.DONE; edits.active.remove(job.id, job); edits.cleanup.remove(job.id, job);
        feedback.finished(job, success, message);
    }
    private void unreachable(Job job, String message) {
        finish(job, false, message);
        feedback.setupHelp(job);
    }

    /**
     * Ask Core to cancel every submitted job, waiting briefly for its answers. A cancellation Core does
     * not answer in time costs at most one lease, not a stuck planner: the heartbeat lease also releases
     * every abandoned job after an abrupt server stop.
     */
    void cancelAllForShutdown() {
        List<Job> jobs = new ArrayList<>(edits.active.values()); jobs.addAll(edits.cleanup.values());
        List<CompletableFuture<JsonObject>> cancels = new ArrayList<>();
        for (Job job : jobs) if (job.submitted)
            cancels.add(client.request("POST", path(job) + "/cancel", Map.of("message", "Minecraft server stopped.")));
        if (!cancels.isEmpty()) {
            try {
                CompletableFuture.allOf(cancels.toArray(new CompletableFuture[0]))
                        .get(SHUTDOWN_CANCEL_WAIT_MS, java.util.concurrent.TimeUnit.MILLISECONDS);
            } catch (java.util.concurrent.TimeoutException late) {
                LOG.warn("WorldPrompt: Core did not answer {} shutdown cancellation(s) within {} ms; its lease expiry will stop them.",
                        cancels.size(), SHUTDOWN_CANCEL_WAIT_MS);
            } catch (java.util.concurrent.ExecutionException failed) {
                LOG.warn("WorldPrompt: some shutdown cancellations failed: {}", safeMessage(failed));
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            }
        }
    }

    private CompletableFuture<JsonObject> application(Job job, String status, String message) {
        return client.request("POST", path(job) + "/application", Map.of("status", status, "operation", job.operation, "message", message));
    }
    private static boolean finished(CompletableFuture<?> future) { return future != null && future.isDone(); }
    private static JsonObject take(Job job) {
        CompletableFuture<JsonObject> current = job.pending; job.pending = null;
        return current.join();
    }
    private static void retry(Job job, long now, String status) { job.retryAt = now + 2000; job.status = status; }
    /** A submission failure that may succeed later; gives up after {@link #MAX_SUBMIT_RETRIES} in total. */
    private void submitRetry(Job job, long now, String status) {
        if (++job.submitRetries >= MAX_SUBMIT_RETRIES) {
            fail(job, "Core did not accept the job after " + job.submitRetries + " attempts.");
            return;
        }
        retry(job, now, status);
    }
    /** Client errors other than timeouts and rate limits repeat identically on every retry. */
    private static boolean permanent(CoreClient.ApiException api) {
        return api.status >= 400 && api.status < 500 && api.status != 408 && api.status != 429;
    }
    /** Core answered, but about a different job or owner than the one this adapter asked about. */
    private static final class CoreMismatchException extends IllegalArgumentException {
        CoreMismatchException(String message) { super(message); }
    }
    private static JsonObject unwrap(JsonObject value) { return value.has("job") ? value.getAsJsonObject("job") : value; }
    private void acceptRemote(Job job, JsonObject value) {
        JsonObject remote = unwrap(value);
        if (!job.id.equals(str(remote, "id", ""))) throw new CoreMismatchException("Core returned another job's ID.");
        if (!job.owner.toString().equals(str(remote, "owner_id", ""))) throw new CoreMismatchException("Core returned another owner's job.");
        job.lastContactAt = System.currentTimeMillis();
        updateUsage(job, remote);
        job.backendStatus = str(remote, "status", "queued"); job.status = remoteStage(remote);
    }
    private void updateUsage(Job job, JsonObject remote) {
        if (remote.has("model")) {
            job.modelLabel = modelLabel(remote);
            if (!job.modelAnnounced && job.rebuildFrom == null) {
                feedback.tell(job, job.modelLabel, ChatFormatting.LIGHT_PURPLE);
                job.modelAnnounced = true;
            }
        }
        TokenUsage reported = TokenUsage.from(remote);
        if (reported.totalTokens() != null) {
            // Emit once when new accounting arrives, never every poll or again
            // at reconstruction completion. Rebuild and undo make no model call.
            if (job.rebuildFrom == null && reported.totalTokens() > job.reportedTokens) {
                job.reportedTokens = reported.totalTokens();
                feedback.tell(job, job.modelLabel + " · usage" + reported.suffix(), ChatFormatting.LIGHT_PURPLE);
            }
        }
    }
    static String remoteStage(JsonObject remote) {
        String state = str(remote, "status", "unknown");
        return switch (state) {
            case "queued" -> "Waiting to start" + (remote.has("queue_position") && !remote.get("queue_position").isJsonNull() ? " · position " + remote.get("queue_position").getAsInt() : "");
            case "preparing" -> "Reading your selection";
            case "planning" -> str(remote, "model", "Model") + " · planning";
            case "checking" -> "Checking the layout";
            case "reviewing" -> str(remote, "model", "Model") + " · reviewing and refining";
            case "validating" -> "Preparing reconstruction";
            case "ready" -> "Preparing reconstruction";
            case "reconstructing" -> "Building · " + str(remote, "operation", "0") + "/" + str(remote, "operation_count", "?");
            case "cancelling" -> "Stopping your job";
            default -> state;
        };
    }
    static String str(JsonObject object, String key, String fallback) {
        JsonElement value = object.get(key); return value == null || value.isJsonNull() ? fallback : value.getAsString();
    }
    static String modelLabel(JsonObject remote) {
        return str(remote, "model", "Model unknown") + " · effort " + str(remote, "reasoning_effort", "unknown")
                + " · fast mode " + (remote.has("fast_mode") ? (str(remote, "fast_mode", "unknown").equals("true") ? "on" : "off") : "unknown");
    }
    static String safeMessage(Throwable error) { return String.valueOf(CoreClient.cause(error).getMessage()); }
    static String path(Job job) { return "/api/world/jobs/" + job.id; }
}
