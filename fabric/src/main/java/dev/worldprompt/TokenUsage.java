package dev.worldprompt;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.util.Locale;

/** Report only accounting supplied by the core; inference progress is not an estimate. */
record TokenUsage(Long totalTokens, boolean complete) {
    static final TokenUsage UNKNOWN = new TokenUsage(null, false);

    static TokenUsage from(JsonObject remote) {
        if (remote == null || !remote.has("token_usage") || !remote.get("token_usage").isJsonObject()) return UNKNOWN;
        JsonObject usage = remote.getAsJsonObject("token_usage");
        JsonElement total = usage.get("total_tokens");
        if (total == null || total.isJsonNull() || !total.isJsonPrimitive() || !total.getAsJsonPrimitive().isNumber()) return UNKNOWN;
        try {
            long count = total.getAsBigDecimal().longValueExact();
            if (count < 0) return UNKNOWN;
            boolean complete = usage.has("complete") && usage.get("complete").isJsonPrimitive()
                    && usage.getAsJsonPrimitive("complete").isBoolean() && usage.get("complete").getAsBoolean();
            return new TokenUsage(count, complete);
        } catch (ArithmeticException | NumberFormatException ignored) { return UNKNOWN; }
    }

    String suffix() {
        return totalTokens == null || totalTokens == 0 ? "" : " · " + String.format(Locale.ROOT, "%,d", totalTokens)
                + (complete ? "" : "+") + " tokens";
    }
}
