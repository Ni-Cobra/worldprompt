package dev.worldprompt;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicLong;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.Vec3;

/** One unfinished prompt or rebuild. Only the server tick thread touches it; futures carry the off-thread replies. */
final class Job {
    enum Mode { CAPTURE, SUBMIT, WAIT, RESULT, PALETTE, START, APPLY, FINISH, DONE }

    private static final AtomicLong ORDER = new AtomicLong();
    final String id = UUID.randomUUID().toString();
    final long order = ORDER.incrementAndGet();
    /** IDs of the edits standing in the region when the snapshot was captured. */
    List<String> base = List.of();
    final UUID owner;
    final String ownerName;
    final RegionSnapshot snapshot;
    final PromptRequest prompt;
    /** Server ticks between complete operations; 0 places the whole replay in one tick. */
    final int delayTicks;
    final long createdAt = System.currentTimeMillis();
    final List<BlockState> palette = new ArrayList<>();
    final Set<String> blockWarnings = new HashSet<>();
    long skippedWrites;
    final JsonObject viewpoint;
    AppliedEdit edit;
    String modelLabel = "Model pending";
    boolean modelAnnounced;
    Mode mode = Mode.CAPTURE;
    String status = "Reading your selection", backendStatus = "", failure = "", soundStage = "", rebuildFrom;
    CompletableFuture<JsonObject> pending, heartbeat, progressAck, cancelRequest;
    boolean submitted, cancelling, applied, userCancelled, cancelRestored, cancelRestoreFailed;
    long reportedTokens;
    long retryAt, nextPoll, nextOperationTick, lastContactAt;
    /** When the job started stopping (cancel or failure), for the bounded wait on Core's confirmation. */
    long cancelStartedAt;
    int operationCount, operation, runIndex, runWritten, refusedConnections, submitRetries;
    int[] runs;
    ReplayPayload replay;
    CompletableFuture<ReplayPayload> resultPending;
    JsonObject finalPlan;
    int choiceOperation = -1;
    boolean preserveChoiceProperties;
    boolean dynamicMatch;
    List<ReplayPlacer.PlacementChoice> activeChoices = List.of();
    List<ReplayPlacer.MatchRule> matchRules = List.of();

    Job(ServerPlayer player, RegionSnapshot snapshot, PromptRequest prompt, int delayTicks) {
        owner = player.getUUID(); ownerName = player.getGameProfile().name();
        this.snapshot = snapshot; this.prompt = prompt; this.delayTicks = delayTicks;
        // Capture immediately in /wp prompt, before multi-tick capture or any network work.
        viewpoint = new JsonObject();
        viewpoint.add("position", vector(player.position()));
        viewpoint.add("eye_position", vector(player.getEyePosition()));
        viewpoint.add("look_direction", vector(player.getLookAngle()));
        viewpoint.addProperty("yaw", player.getYRot()); viewpoint.addProperty("pitch", player.getXRot());
        viewpoint.addProperty("dimension", snapshot.dimension.identifier().toString());
        JsonArray origin = new JsonArray();
        origin.add(snapshot.bounds.minX()); origin.add(snapshot.bounds.minY()); origin.add(snapshot.bounds.minZ());
        viewpoint.add("selection_origin", origin);
    }

    /** The level this job captured, or an error when its dimension is gone. */
    ServerLevel level(MinecraftServer server) {
        ServerLevel level = server.getLevel(snapshot.dimension);
        if (level == null) throw new IllegalStateException("The captured dimension is no longer available.");
        return level;
    }

    private static JsonArray vector(Vec3 value) {
        JsonArray result = new JsonArray(); result.add(value.x); result.add(value.y); result.add(value.z); return result;
    }
}
