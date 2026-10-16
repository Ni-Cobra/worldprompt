package dev.worldprompt;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.JsonParser;
import org.junit.jupiter.api.Test;

class TokenUsageTest {
    private TokenUsage usage(String value) {
        return TokenUsage.from(JsonParser.parseString(value).getAsJsonObject());
    }
    @Test void missingUsageStaysUnknownAndDoesNotDisplayZero() {
        assertEquals("", usage("{}").suffix());
        assertEquals("", usage("{\"token_usage\":null}").suffix());
        assertEquals("", usage("{\"token_usage\":{\"total_tokens\":null,\"complete\":false}}").suffix());
    }
    @Test void knownPartialAndFinalUsageAreDistinctWithoutDoubleCountingFields() {
        assertEquals(" · 1,234+ tokens", usage("{\"token_usage\":{\"total_tokens\":1234,\"complete\":false,\"input_tokens\":1000,\"output_tokens\":234}}").suffix());
        assertEquals(" · 2,500 tokens", usage("{\"token_usage\":{\"total_tokens\":2500,\"complete\":true}}").suffix());
        assertEquals("", usage("{\"token_usage\":{\"total_tokens\":0,\"complete\":true}}").suffix());
    }
    @Test void malformedCountsNeverInventUsage() {
        for (String count : new String[]{"-1", "1.5", "1e100", "\"123\"", "true", "[]"})
            assertEquals("", usage("{\"token_usage\":{\"total_tokens\":" + count + "}}").suffix());
    }
}
