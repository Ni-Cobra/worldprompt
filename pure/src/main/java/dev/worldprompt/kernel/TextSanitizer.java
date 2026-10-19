package dev.worldprompt.kernel;

/** The one cleanup for text that crosses between Minecraft books and plain text, in both directions. */
public final class TextSanitizer {
    private TextSanitizer() {}

    /**
     * Keep newlines; turn tabs into spaces and any carriage return into a newline; drop other control
     * characters and lone surrogates. With {@code dropFormattingCodes}, also drop each legacy "§" code and
     * the character after it, for text that is shown in a book rather than read as the player's words.
     */
    public static String clean(String text, boolean dropFormattingCodes) {
        String normalized = text.replace("\r\n", "\n").replace('\r', '\n').replace('\t', ' ');
        StringBuilder out = new StringBuilder(normalized.length());
        int[] points = normalized.codePoints().toArray();
        for (int i = 0; i < points.length; i++) {
            int point = points[i];
            if (dropFormattingCodes && point == '§') i++;
            else if (point == '\n' || !(Character.isISOControl(point) || (point >= 0xD800 && point <= 0xDFFF)))
                out.appendCodePoint(point);
        }
        return out.toString();
    }
}
