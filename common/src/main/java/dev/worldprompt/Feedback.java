package dev.worldprompt;

import dev.worldprompt.kernel.RegionBounds;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import net.minecraft.ChatFormatting;
import net.minecraft.core.Holder;
import net.minecraft.core.particles.DustParticleOptions;
import net.minecraft.network.chat.ClickEvent;
import net.minecraft.network.chat.Component;
import net.minecraft.network.protocol.game.ClientboundSoundPacket;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.sounds.SoundEvent;
import net.minecraft.sounds.SoundEvents;
import net.minecraft.sounds.SoundSource;
import net.minecraft.util.Prediction;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/** Everything the player sees and hears: chat, the action bar, selection and job outlines, and note-block sounds. */
final class Feedback {
    private static final Logger LOG = LoggerFactory.getLogger("worldprompt");
    private static final String[] SPINNER = {"Ooo", "oOo", "ooO", "ooo"};
    private final MinecraftServer server;
    private final AdapterConfig config;
    private final EditHistory edits;

    Feedback(MinecraftServer server, AdapterConfig config, EditHistory edits) {
        this.server = server; this.config = config; this.edits = edits;
    }

    // Text shared by chat lines, jobs listings and the summary book.

    static String shortId(String id) { return id.substring(0, Math.min(8, id.length())); }
    /** Core stores "normal" and "high"; players know them as Single and Double pass. */
    static String passLabel(String effort) { return effort.equals("high") ? "Double pass" : "Single pass"; }
    static Component message(String text, ChatFormatting color) {
        return Component.literal(String.valueOf(text)).withStyle(color);
    }
    static void tell(ServerPlayer player, String text) { tell(player, text, ChatFormatting.GRAY); }
    static void tell(ServerPlayer player, String text, ChatFormatting color) {
        player.sendSystemMessage(Component.literal("[WorldPrompt] ").withStyle(ChatFormatting.AQUA).append(message(text, color)));
    }
    /** The summary book goes into the inventory, or drops at the player's feet when it is full. */
    static void giveBook(ServerPlayer player, AppliedEdit edit) {
        player.getInventory().placeItemBackInInventory(SummaryBook.create(edit.id, edit.prompt, edit.replay), Prediction.SERVER_ONLY);
        tell(player, shortId(edit.id) + " · Summary book added to your inventory · /wp book " + shortId(edit.id) + " gives another copy.");
    }
    /** Clickable project link, sent with errors meaning Core is not running or not reachable. */
    static Component setupHelp() {
        return Component.literal("[WorldPrompt] ").withStyle(ChatFormatting.AQUA).append(Component.literal("Setup help: " + WorldPrompt.PROJECT_URL)
                .withStyle(style -> style.withColor(ChatFormatting.AQUA).withUnderlined(true)
                        .withClickEvent(new ClickEvent.OpenUrl(java.net.URI.create(WorldPrompt.PROJECT_URL)))));
    }

    // Per-job messages, sent to the job's owner while they are online.

