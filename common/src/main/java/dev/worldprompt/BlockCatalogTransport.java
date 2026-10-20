package dev.worldprompt;

import com.google.gson.Gson;
import dev.worldprompt.kernel.CoreLimits;
import dev.worldprompt.mycelium.block_catalog.v2.model.BlockCatalog;
import dev.worldprompt.mycelium.block_catalog.v2.model.CatalogSource;
import dev.worldprompt.mycelium.bridges.block_catalog.MinecraftBlockCatalog;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Pattern;

/**
 * WorldPrompt's bounded wire representation of Mycelium's immutable native catalog.
 *
 * <p>A registry entry Core's protocol cannot represent is left out of the snapshot, never fatal: one
 * unusual block from one mod must not take the whole catalog, or the whole mod, down with it. The
 * exclusions are reported so they can be logged once, with a bound on how much is logged.
 *
 * <p>Property value order is the native registry's own ({@code block.catalog@2} law C6) and is carried
 * through unchanged. Block identifiers and property names stay sorted, so the wire bytes a job is
 * fingerprinted by are deterministic.
 */
final class BlockCatalogTransport {
    /** How many excluded block identifiers a warning names before it only counts them. */
    private static final int WARNED_EXCLUSIONS = 8;
    /** Compiled once: a capture tests this against every property value of every block. */
    private static final Pattern VALUE_SYNTAX = Pattern.compile("[a-z0-9_]+");
    /** How much of a rejected snapshot's own diagnostic is repeated in the log. */
    private static final int REASON_CHARS = 300;

    private BlockCatalogTransport() { }

    /**
     * The snapshot to send, or no catalog at all when this server has none Core can use.
     *
     * @param transport the wire object, or {@code null} to send no catalog at all
     * @param excluded  the identifiers of blocks left out, capped for logging
     * @param excludedCount how many blocks were left out in total
     * @param unusable  why the whole snapshot was abandoned, or {@code null}
     */
    record Capture(Map<String, Object> transport, List<String> excluded, int excludedCount, String unusable) {
        boolean live() {
            return transport != null;
        }

        /** What was left out, bounded, or {@code null} when the whole snapshot came through. */
        String exclusionWarning() {
            if (excludedCount == 0) return null;
            return excludedCount + " block(s) are not representable in WorldPrompt's block-state protocol and were"
                    + " left out of the catalog sent to Core: " + String.join(", ", excluded)
                    + (excludedCount > excluded.size() ? ", and " + (excludedCount - excluded.size()) + " more" : "")
                    + ". Those blocks cannot be planned or validated; everything else works normally.";
        }

        /** Why no catalog is being sent at all, or {@code null} when one is. */
        String unusableError() {
            if (unusable == null) return null;
            return "This server's block catalog cannot be sent to Core: " + unusable
                    + " Jobs will plan against Core's bundled vanilla catalog, so this server's modded blocks are"
                    + " unavailable until this is resolved.";
        }
    }

    private static Capture none(String reason) {
        return new Capture(null, List.of(), 0, reason);
    }

    /**
     * Called once per server host, after native registries have been initialized, never by the HTTP thread.
     *
     * <p>The contract validates a snapshot strictly and rejects a malformed definition outright, which
     * is right for the contract but must not be fatal here: a mod that names a property or value the
     * model refuses would otherwise take the whole catalog, and with it every {@code /wp} command.
     * WorldPrompt falls back to no catalog instead, and says so.
     */
    static Capture capture() {
        return capture(new MinecraftBlockCatalog());
    }

    /** The seam the test uses: a source whose snapshot the contract refuses must not be fatal. */
    static Capture capture(CatalogSource source) {
        BlockCatalog catalog;
        try {
            catalog = source.capture();
        } catch (RuntimeException rejected) {
            return none("this server's registry could not be read as a valid snapshot ("
                    + rejected.getClass().getSimpleName() + ": " + clipped(rejected.getMessage()) + ").");
        }
        return encode(catalog);
    }

