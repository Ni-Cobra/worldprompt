package dev.worldprompt;

import dev.worldprompt.kernel.CoreLimits;
import dev.worldprompt.mycelium.world_replay.v1.model.ReplayProgram;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.Set;

/** Complete, validated placement data retained for this Minecraft server session. */
final class ReplayPayload {
    private static final Set<String> SHAPES = Set.of("fill", "single", "replace", "hollow_box",
            "solid_ellipsoid", "hollow_ellipsoid", "solid_cylinder", "hollow_cylinder", "line");
    final JsonObject plan;
    final List<String> palette;
    final int width, height, length;
    final List<Operation> operations;
    final JsonObject modelSettings;
    /** The Double pass draft plan's summary; null for Single pass. */
    final String draftSummary;

    private ReplayPayload(JsonObject result, List<Operation> operations, List<String> palette, int[] dimensions) {
        this.plan = result.getAsJsonObject("plan").deepCopy();
        JsonElement draft = result.get("draft_plan_summary");
        draftSummary = draft != null && draft.isJsonPrimitive() ? draft.getAsString().strip() : null;
        this.palette = List.copyOf(palette);
        this.operations = List.copyOf(operations);
        width = dimensions[0]; height = dimensions[1]; length = dimensions[2];
        modelSettings = new JsonObject();
        for (String key : List.of("provider", "model", "reasoning_effort", "fast_mode", "token_usage"))
            if (result.has(key)) modelSettings.add(key, result.get(key).deepCopy());
    }

    static ReplayPayload parse(JsonObject result) {
        try {
            if (!result.has("result_version") || integer(result.get("result_version")) != 2 || !"base64-u32le".equals(result.get("run_encoding").getAsString()))
                throw new IllegalArgumentException("Update Core and the mod together: a complete result payload is required.");
            JsonArray dims = result.getAsJsonArray("dimensions");
            if (dims.size() != 3) throw new IllegalArgumentException("Invalid result dimensions.");
            int[] dimensions = new int[3];
            long volume = 1;
            for (int i = 0; i < 3; i++) {
                dimensions[i] = integer(dims.get(i));
                if (dimensions[i] < 1 || dimensions[i] > CoreLimits.MAX_AXIS) throw new IllegalArgumentException("Invalid result dimensions.");
                volume *= dimensions[i];
            }
            if (volume > CoreLimits.MAX_BLOCKS) throw new IllegalArgumentException("Result selection is too large.");
            List<String> palette = strings(result.getAsJsonArray("palette"), CoreLimits.MAX_CAPTURE_PALETTE + CoreLimits.MAX_OPERATIONS * CoreLimits.MAX_BLOCK_CHOICES);
            JsonArray plan = result.getAsJsonObject("plan").getAsJsonArray("operations");
            JsonArray packed = result.getAsJsonArray("operations");
            int count = integer(result.get("operation_count"));
            if (count < 0 || count > CoreLimits.MAX_OPERATIONS || count != plan.size() || count != packed.size())
                throw new IllegalArgumentException("Inconsistent result operation counts.");
            List<Operation> operations = new ArrayList<>();
            long totalBytes = 0, totalWrites = 0;
            boolean randomized = false;
            for (int i = 0; i < count; i++) {
                JsonObject definition = plan.get(i).getAsJsonObject();
                String shape = definition.get("op").getAsString();
                if (!SHAPES.contains(shape)) throw new IllegalArgumentException("Unknown plan operation.");
                JsonElement block = definition.get("block");
                if (block.isJsonArray()) strings(block.getAsJsonArray(), CoreLimits.MAX_BLOCK_CHOICES);
                else string(block);
                if (shape.equals("replace")) {
                    strings(definition.getAsJsonArray("match"), CoreLimits.MAX_BLOCK_CHOICES);
                    bool(definition.get("preserve_properties"));
                }
                String[] vectors = shape.equals("single") ? new String[]{"at"}
                        : shape.equals("line") ? new String[]{"start", "end"} : new String[]{"min", "max"};
                for (String vector : vectors) {
                    JsonArray coordinates = definition.getAsJsonArray(vector);
                    if (coordinates.size() != 3) throw new IllegalArgumentException("Invalid operation coordinates.");
                    for (int axis = 0; axis < 3; axis++) {
                        int n = integer(coordinates.get(axis));
                        if (n < 0 || n >= dimensions[axis]) throw new IllegalArgumentException("Operation outside selection.");
                    }
                }
                JsonObject encoded = packed.get(i).getAsJsonObject();
                int runCount = integer(encoded.get("run_count"));
                long bytes = (long) runCount * 12;
                totalBytes += bytes;
                if (runCount < 0 || totalBytes > CoreLimits.MAX_REPLAY_BYTES)
                    throw new IllegalArgumentException("Complete replay exceeds " + (CoreLimits.MAX_REPLAY_BYTES >> 20) + " MiB.");
                if (integer(encoded.get("index")) != i || !shape.equals(encoded.get("op").getAsString()))
                    throw new IllegalArgumentException("Operation order does not match the plan.");
                boolean dynamic = shape.equals("replace") && randomized;
                if (bool(encoded.get("dynamic_match")) != dynamic) throw new IllegalArgumentException("Operation matching mode changed.");
                String base64 = string(encoded.get("runs_base64"));
                if (base64.length() != bytes / 3 * 4) throw new IllegalArgumentException("Invalid placement data length.");
                byte[] raw = Base64.getDecoder().decode(base64);
                if (raw.length != bytes) throw new IllegalArgumentException("Truncated placement data.");
                int[] runs = new int[runCount * 3];
                ByteBuffer.wrap(raw).order(ByteOrder.LITTLE_ENDIAN).asIntBuffer().get(runs);
                ReplayProgram program = new ReplayProgram(runs, (int) volume, palette.size());
                long writes = 0;
                for (int j = 1; j < runs.length; j += 3) writes += runs[j];
                totalWrites += writes;
                if (writes != integer(encoded.get("writes")) || totalWrites > CoreLimits.MAX_WORK)
                    throw new IllegalArgumentException("Invalid result placement count.");
                operations.add(new Operation(program, dynamic));
                randomized |= block.isJsonArray();
            }
            return new ReplayPayload(result, operations, palette, dimensions);
        } catch (NullPointerException | IllegalStateException | ArithmeticException error) {
            throw new IllegalArgumentException("Malformed complete result payload.", error);
        }
    }
    private static int integer(JsonElement value) {
        if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isNumber())
            throw new IllegalArgumentException("Expected an integer in result payload.");
        return value.getAsBigDecimal().intValueExact();
    }
    private static boolean bool(JsonElement value) {
        if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isBoolean())
            throw new IllegalArgumentException("Expected a boolean in result payload.");
        return value.getAsBoolean();
    }
    private static String string(JsonElement value) {
        if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isString())
            throw new IllegalArgumentException("Expected text in result payload.");
        return value.getAsString();
    }
    private static List<String> strings(JsonArray values, int maximum) {
        if (values.isEmpty() || values.size() > maximum) throw new IllegalArgumentException("Invalid block list length.");
        List<String> result = new ArrayList<>(values.size());
        for (JsonElement value : values) result.add(string(value));
        return result;
    }
    record Operation(ReplayProgram program, boolean dynamicMatch) {
        /** Defensive view retained for tests and package-local diagnostics; replay uses {@link #program()}. */
        int[] runs() { return program.copyRuns(); }
    }
}
