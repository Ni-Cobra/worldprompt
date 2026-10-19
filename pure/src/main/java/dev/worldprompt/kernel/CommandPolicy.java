package dev.worldprompt.kernel;

import java.util.Map;
import java.util.TreeMap;

/** The original command defaults, declared once for both invocation checks and native node registration. */
public final class CommandPolicy {
    public static final String PERMISSION_PREFIX = "worldprompt.command.";
    private static final Map<String, Boolean> OPERATOR_DEFAULTS = Map.ofEntries(
            Map.entry("pos1", false), Map.entry("pos2", false), Map.entry("select", false),
            Map.entry("prompt", true), Map.entry("info", false), Map.entry("delay", false),
            Map.entry("undo", true), Map.entry("cancel", false), Map.entry("rebuild", true),
            Map.entry("book", false), Map.entry("jobs", false));

    private CommandPolicy() {}

    public static boolean operatorDefault(String command) {
        Boolean policy = OPERATOR_DEFAULTS.get(command);
        if (policy == null) throw new IllegalArgumentException("Unknown WorldPrompt command: " + command);
        return policy;
    }

    public static Map<String, Boolean> permissionDefaults() {
        Map<String, Boolean> nodes = new TreeMap<>();
        OPERATOR_DEFAULTS.forEach((command, operator) -> nodes.put(PERMISSION_PREFIX + command, operator));
        return Map.copyOf(nodes);
    }
}
