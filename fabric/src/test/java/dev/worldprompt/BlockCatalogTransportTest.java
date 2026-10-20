package dev.worldprompt;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import dev.worldprompt.kernel.CoreLimits;
import dev.worldprompt.mycelium.block_catalog.v2.model.BlockCatalog;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** The shared Python fixture and the Java producer use the same wire shape, order and bounds. */
class BlockCatalogTransportTest {
    private static BlockCatalog.Entry entry(Map<String, List<String>> properties, Map<String, String> defaults) {
        return new BlockCatalog.Entry(properties, defaults);
    }

    private static Map<String, List<String>> domain(String name, List<String> values) {
        Map<String, List<String>> properties = new LinkedHashMap<>();
        properties.put(name, values);
        return properties;
    }

    @SuppressWarnings("unchecked")
    private static List<String> wireDomain(BlockCatalogTransport.Capture capture, String id, String property) {
        Map<String, Object> blocks = (Map<String, Object>) capture.transport().get("blocks");
        Map<String, Object> entry = (Map<String, Object>) blocks.get(id);
        return (List<String>) ((Map<String, Object>) entry.get("properties")).get(property);
    }

    @Test void sharedCoreFixtureSurvivesTheTypedModelAndWireEncoding() throws Exception {
        Path directory = Path.of("").toAbsolutePath();
        while (directory != null && !Files.exists(directory.resolve("tests/fixtures/block_catalog.json")))
            directory = directory.getParent();
        assertNotNull(directory, "Shared Core catalog fixture is available in the checkout");
        JsonObject fixture = JsonParser.parseString(Files.readString(directory.resolve("tests/fixtures/block_catalog.json"))).getAsJsonObject();
        Map<String, BlockCatalog.Entry> blocks = new LinkedHashMap<>();
        fixture.getAsJsonObject("blocks").asMap().forEach((id, raw) -> {
            JsonObject json = raw.getAsJsonObject();
            Map<String, List<String>> properties = new LinkedHashMap<>();
            json.getAsJsonObject("properties").asMap().forEach((name, values) -> {
                List<String> domain = new ArrayList<>();
                values.getAsJsonArray().forEach(value -> domain.add(value.getAsString()));
                properties.put(name, domain);
            });
            Map<String, String> defaults = new LinkedHashMap<>();
            json.getAsJsonObject("defaults").asMap().forEach((name, value) -> defaults.put(name, value.getAsString()));
            blocks.put(id, entry(properties, defaults));
        });
        BlockCatalog catalog = new BlockCatalog(fixture.get("minecraft_version").getAsString(), blocks);
        BlockCatalogTransport.Capture capture = BlockCatalogTransport.encode(catalog);
        assertTrue(capture.live());
        assertEquals(0, capture.excludedCount());
        assertNull(capture.unusableError());
        assertEquals(fixture, new Gson().toJsonTree(capture.transport()));
        assertEquals(Map.of("tone", "cyan"), catalog.normalize("test:color_block", Map.of("tone", "cyan")));
        assertThrows(IllegalArgumentException.class, () -> catalog.normalize("test:color_block", Map.of("tone", "purple")));
    }

    /** block.catalog@2 law C6 reaches the wire: neither the model nor the encoder may sort a domain. */
    @Test void nativeValueOrderSurvivesToTheWire() {
        List<String> facing = List.of("north", "south", "east", "west");
        List<String> rotation = new ArrayList<>();
        for (int i = 0; i < 16; i++) rotation.add(String.valueOf(i));
        Map<String, List<String>> properties = new LinkedHashMap<>();
        properties.put("facing", facing);
        properties.put("rotation", rotation);
        var catalog = new BlockCatalog("fixture", Map.of("test:beacon",
                entry(properties, Map.of("facing", "north", "rotation", "0"))));
        BlockCatalogTransport.Capture capture = BlockCatalogTransport.encode(catalog);
        assertEquals(facing, wireDomain(capture, "test:beacon", "facing"),
                "a direction enum keeps its declaration order, not east/north/south/west");
        assertEquals(rotation, wireDomain(capture, "test:beacon", "rotation"),
                "multi-digit numeric values stay in numeric order, not 0,1,10,11,...,2");
    }

    @Test void theSerializedSnapshotCannotBeChangedByAnHttpSupplier() {
        var catalog = new BlockCatalog("fixture", Map.of("test:block", entry(
                domain("powered", List.of("false", "true")), Map.of("powered", "false"))));
        Map<String, Object> wire = BlockCatalogTransport.encode(catalog).transport();
        Map<?, ?> blocks = (Map<?, ?>) wire.get("blocks");
        Map<?, ?> entry = (Map<?, ?>) blocks.get("test:block");
        Map<?, ?> properties = (Map<?, ?>) entry.get("properties");
        assertThrows(UnsupportedOperationException.class, () -> wire.remove("blocks"));
        assertThrows(UnsupportedOperationException.class, () -> blocks.remove("test:block"));
        assertThrows(UnsupportedOperationException.class, () -> ((Map<?, ?>) entry.get("defaults")).clear());
        assertThrows(UnsupportedOperationException.class, () -> ((List<?>) properties.get("powered")).clear());
    }

