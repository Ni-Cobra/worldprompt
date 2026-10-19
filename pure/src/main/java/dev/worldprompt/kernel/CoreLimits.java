package dev.worldprompt.kernel;

/**
 * Core limits the mod checks before sending a job or placing a result. Each value mirrors the
 * constant of the same name in Core's {@code limits.py}; {@code tests/test_limits_sync.py} keeps them equal.
 */
public final class CoreLimits {
    public static final int MAX_AXIS = 32_767;
    public static final int MAX_BLOCKS = 4_000_000;
    public static final int MAX_CAPTURE_PALETTE = 65_536;
    public static final int MAX_OPERATIONS = 512;
    public static final int MAX_BLOCK_CHOICES = 256;
    public static final int MAX_WORK = 80_000_000;
    public static final int MAX_REPLAY_BYTES = 64 * 1024 * 1024;
    public static final int MAX_PROMPT_BYTES = 64_000;
    public static final int MAX_LECTERNS = 64;
    public static final int MAX_BOOK_CHARACTERS = 4_000;
    public static final int MAX_TOTAL_LECTERN_CHARACTERS = 16_000;
    public static final int MAX_WORLD_COORDINATE = 30_000_000;
    public static final int MAX_STATE_CHARS = 512;
    public static final int MAX_CATALOG_BYTES = 16 * 1024 * 1024;
    public static final int MAX_CATALOG_BLOCKS = 65_536;
    public static final int MAX_CATALOG_PROPERTIES_PER_BLOCK = 64;
    public static final int MAX_CATALOG_VALUES_PER_PROPERTY = 256;
    public static final int MAX_CATALOG_PROPERTIES = 1_000_000;
    public static final int MAX_CATALOG_VALUES = 4_000_000;
    public static final int MAX_CATALOG_VERSION_CHARS = 128;

    /** A code point is at most four UTF-8 bytes, so a prompt this long always fits Core's byte limit. */
    public static final int MAX_PROMPT_CHARACTERS = MAX_PROMPT_BYTES / 4;

    private CoreLimits() {}
}
