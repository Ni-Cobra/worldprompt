package dev.worldprompt;

import static org.junit.jupiter.api.Assertions.*;
import dev.worldprompt.kernel.PromptRequest;
import org.junit.jupiter.api.Test;

class PromptRequestTest {
    @Test void explicitDoublePassIsSeparateFromPrompt() {
        assertEquals(new PromptRequest("Build a dock", "high"), PromptRequest.parse(" Build a dock ", "double"));
    }
    @Test void singlePassAndEmbeddedFlagsRemainPromptText() {
        assertEquals(new PromptRequest("Build a dock", "normal"), PromptRequest.parse("Build a dock", "single"));
        assertEquals(new PromptRequest("Explain --double-pass syntax", "normal"), PromptRequest.parse("Explain --double-pass syntax", "single"));
        assertEquals("normal", PromptRequest.parse("stone--double-pass", "single").effort());
        assertEquals(new PromptRequest("Build a dock --double-pass", "normal"), PromptRequest.parse("Build a dock --double-pass", "single"));
    }
    @Test void commandModesPreserveProtocolCompatibility() {
        assertEquals("normal", PromptRequest.parse("Build", "single").effort());
        assertEquals("high", PromptRequest.parse("Build", "double").effort());
    }
    @Test void legacyNormalAndHighCommandNamesAreRejected() {
        assertThrows(IllegalArgumentException.class, () -> PromptRequest.parse("Build", "normal"));
        assertThrows(IllegalArgumentException.class, () -> PromptRequest.parse("Build", "high"));
    }
    @Test void emptyOrInvalidEffortIsRejected() {
        assertThrows(IllegalArgumentException.class, () -> PromptRequest.parse("Build", "unknown"));
        assertThrows(IllegalArgumentException.class, () -> PromptRequest.parse("  ", "single"));
    }
    @Test void characterLimitCountsUnicodeCodePointsLikeCore() {
        String prompt = "\uD83C\uDF32".repeat(16000);
        assertEquals(prompt, PromptRequest.parse(prompt, "single").prompt());
        assertThrows(IllegalArgumentException.class, () -> PromptRequest.parse(prompt + "x", "single"));
    }
}