    /** One registry entry Core cannot carry must cost that entry, not the catalog and not the mod. */
    @Test void aPerBlockOutlierIsLeftOutAndEverythingElseStillShips() {
        List<String> tooManyValues = new ArrayList<>();
        for (int i = 0; i <= CoreLimits.MAX_CATALOG_VALUES_PER_PROPERTY; i++) tooManyValues.add("v" + i);
        Map<String, List<String>> tooManyProperties = new LinkedHashMap<>();
        Map<String, String> wideDefaults = new LinkedHashMap<>();
        for (int i = 0; i <= CoreLimits.MAX_CATALOG_PROPERTIES_PER_BLOCK; i++) {
            tooManyProperties.put("p" + i, List.of("a"));
            wideDefaults.put("p" + i, "a");
        }
        Map<String, BlockCatalog.Entry> blocks = new LinkedHashMap<>();
        blocks.put("test:good", entry(domain("powered", List.of("false", "true")), Map.of("powered", "false")));
        blocks.put("mod:wide_values", entry(domain("state", tooManyValues), Map.of("state", "v0")));
        blocks.put("mod:wide_properties", entry(tooManyProperties, wideDefaults));
        // A value the game can name but Core's block-state syntax cannot carry.
        blocks.put("mod:odd_syntax", entry(domain("shape", List.of("plain", "Mixed-Case")), Map.of("shape", "plain")));
        blocks.put("mod:long_value", entry(domain("tag", List.of("v".repeat(CoreLimits.MAX_STATE_CHARS + 1))),
                Map.of("tag", "v".repeat(CoreLimits.MAX_STATE_CHARS + 1))));

        BlockCatalogTransport.Capture capture = BlockCatalogTransport.encode(new BlockCatalog("fixture", blocks));
        assertTrue(capture.live(), "the catalog is still sent");
        assertNull(capture.unusableError());
        assertEquals(4, capture.excludedCount());
        assertEquals(List.of("mod:long_value", "mod:odd_syntax", "mod:wide_properties", "mod:wide_values"),
                capture.excluded());
        assertEquals(Map.of("test:good", Map.of("properties", Map.of("powered", List.of("false", "true")),
                        "defaults", Map.of("powered", "false"))),
                capture.transport().get("blocks"));
        assertNotNull(capture.exclusionWarning());
        assertTrue(capture.exclusionWarning().contains("mod:wide_values"));
    }

    /** A warning names a bounded sample, however many entries a modpack excludes. */
    @Test void theExclusionWarningIsBounded() {
        Map<String, BlockCatalog.Entry> blocks = new LinkedHashMap<>();
        blocks.put("test:good", entry(domain("powered", List.of("false", "true")), Map.of("powered", "false")));
        for (int i = 0; i < 40; i++)
            blocks.put("mod:bad" + i, entry(domain("shape", List.of("Bad")), Map.of("shape", "Bad")));
        BlockCatalogTransport.Capture capture = BlockCatalogTransport.encode(new BlockCatalog("fixture", blocks));
        assertTrue(capture.live());
        assertEquals(40, capture.excludedCount());
        assertEquals(8, capture.excluded().size());
        assertTrue(capture.exclusionWarning().contains("and 32 more"));
    }

    /**
     * A snapshot with nothing usable left, or one past a global limit, is not sent at all: Core then
     * uses its own bundled vanilla catalog instead of being handed a partial one as authoritative.
     */
    @Test void anUnusableSnapshotFallsBackToNoCatalogAtAll() {
        var noneRepresentable = new BlockCatalog("fixture", Map.of("mod:bad",
                entry(domain("shape", List.of("Bad")), Map.of("shape", "Bad"))));
        BlockCatalogTransport.Capture empty = BlockCatalogTransport.encode(noneRepresentable);
        assertFalse(empty.live());
        assertNull(empty.transport());
        assertNotNull(empty.unusableError());

        var longVersion = new BlockCatalog("v".repeat(CoreLimits.MAX_CATALOG_VERSION_CHARS + 1),
                Map.of("test:block", entry(Map.of(), Map.of())));
        assertFalse(BlockCatalogTransport.encode(longVersion).live());
    }

