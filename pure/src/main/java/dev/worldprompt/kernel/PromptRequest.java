package dev.worldprompt.kernel;

public record PromptRequest(String prompt, String effort) {
    public static PromptRequest parse(String text, String effort) {
        String prompt = text.strip();
        // Core's protocol and saved jobs keep the earlier normal/high values.
        effort = switch (effort) {
            case "single" -> "normal";
            case "double" -> "high";
            default -> throw new IllegalArgumentException("Workflow must be single or double.");
        };
        if (prompt.isBlank() || prompt.codePointCount(0, prompt.length()) > CoreLimits.MAX_PROMPT_CHARACTERS)
            throw new IllegalArgumentException("Prompt must contain 1–" + CoreLimits.MAX_PROMPT_CHARACTERS + " characters.");
        return new PromptRequest(prompt, effort);
    }
}
