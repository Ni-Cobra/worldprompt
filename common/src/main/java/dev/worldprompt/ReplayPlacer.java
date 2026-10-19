package dev.worldprompt;

import dev.worldprompt.kernel.CoreLimits;
import dev.worldprompt.mycelium.world_replay.v1.model.ReplayTraversal;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.mojang.brigadier.exceptions.CommandSyntaxException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import net.minecraft.commands.arguments.blocks.BlockStateParser;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.Registries;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.Property;

/**
 * Turns a job's Core result into world writes on the server tick thread: parses the palette and each
 * operation's block choices and match rules, applies one operation's placement runs per step, and
 * restores snapshots. It never talks to Core; {@link CoreJobDriver} decides when a step may run.
 */
final class ReplayPlacer {
    private final MinecraftServer server;
    private final AdapterConfig config;
    private final EditHistory edits;
    private final Feedback feedback;

    ReplayPlacer(MinecraftServer server, AdapterConfig config, EditHistory edits, Feedback feedback) {
        this.server = server; this.config = config; this.edits = edits; this.feedback = feedback;
    }

    record PlacementChoice(BlockState state, Set<String> explicitProperties) { }
    record MatchRule(String base, Map<String, String> properties) { }

    /** Write the snapshot back over its region; every chunk is checked first, so a restore never stops halfway. */
    void restore(RegionSnapshot snapshot, AppliedEdit edit) {
        ServerLevel level = server.getLevel(snapshot.dimension);
        if (level == null) throw new IllegalStateException("The snapshot dimension is unavailable.");
        snapshot.ensureLoaded(level);
        if (edit != null) edit.restored = false;
        snapshot.restore(level);
        if (edit != null) edit.restored = true;
    }

    /** Parse up to 256 more palette entries; an unsupported state becomes null (its placements are skipped). */
    void parsePalette(Job job) {
        int stop = Math.min(job.replay.palette.size(), job.palette.size() + 256);
        for (; job.palette.size() < stop;) {
            String state = job.replay.palette.get(job.palette.size());
            String compatible = compatibleState(state);
            try {
                job.palette.add(BlockStateParser.parseForBlock(job.level(server).registryAccess().lookupOrThrow(Registries.BLOCK), compatible, false).blockState());
                if (!state.equals(compatible)) feedback.blockWarning(job, state, "Using " + compatible + " for legacy block " + state);
            } catch (CommandSyntaxException e) {
                // Keep palette indexes stable; null means leave the live block untouched.
                job.palette.add(null);
                feedback.blockWarning(job, state, "Unsupported block state " + state + "; its placements will be skipped");
            }
        }
    }

    /**
     * Place up to {@code budget} blocks of the current operation. Finishing the last operation moves the
     * job to FINISH. {@code ticks} is the manager's tick counter, which the per-operation delay counts against.
     */
    void step(Job job, long ticks, long budget) {
        boolean instant = job.delayTicks == 0;
        if (job.operation == job.operationCount) {
            if (instant && job.operationCount > 0) feedback.replaySound(job);
            job.mode = Job.Mode.FINISH; job.status = "Finishing up"; return;
        }
        if (!instant && ticks < job.nextOperationTick) return;
        if (job.choiceOperation != job.operation) {
            job.activeChoices = choices(job);
            job.choiceOperation = job.operation;
        }
        if (job.cursor == null) {
            job.cursor = job.replay.operations.get(job.operation).program().cursor();
        }
        ServerLevel level = job.level(server);
        long skippedBefore = job.cursor.skippedRunPositions();
        try {
            job.cursor.drain(budget, new ReplayTraversal.PlacementPort() {
                @Override
                public boolean skipRun(int paletteIndex) {
                    return job.activeChoices.isEmpty() && job.palette.get(paletteIndex) == null;
                }

                @Override
                public void visit(int index, int paletteIndex) {
                    BlockPos pos = job.snapshot.position(index);
                    if (!level.hasChunkAt(pos))
                        throw new IllegalStateException("Reconstruction region unloaded. Load it and use /wp undo to recover.");
                    if (job.dynamicMatch && !matches(level.getBlockState(pos), job.matchRules)) return;

                    BlockState selected = job.activeChoices.isEmpty() ? job.palette.get(paletteIndex) : null;
                    if (!job.activeChoices.isEmpty()) {
                        PlacementChoice choice = job.activeChoices.get(level.getRandom().nextInt(job.activeChoices.size()));
                        if (choice != null) {
                            selected = choice.state();
                            if (job.preserveChoiceProperties)
                                selected = inheritProperties(level.getBlockState(pos), selected, choice.explicitProperties());
                        }
                    }
                    if (selected == null) {
                        job.skippedWrites++;
                        return;
                    }
                    edits.markApplied(job);
                    try {
                        boolean changed = level.setBlock(pos, selected, RegionSnapshot.PLACE_FLAGS);
                        if (!changed && !level.getBlockState(pos).equals(selected)) {
                            job.skippedWrites++;
                            feedback.blockWarning(job, selected.toString(), "Minecraft refused " + selected + " at " + pos.toShortString() + "; continuing");
                        }
                    } catch (IllegalArgumentException invalidState) {
                        job.skippedWrites++;
                        feedback.blockWarning(job, selected.toString(), "Minecraft rejected " + selected + " at " + pos.toShortString() + "; continuing");
                    }
                }
            });
        } finally {
            job.skippedWrites += job.cursor.skippedRunPositions() - skippedBefore;
        }
        if (job.cursor.complete()) {
            job.cursor = null;
            // With a delay, exactly one completion sound and delay per original operation,
            // including empty ones; an instant replay plays one sound when it finishes.
            if (!instant) feedback.operationSound(job);
            job.operation++;
            job.status = "Building · " + job.operation + "/" + job.operationCount;
            job.nextOperationTick = ticks + job.delayTicks;
        }
    }