    ServerPlayer owner(Job job) { return server.getPlayerList().getPlayer(job.owner); }
    void tell(Job job, String text, ChatFormatting color) {
        ServerPlayer player = owner(job);
        if (player != null) tell(player, shortId(job.id) + " · " + text, color);
    }
    void setupHelp(Job job) {
        ServerPlayer player = owner(job);
        if (player != null) player.sendSystemMessage(setupHelp());
    }
    /** The job stopped itself; a player who already applied blocks is told how to restore them. */
    void failed(Job job, String message) {
        sound(job, SoundEvents.NOTE_BLOCK_BASS, 0.6f, 0.6f);
        ServerPlayer player = owner(job);
        if (player != null) tell(player, shortId(job.id) + " · " + message + (job.applied ? " Use /wp undo " + shortId(job.id)
                + " to restore the original snapshot, including while core cleanup is pending." : ""), ChatFormatting.RED);
        LOG.warn("WorldPrompt job {} stopped: {}", job.id, message);
    }
    /** The job ended. Another running job of this player keeps the action bar; the result stays in chat. */
    void finished(Job job, boolean success, String message) {
        ServerPlayer player = owner(job);
        if (player == null) return;
        ChatFormatting color = success ? (job.blockWarnings.isEmpty() ? ChatFormatting.GREEN : ChatFormatting.YELLOW)
                : job.userCancelled && !job.cancelRestoreFailed ? ChatFormatting.YELLOW : ChatFormatting.RED;
        if (edits.ownJobs(job.owner).stream().noneMatch(other -> edits.active.containsKey(other.id)))
            player.sendOverlayMessage(message(message, color));
        if (success || !job.cancelling) sound(job, success ? SoundEvents.NOTE_BLOCK_CHIME : SoundEvents.NOTE_BLOCK_BASS,
                0.6f, success ? 1.2f : 0.6f);
        tell(player, shortId(job.id) + " · " + message, color);
    }
    void blockWarning(Job job, String key, String warning) {
        if (!job.blockWarnings.add(key)) return;
        ServerPlayer player = owner(job);
        if (player != null) {
            if (job.blockWarnings.size() <= 5) tell(player, shortId(job.id) + " · Warning: " + warning, ChatFormatting.YELLOW);
            else if (job.blockWarnings.size() == 6)
                tell(player, shortId(job.id) + " · More block warnings suppressed; a summary will appear when the job finishes.", ChatFormatting.YELLOW);
        }
        LOG.warn("WorldPrompt job {}: {}", job.id, warning);
    }

    // The action bar and particle outlines, driven by the manager's tick counter.

    /** One action bar per player: the newest running job, plus how many others it owns. */
    void actionBars(long ticks) {
        if (ticks % 2 != 0) return;
        Map<UUID, Job> shown = new LinkedHashMap<>();
        for (Job job : edits.active.values()) {
            if (job.mode == Job.Mode.DONE) continue;
            Job current = shown.get(job.owner);
            // Prefer the newest job that is still running over one that is only stopping.
            if (current == null || current.cancelling || !job.cancelling) shown.put(job.owner, job);
        }
        for (Job job : shown.values()) {
            ServerPlayer player = owner(job);
            if (player == null) continue;
            int others = edits.ownJobs(job.owner).size() - 1;
            long seconds = (System.currentTimeMillis() - job.createdAt) / 1000;
            player.sendOverlayMessage(Component.literal(SPINNER[(int) (ticks / 4 % SPINNER.length)] + " ").withStyle(ChatFormatting.AQUA)
                    .append(Component.literal((others > 0 ? shortId(job.id) + " · " : "") + job.status)
                            .withStyle(job.cancelling ? ChatFormatting.YELLOW : ChatFormatting.WHITE))
                    .append(Component.literal(" · " + seconds + "s" + (others > 0 ? " · +" + others + " more" : ""))
                            .withStyle(ChatFormatting.GRAY)));
        }
    }
    /** The dense outline of a running job's region. */
    void jobOutline(Job job, long ticks) {
        ServerPlayer player = owner(job);
        if (player == null) return;
        if (ticks % 10 == 0 && !job.cancelling && player.level().dimension().equals(job.snapshot.dimension)) {
            ServerLevel level = job.level(server);
            job.snapshot.bounds.edges(config.maxParticleSegments, (x, y, z) -> level.sendParticles(player,
                    // 26.3 booleans are overrideLimiter and alwaysShow; both implement forced visibility.
                    new DustParticleOptions(0xFFFFFF, 0.9f), true, true, x, y, z, 1, 0, 0, 0, 0));
        }
    }
    /** The fainter outline of each player's current pos1/pos2 selection. */
    void selectionOutlines(long ticks, Map<UUID, Selection> selections) {
        if (ticks % 20 != 0) return;
        for (Map.Entry<UUID, Selection> entry : selections.entrySet()) {
            UUID owner = entry.getKey();
            ServerPlayer player = server.getPlayerList().getPlayer(owner);
            Selection selection = entry.getValue();
            if (player == null || !selection.complete() || !player.level().dimension().equals(selection.dimension())) continue;
            RegionBounds bounds;
            try {
                bounds = selection.bounds(config.maxSelectionBlocks);
            } catch (IllegalArgumentException | ArithmeticException tooLarge) {
                continue;
            }
            // Players can select their next region while jobs run; a job's own region keeps its denser outline.
            RegionBounds selected = bounds;
            if (edits.ownJobs(owner).stream().anyMatch(job -> job.snapshot.bounds.equals(selected)
                    && job.snapshot.dimension.equals(selection.dimension()))) continue;
            int segments = Math.max(1, config.maxParticleSegments / 3);
            ServerLevel level = player.level();
            bounds.edges(segments, (x, y, z) -> level.sendParticles(player,
                    new DustParticleOptions(0xFFFFFF, 0.4f), true, true, x, y, z, 1, 0, 0, 0, 0));
        }
    }

