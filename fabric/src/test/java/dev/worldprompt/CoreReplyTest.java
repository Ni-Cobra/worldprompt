package dev.worldprompt;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;

/** Replies the mod cannot read must be detected so the player is told, not just the log. */
class CoreReplyTest {
    private static JsonObject json(String text) { return JsonParser.parseString(text).getAsJsonObject(); }

    @Test void infoLinesDescribeAWellFormedReply() {
        List<String> lines = PromptManager.infoLines(json("{\"version\":\"0.10.5\",\"provider\":\"codex\",\"model\":\"m\","
                + "\"active_jobs\":1,\"world_max_jobs_per_player\":3,\"timeout_seconds\":600}"), 2_000_000);
        assertEquals(5, lines.size());
        assertEquals("WorldPrompt Core 0.10.5 · mod " + Versions.MOD + " · connected", lines.getFirst());
        assertTrue(lines.get(2).contains("per player 3"), lines.get(2));
        assertTrue(lines.get(3).startsWith("Timeouts: Single pass 600s"), lines.get(3));
        List<String> unlimited = PromptManager.infoLines(json("{\"job_limits\":false,\"world_concurrency\":1000000,"
                + "\"timeout_seconds\":31536000}"), 1);
        assertTrue(unlimited.get(2).endsWith("queued · no job limits"), unlimited.get(2));
        assertEquals("Timeouts: none", unlimited.get(3));
    }
    @Test void infoLinesRejectMalformedFields() {
        assertNull(PromptManager.infoLines(json("{\"version\":{\"nested\":true}}"), 1));
        assertNull(PromptManager.infoLines(json("{\"provider\":[1,2]}"), 1));
    }
    @Test void jobListIsAllOrNothing() {
        UUID viewer = UUID.randomUUID();
        List<String> mine = new ArrayList<>(), others = new ArrayList<>();
        assertTrue(PromptManager.collectRemoteJobs(json("{\"jobs\":[{\"id\":\"abcdef0123\",\"owner_id\":\"" + viewer
                + "\",\"owner_name\":\"Ann\",\"effort\":\"high\",\"status\":\"queued\"},"
                + "{\"id\":\"99999999zz\",\"owner_id\":\"someone\",\"status\":\"planning\"}]}"),
                viewer, new LinkedHashMap<>(), mine, others));
        assertEquals(1, mine.size()); assertEquals(1, others.size());
        assertTrue(mine.getFirst().startsWith("abcdef01 · Ann · Double pass"), mine.getFirst());

        List<String> none = new ArrayList<>(), noneOthers = new ArrayList<>();
        for (String bad : List.of("{}", "{\"jobs\":5}", "{\"jobs\":[1]}",
                "{\"jobs\":[{\"id\":\"a\",\"owner_id\":\"" + viewer + "\"},{\"id\":{}}]}")) {
            assertFalse(PromptManager.collectRemoteJobs(json(bad), viewer, new LinkedHashMap<>(), none, noneOthers), bad);
            assertTrue(none.isEmpty() && noneOthers.isEmpty(), "A malformed reply adds nothing: " + bad);
        }
    }
}
