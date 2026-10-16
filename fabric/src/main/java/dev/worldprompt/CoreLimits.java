package dev.worldprompt;

/**
 * Core limits the mod checks before sending a job or placing a result. Each value mirrors the
 * constant of the same name in Core's {@code limits.py}; {@code tests/test_limits_sync.py} keeps them equal.
 */
final class CoreLimits {
    static final int MAX_AXIS = 32_767;
    static final int MAX_BLOCKS = 4_000_000;
    static final int MAX_CAPTURE_PALETTE = 65_536;
    static final int MAX_OPERATIONS = 512;
    static final int MAX_BLOCK_CHOICES = 256;
    static final int MAX_WORK = 80_000_000;
    static final int MAX_REPLAY_BYTES = 64 * 1024 * 1024;
    static final int MAX_PROMPT_BYTES = 64_000;
    static final int MAX_LECTERNS = 64;
    static final int MAX_BOOK_CHARACTERS = 4_000;
    static final int MAX_TOTAL_LECTERN_CHARACTERS = 16_000;
    static final int MAX_WORLD_COORDINATE = 30_000_000;

    /** A code point is at most four UTF-8 bytes, so a prompt this long always fits Core's byte limit. */
    static final int MAX_PROMPT_CHARACTERS = MAX_PROMPT_BYTES / 4;

    private CoreLimits() {}
}