    // Sounds.

    void stageSound(Job job) {
        if (job.cancelling || job.mode == Job.Mode.DONE) return;
        String stage = switch (job.mode) {
            case CAPTURE, SUBMIT -> "capturing";
            case WAIT -> job.backendStatus;
            case RESULT, PALETTE -> "validating";
            case START -> "ready";
            case APPLY, FINISH -> "reconstructing";
            default -> "";
        };
        if (stage.isEmpty() || stage.equals(job.soundStage)) return;
        job.soundStage = stage;
        switch (stage) {
            case "capturing", "preparing" -> sound(job, SoundEvents.NOTE_BLOCK_HARP, 0.45f, 0.8f);
            case "queued" -> sound(job, SoundEvents.NOTE_BLOCK_HAT, 0.35f, 0.8f);
            case "planning" -> sound(job, SoundEvents.NOTE_BLOCK_PLING, 0.45f, 0.9f);
            case "checking", "validating" -> sound(job, SoundEvents.NOTE_BLOCK_XYLOPHONE, 0.4f, 1.0f);
            case "reviewing" -> sound(job, SoundEvents.NOTE_BLOCK_BELL, 0.4f, 0.85f);
            case "ready" -> sound(job, SoundEvents.NOTE_BLOCK_CHIME, 0.4f, 1.0f);
            case "reconstructing" -> sound(job, SoundEvents.NOTE_BLOCK_HARP, 0.45f, 1.2f);
            default -> { }
        }
    }
    void operationSound(Job job) {
        var center = OperationCenter.of(job.finalPlan.getAsJsonArray("operations").get(job.operation).getAsJsonObject(), job.snapshot.bounds);
        placementSound(job, center[0], center[1], center[2]);
    }
    /** An instant (zero-delay) replay plays one sound at the selection center, not one per operation. */
    void replaySound(Job job) {
        RegionBounds b = job.snapshot.bounds;
        placementSound(job, b.minX() + b.width() / 2.0, b.minY() + b.height() / 2.0, b.minZ() + b.length() / 2.0);
    }
    private void placementSound(Job job, double x, double y, double z) {
        job.level(server).playSound(null, x, y, z, SoundEvents.NOTE_BLOCK_BASEDRUM, SoundSource.BLOCKS, 2.0f, 1.5f);
    }
    void sound(Job job, Holder<SoundEvent> sound, float volume, float pitch) {
        sound(owner(job), sound, volume, pitch);
    }
    void sound(ServerPlayer player, Holder<SoundEvent> sound, float volume, float pitch) {
        if (player != null) player.connection.send(new ClientboundSoundPacket(sound, SoundSource.PLAYERS,
                player.getX(), player.getY(), player.getZ(), volume, pitch, player.level().getRandom().nextLong()));
    }
}
