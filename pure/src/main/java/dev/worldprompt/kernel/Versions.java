package dev.worldprompt.kernel;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Core and the mod must share MAJOR.MINOR, the Core ↔ mod API version. Each side sends its version on
 * every exchange (Core in {@link #CORE_HEADER}, the mod in {@link #MOD_HEADER}) and refuses a mismatch.
 */
public final class Versions {
    public static final String CORE_HEADER = "X-WorldPrompt-Version";
    public static final String MOD_HEADER = "X-WorldPrompt-Mod-Version";
    /** Core's error code for a request from an incompatible mod; the mod uses it for an incompatible Core too. */
    public static final String MISMATCH = "version_mismatch";
    private static final Pattern VERSION = Pattern.compile("(\\d{1,9})\\.(\\d{1,9})\\.\\d{1,9}(?:[-+][0-9A-Za-z.+-]{1,64})?");
    /** This mod's version, written into a resource from gradle.properties at build time. */
    public static final String MOD = load();

    private Versions() {}

    private static String load() {
        try (InputStream stream = Versions.class.getResourceAsStream("/dev/worldprompt/version.txt")) {
            if (stream == null) throw new IllegalStateException("The WorldPrompt mod jar has no version resource.");
            String version = new String(stream.readAllBytes(), StandardCharsets.UTF_8).strip();
            if (api(version) == null) throw new IllegalStateException("The WorldPrompt mod has a malformed version: " + version);
            return version;
        } catch (IOException e) { throw new IllegalStateException("Cannot read the WorldPrompt mod version.", e); }
    }

    /** MAJOR.MINOR of a version, or null if it is not a MAJOR.MINOR.PATCH version. */
    public static String api(String version) {
        Matcher match = version == null ? null : VERSION.matcher(version);
        if (match == null || !match.matches()) return null;
        return Integer.parseInt(match.group(1)) + "." + Integer.parseInt(match.group(2));
    }

    /** Null when Core's reported version speaks this mod's API, else the message the player sees. */
    public static String coreMismatch(String coreVersion) {
        String mod = api(MOD), core = api(coreVersion);
        if (coreVersion == null) return "WorldPrompt Core is older than this mod (" + MOD + ") and does not report its version. "
                + "Install WorldPrompt Core " + mod + ".x to match the mod.";
        if (mod.equals(core)) return null;
        if (core == null) return "WorldPrompt Core reports an unreadable version. Install Core " + mod + ".x to match mod " + MOD + ".";
        return "WorldPrompt Core " + coreVersion + " does not work with mod " + MOD
                + ": the first two version numbers must match. Install Core " + mod + ".x, or mod " + core + ".x.";
    }
}