    /**
     * Parse the current operation's block choices and, for a replace that follows a randomized operation,
     * its match rules. Whether matching is dynamic is Core's flag, validated once by {@link ReplayPayload}.
     */
    private List<PlacementChoice> choices(Job job) {
        JsonObject operation = job.finalPlan.getAsJsonArray("operations").get(job.operation).getAsJsonObject();
        boolean replace = operation.get("op").getAsString().equals("replace");
        job.preserveChoiceProperties = replace && operation.get("preserve_properties").getAsBoolean();
        job.dynamicMatch = job.replay.operations.get(job.operation).dynamicMatch();
        job.matchRules = List.of();
        if (job.dynamicMatch) {
            List<MatchRule> rules = new ArrayList<>();
            for (JsonElement match : operation.getAsJsonArray("match")) {
                MatchRule parsed = rule(job, match.getAsString());
                if (parsed != null) rules.add(parsed);
            }
            job.matchRules = rules;
        }
        JsonElement block = operation.get("block");
        if (!block.isJsonArray() && !job.dynamicMatch) return List.of();
        JsonArray options = block.isJsonArray() ? block.getAsJsonArray() : new JsonArray();
        if (!block.isJsonArray()) options.add(block.getAsString());
        if (options.isEmpty() || options.size() > CoreLimits.MAX_BLOCK_CHOICES) throw new IllegalArgumentException("Invalid block choice count.");
        List<PlacementChoice> parsed = new ArrayList<>(options.size());
        for (JsonElement option : options) {
            String text = option.getAsString();
            String compatible = compatibleState(text);
            try {
                BlockState state = BlockStateParser.parseForBlock(job.level(server).registryAccess().lookupOrThrow(Registries.BLOCK), compatible, false).blockState();
                if (!text.equals(compatible)) feedback.blockWarning(job, text, "Using " + compatible + " for legacy block " + text);
                Set<String> explicit = new HashSet<>();
                int bracket = text.indexOf('[');
                if (bracket >= 0) explicit.addAll(properties(text.substring(bracket + 1, text.length() - 1)).keySet());
                parsed.add(new PlacementChoice(state, explicit));
            } catch (CommandSyntaxException e) {
                parsed.add(null);
                feedback.blockWarning(job, text, "Unsupported block state " + text + "; its random placements will be skipped");
            }
        }
        return parsed;
    }
    private static String compatibleState(String text) {
        return text.equals("minecraft:chain") || text.startsWith("minecraft:chain[")
                ? "minecraft:iron_chain" + text.substring("minecraft:chain".length()) : text;
    }
    private MatchRule rule(Job job, String text) {
        String compatible = compatibleState(text);
        if (compatible.indexOf('[') >= 0) {
            try {
                // A bracketed match is one complete state; omitted properties use
                // native defaults, just as they do for a placed block. The live
                // catalog gives Core these defaults for modded blocks as well.
                compatible = BlockStateParser.serialize(BlockStateParser.parseForBlock(
                        job.level(server).registryAccess().lookupOrThrow(Registries.BLOCK), compatible, false).blockState());
            } catch (CommandSyntaxException error) {
                feedback.blockWarning(job, "match:" + text, "Unsupported match state " + text + "; its matches will be skipped");
                return null;
            }
        }
        text = compatible;
        int bracket = text.indexOf('[');
        return new MatchRule(bracket < 0 ? text : text.substring(0, bracket),
                bracket < 0 ? null : properties(text.substring(bracket + 1, text.length() - 1)));
    }
    private static Map<String, String> properties(String text) {
        Map<String, String> values = new HashMap<>();
        if (!text.isEmpty()) for (String part : text.split(",")) {
            int separator = part.indexOf('=');
            if (separator > 0) values.put(part.substring(0, separator), part.substring(separator + 1));
        }
        return values;
    }
    private static boolean matches(BlockState state, List<MatchRule> rules) {
        String current = BlockStateParser.serialize(state);
        int bracket = current.indexOf('[');
        String base = bracket < 0 ? current : current.substring(0, bracket);
        Map<String, String> values = null;
        for (MatchRule rule : rules) {
            if (!rule.base().equals(base)) continue;
            if (rule.properties() == null) return true;
            if (values == null) values = bracket < 0 ? Map.of() : properties(current.substring(bracket + 1, current.length() - 1));
            if (values.equals(rule.properties())) return true;
        }
        return false;
    }
    static BlockState inheritProperties(BlockState source, BlockState target, Set<String> explicit) {
        for (Property<?> property : source.getProperties()) {
            if (explicit.contains(property.getName())) continue;
            Property<?> supported = target.getBlock().getStateDefinition().getProperty(property.getName());
            if (supported != null)
                target = inheritProperty(target, supported, propertyName(source, property));
        }
        return target;
    }
    private static <T extends Comparable<T>> String propertyName(BlockState state, Property<T> property) {
        return property.getName(state.getValue(property));
    }
    private static <T extends Comparable<T>> BlockState inheritProperty(BlockState state, Property<T> property, String name) {
        return property.getValue(name).map(value -> state.setValue(property, value)).orElse(state);
    }
}
