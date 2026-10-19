package dev.worldprompt;

import com.google.gson.Gson;
import dev.worldprompt.kernel.CoreLimits;
import dev.worldprompt.mycelium.block_catalog.v1.model.BlockCatalog;
import dev.worldprompt.mycelium.bridges.block_catalog.MinecraftBlockCatalog;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/** WorldPrompt's bounded wire representation of Mycelium's immutable native catalog. */
final class BlockCatalogTransport {
    private BlockCatalogTransport() { }

    /** Called once per server host, after native registries have been initialized, never by the HTTP thread. */
    static Map<String, Object> capture() {
        return encode(new MinecraftBlockCatalog().capture());
    }

    static Map<String, Object> encode(BlockCatalog catalog) {
        if (catalog.blocks().isEmpty() || catalog.blocks().size() > CoreLimits.MAX_CATALOG_BLOCKS)
            throw new IllegalArgumentException("The live block catalog exceeds Core's block limit.");
        bounded(catalog.version(), CoreLimits.MAX_CATALOG_VERSION_CHARS);
        if (catalog.version().codePoints().anyMatch(Character::isISOControl))
            throw new IllegalArgumentException("Invalid live Minecraft version.");
        Map<String, Object> blocks = new TreeMap<>();
        long propertyCount = 0, valueCount = 0;
        for (var block : catalog.blocks().entrySet()) {
            bounded(block.getKey(), CoreLimits.MAX_STATE_CHARS);
            var entry = block.getValue();
            if (entry.properties().size() > CoreLimits.MAX_CATALOG_PROPERTIES_PER_BLOCK)
                throw new IllegalArgumentException("A live block exceeds Core's property limit: " + block.getKey());
            propertyCount += entry.properties().size();
            Map<String, List<String>> properties = new TreeMap<>();
            for (var property : entry.properties().entrySet()) {
                bounded(property.getKey(), CoreLimits.MAX_STATE_CHARS);
                if (property.getValue().size() > CoreLimits.MAX_CATALOG_VALUES_PER_PROPERTY)
                    throw new IllegalArgumentException("A live property exceeds Core's value limit: " + property.getKey());
                for (String value : property.getValue()) {
                    bounded(value, CoreLimits.MAX_STATE_CHARS);
                    if (!value.matches("[a-z0-9_]+"))
                        throw new IllegalArgumentException("A live property cannot be represented by Core's block-state syntax: "
                                + block.getKey() + "/" + property.getKey());
                }
                valueCount += property.getValue().size();
                properties.put(property.getKey(), property.getValue().stream().sorted().toList());
            }
            if (propertyCount > CoreLimits.MAX_CATALOG_PROPERTIES || valueCount > CoreLimits.MAX_CATALOG_VALUES)
                throw new IllegalArgumentException("The live block catalog exceeds Core's property/value limits.");
            blocks.put(block.getKey(), Map.of("properties", Collections.unmodifiableMap(properties),
                    "defaults", Collections.unmodifiableMap(new TreeMap<>(entry.defaults()))));
        }
        Map<String, Object> transport = Map.of("minecraft_version", catalog.version(),
                "blocks", Collections.unmodifiableMap(blocks));
        if (new Gson().toJson(transport).getBytes(StandardCharsets.UTF_8).length > CoreLimits.MAX_CATALOG_BYTES)
            throw new IllegalArgumentException("The live block catalog exceeds Core's byte limit.");
        return transport;
    }

    private static void bounded(String text, int limit) {
        if (text.codePointCount(0, text.length()) > limit)
            throw new IllegalArgumentException("A live catalog field exceeds Core's text limit.");
    }
}