    static Capture encode(BlockCatalog catalog) {
        if (!boundedText(catalog.version(), CoreLimits.MAX_CATALOG_VERSION_CHARS)
                || catalog.version().codePoints().anyMatch(Character::isISOControl))
            return none("its Minecraft version string is not one Core accepts.");

        Map<String, Object> blocks = new TreeMap<>();
        List<String> excluded = new ArrayList<>();
        int excludedCount = 0;
        long propertyCount = 0, valueCount = 0;
        for (var block : catalog.blocks().entrySet()) {
            Map<String, List<String>> properties = representable(block.getKey(), block.getValue());
            if (properties == null) {
                excludedCount++;
                if (excluded.size() < WARNED_EXCLUSIONS) excluded.add(block.getKey());
                continue;
            }
            // A global overflow is not something per-block isolation can fix, and truncating by
            // identifier order would silently drop whole namespaces. Send no catalog instead.
            propertyCount += properties.size();
            for (List<String> domain : properties.values()) valueCount += domain.size();
            if (blocks.size() + 1 > CoreLimits.MAX_CATALOG_BLOCKS
                    || propertyCount > CoreLimits.MAX_CATALOG_PROPERTIES
                    || valueCount > CoreLimits.MAX_CATALOG_VALUES)
                return none("it is larger than Core's catalog limits even after unusable blocks were left out.");
            blocks.put(block.getKey(), Map.of("properties", Collections.unmodifiableMap(properties),
                    "defaults", Collections.unmodifiableMap(new TreeMap<>(block.getValue().defaults()))));
        }
        if (blocks.isEmpty())
            return none(catalog.blocks().isEmpty()
                    ? "this server reported no registered blocks at all."
                    : "no block in it can be represented in WorldPrompt's block-state protocol.");

        Map<String, Object> transport = Map.of("minecraft_version", catalog.version(),
                "blocks", Collections.unmodifiableMap(blocks));
        if (new Gson().toJson(transport).getBytes(StandardCharsets.UTF_8).length > CoreLimits.MAX_CATALOG_BYTES)
            return none("it is larger than Core's catalog byte limit even after unusable blocks were left out.");
        return new Capture(transport, List.copyOf(excluded), excludedCount, null);
    }

    /**
     * This block's property domains in native order, or {@code null} when Core's protocol cannot carry
     * the block. A single unrepresentable property excludes the whole block: sending it without that
     * property would understate its state space, and Core would then validate a state that cannot exist.
     */
    private static Map<String, List<String>> representable(String id, BlockCatalog.Entry entry) {
        if (!boundedText(id, CoreLimits.MAX_STATE_CHARS)
                || entry.properties().size() > CoreLimits.MAX_CATALOG_PROPERTIES_PER_BLOCK)
            return null;
        Map<String, List<String>> properties = new TreeMap<>();
        for (var property : entry.properties().entrySet()) {
            if (!boundedText(property.getKey(), CoreLimits.MAX_STATE_CHARS)
                    || property.getValue().size() > CoreLimits.MAX_CATALOG_VALUES_PER_PROPERTY)
                return null;
            for (String value : property.getValue())
                if (!boundedText(value, CoreLimits.MAX_STATE_CHARS) || !VALUE_SYNTAX.matcher(value).matches())
                    return null;
            // The contract's order, kept: see block.catalog@2 law C6.
            properties.put(property.getKey(), List.copyOf(property.getValue()));
        }
        return properties;
    }

    private static boolean boundedText(String text, int limit) {
        return text.codePointCount(0, text.length()) <= limit;
    }

    /** The contract's diagnostic quotes registry text of its own choosing, so bound what is logged. */
    private static String clipped(String reason) {
        if (reason == null) return "no detail";
        // The text quotes a registry value, so a mod chooses it: keep it short and on one line.
        StringBuilder clean = new StringBuilder();
        reason.codePoints().limit(REASON_CHARS)
                .forEach(point -> clean.appendCodePoint(Character.isISOControl(point) ? ' ' : point));
        return reason.codePointCount(0, reason.length()) > REASON_CHARS ? clean + "..." : clean.toString();
    }
}
