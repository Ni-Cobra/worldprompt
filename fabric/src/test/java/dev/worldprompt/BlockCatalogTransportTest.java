package dev.worldprompt;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import dev.worldprompt.kernel.CoreLimits;
import dev.worldprompt.mycelium.block_catalog.v1.model.BlockCatalog;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** The shared Python fixture and the Java producer use the same wire shape and bounds. */
class BlockCatalogTransportTest {
    @Test void sharedCoreFixtureSurvivesTheTypedModelAndWireEncoding() throws Exception {
        Path directory = Path.of("").toAbsolutePath();
        while (directory != null && !Files.exists(directory.resolve("tests/fixtures/block_catalog.json")))
            directory = directory.getParent();
        assertNotNull(directory, "Shared Core catalog fixture is available in the checkout");
        JsonObject fixture = JsonParser.parseString(Files.readString(directory.resolve("tests/fixtures/block_catalog.json"))).getAsJsonObject();
        Map<String, BlockCatalog.Entry> blocks = new LinkedHashMap<>();
        fixture.getAsJsonObject("blocks").asMap().forEach((id, raw) -> {
            JsonObject entry = raw.getAsJsonObject();
            Map<String, List<String>> properties = new LinkedHashMap<>();
            entry.getAsJsonObject("properties").asMap().forEach((name, domain) -> {
                List<String> values = new ArrayList<>();
                domain.getAsJsonArray().forEach(value -> values.add(value.getAsString()));
                properties.put(name, values);
            });
            Map<String, String> defaults = new LinkedHashMap<>();
            entry.getAsJsonObject("defaults").asMap().forEach((name, value) -> defaults.put(name, value.getAsString()));
            blocks.put(id, new BlockCatalog.Entry(properties, defaults));
        });
        BlockCatalog catalog = new BlockCatalog(fixture.get("minecraft_version").getAsString(), blocks);
        assertEquals(fixture, new Gson().toJsonTree(BlockCatalogTransport.encode(catalog)));
        assertEquals(Map.of("tone", "cyan"), catalog.normalize("test:color_block", Map.of("tone", "cyan")));
        assertThrows(IllegalArgumentException.class, () -> catalog.normalize("test:color_block", Map.of("tone", "purple")));
    }

    @Test void theSerializedSnapshotCannotBeChangedByAnHttpSupplier() {
        var catalog = new BlockCatalog("fixture", Map.of("test:block", new BlockCatalog.Entry(
                Map.of("powered", List.of("false", "true")), Map.of("powered", "false"))));
        Map<String, Object> wire = BlockCatalogTransport.encode(catalog);
        Map<?, ?> blocks = (Map<?, ?>) wire.get("blocks");
        Map<?, ?> entry = (Map<?, ?>) blocks.get("test:block");
        Map<?, ?> properties = (Map<?, ?>) entry.get("properties");
        assertThrows(UnsupportedOperationException.class, () -> wire.remove("blocks"));
        assertThrows(UnsupportedOperationException.class, () -> blocks.remove("test:block"));
        assertThrows(UnsupportedOperationException.class, () -> ((Map<?, ?>) entry.get("defaults")).clear());
        assertThrows(UnsupportedOperationException.class, () -> ((List<?>) properties.get("powered")).clear());
    }

    @Test void protocolLimitsAreCheckedBeforeSubmission() {
        List<String> values = new ArrayList<>();
        for (int i = 0; i <= CoreLimits.MAX_CATALOG_VALUES_PER_PROPERTY; i++) values.add("v" + i);
        var tooManyValues = new BlockCatalog("fixture", Map.of("test:block", new BlockCatalog.Entry(
                Map.of("state", values), Map.of("state", "v0"))));
        assertThrows(IllegalArgumentException.class, () -> BlockCatalogTransport.encode(tooManyValues));
        var longVersion = new BlockCatalog("v".repeat(CoreLimits.MAX_CATALOG_VERSION_CHARS + 1),
                Map.of("test:block", new BlockCatalog.Entry(Map.of(), Map.of())));
        assertThrows(IllegalArgumentException.class, () -> BlockCatalogTransport.encode(longVersion));
    }
}
