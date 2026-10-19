package dev.worldprompt;

import dev.worldprompt.kernel.CoreLimits;
import dev.worldprompt.kernel.PromptRequest;
import dev.worldprompt.mycelium.world_replay.v1.model.ReplayTraversal;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class ReplayPayloadTest {
    private JsonObject payload(int... runs) {
        ByteBuffer bytes = ByteBuffer.allocate(runs.length * 4).order(ByteOrder.LITTLE_ENDIAN);
        int writes = 0;
        for (int i = 0; i < runs.length; i++) { bytes.putInt(runs[i]); if (i % 3 == 1) writes += runs[i]; }
        return new Gson().toJsonTree(Map.of("result_version", 2, "run_encoding", "base64-u32le",
                "dimensions", List.of(10000,1,1), "palette", List.of("minecraft:stone"), "operation_count", 1,
                "plan", Map.of("version", 1, "operations", List.of(Map.of("op", "fill", "min", List.of(0,0,0),
                        "max", List.of(9999,0,0), "block", "minecraft:stone"))),
                "operations", List.of(Map.of("index", 0, "op", "fill", "run_count", runs.length / 3,
                        "writes", writes, "dynamic_match", false, "runs_base64", Base64.getEncoder().encodeToString(bytes.array())))))
                .getAsJsonObject();
    }
    @Test void decodesMoreThanOneFormerPageAndKeepsAnIndependentPlan() {
        int[] runs = new int[15000];
        for (int i = 0; i < 5000; i++) { runs[i * 3] = i * 2; runs[i * 3 + 1] = 1; }
        JsonObject json = payload(runs);
        ReplayPayload result = ReplayPayload.parse(json);
        json.getAsJsonObject("plan").remove("operations");
        assertArrayEquals(runs, result.operations.getFirst().runs());
        assertEquals(1, result.plan.getAsJsonArray("operations").size());
        assertEquals(10000, result.width);
    }
    @Test void acceptsCylinderShapesAndRejectsUnknownOnes() {
        for (String op : List.of("solid_cylinder", "hollow_cylinder", "cone")) {
            JsonObject json = payload(0,1,0);
            json.getAsJsonObject("plan").getAsJsonArray("operations").get(0).getAsJsonObject().addProperty("op", op);
            json.getAsJsonArray("operations").get(0).getAsJsonObject().addProperty("op", op);
            if (op.equals("cone")) assertThrows(IllegalArgumentException.class, () -> ReplayPayload.parse(json));
            else assertEquals(1, ReplayPayload.parse(json).operations.size());
        }
    }
    @Test void emptyOperationsRemainRealSteps() {
        assertEquals(0, ReplayPayload.parse(payload()).operations.getFirst().runs().length);
    }
    @Test void retainedProgramProtectsInspectionDataAndMakesIndependentCursors() {
        ReplayPayload.Operation operation = ReplayPayload.parse(payload(2, 3, 0)).operations.getFirst();
        int[] inspection = operation.runs();
        inspection[0] = 99;
        inspection[1] = 0;

        ReplayTraversal first = operation.program().cursor();
        ReplayTraversal second = operation.program().cursor();
        List<Integer> firstVisits = new java.util.ArrayList<>();
        List<Integer> secondVisits = new java.util.ArrayList<>();
        first.drain(1, recording(firstVisits));
        second.drain(1, recording(secondVisits));
        first.drain(2, recording(firstVisits));
        assertEquals(List.of(2, 3, 4), firstVisits);
        assertTrue(first.complete());
        assertFalse(second.complete());
        second.drain(2, recording(secondVisits));
        assertEquals(List.of(2, 3, 4), secondVisits);
        assertTrue(second.complete());
    }

    private static ReplayTraversal.PlacementPort recording(List<Integer> visits) {
        return new ReplayTraversal.PlacementPort() {
            @Override public boolean skipRun(int paletteIndex) { return false; }
            @Override public void visit(int index, int paletteIndex) { visits.add(index); }
        };
    }
    @Test void rejectsOutOfBoundsNegativeOverlappingOrInvalidPaletteRuns() {
        for (int[] runs : new int[][]{{9999,2,0}, {-1,1,0}, {0,0,0}, {0,1,1}, {0,1,-1}, {3,2,0,4,1,0}})
            assertThrows(IllegalArgumentException.class, () -> ReplayPayload.parse(payload(runs)));
    }
    @Test void rejectsIncompleteLegacyOrInconsistentPayloads() {
        for (String field : List.of("result_version", "run_encoding", "operations", "palette", "plan")) {
            JsonObject json = payload(0,1,0); json.remove(field);
            assertThrows(IllegalArgumentException.class, () -> ReplayPayload.parse(json));
        }
        JsonObject json = payload(0,1,0); json.addProperty("operation_count", 2);
        assertThrows(IllegalArgumentException.class, () -> ReplayPayload.parse(json));
    }
    @Test void rejectsTruncationOrderMatchingCountsAndHugeDeclaredData() {
        Map<String, Object> mutations = Map.of("runs_base64", "AA==", "index", 1,
                "dynamic_match", true, "run_count", CoreLimits.MAX_REPLAY_BYTES / 12 + 1, "writes", 2);
        for (var entry : mutations.entrySet()) {
            JsonObject json = payload(0,1,0);
            json.getAsJsonArray("operations").get(0).getAsJsonObject().add(entry.getKey(), new Gson().toJsonTree(entry.getValue()));
            assertThrows(IllegalArgumentException.class, () -> ReplayPayload.parse(json));
        }
    }
    @Test void refusesFractionalOrStringIntegersAndInvalidLaterPlanCoordinates() {
        for (Object value : List.of(1.5, "1", -1, 513)) {
            JsonObject json = payload(0,1,0); json.add("operation_count", new Gson().toJsonTree(value));
            assertThrows(IllegalArgumentException.class, () -> ReplayPayload.parse(json));
        }
        JsonObject json = payload(0,1,0);
        json.getAsJsonObject("plan").getAsJsonArray("operations").get(0).getAsJsonObject()
                .getAsJsonArray("max").set(0, new Gson().toJsonTree(10000));
        assertThrows(IllegalArgumentException.class, () -> ReplayPayload.parse(json));
    }

    /** The payload Core's tests/test_protocol_fixture.py writes; the same bytes Core would send today. */
    private JsonObject goldenPayload() throws IOException {
        try (InputStream in = ReplayPayloadTest.class.getResourceAsStream("/complete-result.json")) {
            assertNotNull(in, "fabric/src/test/resources/complete-result.json is missing");
            return JsonParser.parseReader(new InputStreamReader(in, StandardCharsets.UTF_8)).getAsJsonObject();
        }
    }
    @Test void parsesTheGoldenCoreResult() throws IOException {
        ReplayPayload result = ReplayPayload.parse(goldenPayload());
        assertEquals(4, result.operations.size());
        assertEquals(6, result.width);
        assertEquals(3, result.height);
        assertEquals(4, result.length);
        assertEquals(List.of("minecraft:air", "minecraft:stone", "minecraft:oak_planks", "minecraft:glass", "minecraft:torch"),
                result.palette);
        // Op 1 chooses between planks per placement, so the later replace must match the live state.
        assertEquals(List.of(false, false, true, false), result.operations.stream().map(ReplayPayload.Operation::dynamicMatch).toList());
        assertArrayEquals(new int[]{0, 24, 1}, result.operations.get(0).runs());
        assertArrayEquals(new int[]{62, 1, 4}, result.operations.get(3).runs());
        assertTrue(result.plan.getAsJsonArray("operations").get(1).getAsJsonObject().get("block").isJsonArray());
    }
    @Test void goldenResultCarriesModelAndTokenUsage() throws IOException {
        JsonObject settings = ReplayPayload.parse(goldenPayload()).modelSettings;
        assertEquals("codex", settings.get("provider").getAsString());
        assertEquals("fixture-model", settings.get("model").getAsString());
        assertEquals("medium", settings.get("reasoning_effort").getAsString());
        assertFalse(settings.get("fast_mode").getAsBoolean());
        JsonObject usage = settings.getAsJsonObject("token_usage");
        assertEquals(2700, usage.get("input_tokens").getAsInt());
        assertEquals(550, usage.get("output_tokens").getAsInt());
        assertEquals(3250, usage.get("total_tokens").getAsInt());
        assertEquals(2, usage.get("model_calls").getAsInt());
        assertTrue(usage.get("complete").getAsBoolean());
    }
    @Test void doublePassBookHasDraftAndReviewSections() throws IOException {
        ReplayPayload result = ReplayPayload.parse(goldenPayload());
        assertEquals("Stone slab with a plank layer", result.draftSummary);
        String[] sections = SummaryBook.sections("0123456789abcdef", new PromptRequest("slab", "high"), result);
        assertEquals(3, sections.length);
        assertTrue(sections[0].contains("slab") && sections[0].contains("Double pass"), sections[0]);
        assertEquals("Draft\n\nStone slab with a plank layer", sections[1]);
        assertEquals("Review (built)\n\nStone slab with a mixed plank layer, glassed over and torch-lit", sections[2]);
    }
    @Test void singlePassBookHasOneSummarySection() {
        ReplayPayload result = ReplayPayload.parse(payload(0, 10000, 0));
        assertNull(result.draftSummary);
        String[] sections = SummaryBook.sections("0123456789abcdef", new PromptRequest("wall", "normal"), result);
        assertEquals(2, sections.length);
        assertEquals("The model returned no summary.", sections[1]);
    }
}