    /**
     * An aggregate overflow is not truncated by identifier order, which would silently drop whole
     * namespaces; it drops the snapshot. This covers the block total. The property and value totals
     * take the same branch, but their limits (1,000,000 and 4,000,000) are far too large to build an
     * input for in a unit test, so they are covered by inspection rather than here.
     */
    @Test void aGlobalOverflowDropsTheSnapshotInsteadOfTruncatingIt() {
        Map<String, BlockCatalog.Entry> blocks = new LinkedHashMap<>();
        BlockCatalog.Entry plain = entry(Map.of(), Map.of());
        for (int i = 0; i <= CoreLimits.MAX_CATALOG_BLOCKS; i++) blocks.put("mod:block" + i, plain);
        BlockCatalogTransport.Capture capture = BlockCatalogTransport.encode(new BlockCatalog("fixture", blocks));
        assertFalse(capture.live(), "a catalog over Core's block limit is not sent");
        assertEquals(0, capture.excludedCount(), "every block was representable; the total was not");
        assertNotNull(capture.unusableError());
        assertTrue(capture.unusableError().contains("bundled vanilla catalog"));
    }

    /** Text Core's protocol cannot carry costs that block, whichever field is too long. */
    @SuppressWarnings("unchecked")
    @Test void anOverLongIdentifierOrPropertyNameExcludesOnlyThatBlock() {
        Map<String, BlockCatalog.Entry> blocks = new LinkedHashMap<>();
        blocks.put("test:good", entry(domain("powered", List.of("false", "true")), Map.of("powered", "false")));
        blocks.put("mod:" + "n".repeat(CoreLimits.MAX_STATE_CHARS),
                entry(domain("powered", List.of("false")), Map.of("powered", "false")));
        String longName = "p".repeat(CoreLimits.MAX_STATE_CHARS + 1);
        blocks.put("mod:long_property", entry(domain(longName, List.of("a")), Map.of(longName, "a")));
        BlockCatalogTransport.Capture capture = BlockCatalogTransport.encode(new BlockCatalog("fixture", blocks));
        assertTrue(capture.live());
        assertEquals(2, capture.excludedCount());
        assertEquals(List.of("test:good"), List.copyOf(((Map<String, Object>) capture.transport().get("blocks")).keySet()));
    }

    /** A clean capture says nothing, so a warning cannot become server-start noise. */
    @Test void aCleanCaptureReportsNoWarningAtAll() {
        var catalog = new BlockCatalog("fixture", Map.of("test:block", entry(
                domain("powered", List.of("false", "true")), Map.of("powered", "false"))));
        BlockCatalogTransport.Capture capture = BlockCatalogTransport.encode(catalog);
        assertTrue(capture.live());
        assertEquals(0, capture.excludedCount());
        assertEquals(List.of(), capture.excluded());
        assertNull(capture.exclusionWarning());
        assertNull(capture.unusableError());
    }

    /**
     * The whole-catalog fallback, which is the path a contract rejection takes. The contract
     * refuses a malformed definition outright; that refusal must become a catalog-less capture
     * here rather than an exception, which is what keeps it from reaching the command host.
     */
    @Test void aSnapshotTheContractRefusesCostsTheCatalogAndNotTheMod() {
        // The control characters sit inside the part that is kept, so stripping them is
        // what removes them rather than the truncation.
        String noisy = "bad\nvalue\there: " + "x".repeat(500);
        BlockCatalogTransport.Capture capture = BlockCatalogTransport.capture(() -> {
            throw new IllegalArgumentException(noisy);
        });
        assertFalse(capture.live());
        assertNull(capture.transport());
        assertEquals(0, capture.excludedCount());
        String reported = capture.unusableError();
        assertNotNull(reported);
        assertTrue(reported.contains("IllegalArgumentException"));
        assertTrue(reported.contains("bundled vanilla catalog"));
        assertTrue(reported.contains("bad value here: "), "a control character becomes a space");
        assertFalse(reported.contains("\n") || reported.contains("\t"), "no control character reaches the log");
        assertTrue(reported.contains("..."), "an over-long diagnostic says it was cut");
    }

    /** The clip boundary itself: exactly at the limit keeps everything, and never splits a character. */
    @Test void aDiagnosticIsClippedByCharacterAndNotByCharUnit() {
        String exact = "y".repeat(300);
        String atLimit = BlockCatalogTransport.capture(() -> {
            throw new IllegalStateException(exact);
        }).unusableError();
        assertTrue(atLimit.contains(exact), "a diagnostic exactly at the limit is kept whole");
        assertFalse(atLimit.contains("..."), "nothing was cut, so nothing says it was");

        // Each of these is one character made of two char units. Cutting by char unit would
        // leave half of one behind.
        String emoji = "🧪".repeat(400);
        String clipped = BlockCatalogTransport.capture(() -> {
            throw new IllegalStateException(emoji);
        }).unusableError();
        assertTrue(clipped.contains("..."), "400 characters is past the limit");
        assertTrue(clipped.codePoints().noneMatch(point -> point >= Character.MIN_SURROGATE
                        && point <= Character.MAX_SURROGATE),
                "half of a character survived the clip");
    }
}
