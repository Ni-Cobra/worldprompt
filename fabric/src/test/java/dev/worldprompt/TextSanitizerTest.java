package dev.worldprompt;

import dev.worldprompt.kernel.TextSanitizer;

import static org.junit.jupiter.api.Assertions.*;
import org.junit.jupiter.api.Test;

class TextSanitizerTest {
    @Test void keepsNewlinesAndDropsOtherControls() {
        assertEquals("a\nb\nc d", TextSanitizer.clean("a\r\nb\rc\td", false));
        assertEquals("bell gone, c1 gone", TextSanitizer.clean("bell\u0007 gone, c1\u0085 gone", false));
        assertEquals("café 🏠", TextSanitizer.clean("café 🏠", false));
        assertEquals("lone ", TextSanitizer.clean("lone \uD800", false));
    }
    @Test void formattingCodesSurviveUnlessAskedToDropThem() {
        assertEquals("§cred", TextSanitizer.clean("§cred", false));
        assertEquals("red", TextSanitizer.clean("§cred", true));
        assertEquals("red text\nnext", TextSanitizer.clean("§cred\u0007 text\r\nnext", true));
    }
}
