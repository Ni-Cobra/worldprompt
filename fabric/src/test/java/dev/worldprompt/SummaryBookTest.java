package dev.worldprompt;

import static org.junit.jupiter.api.Assertions.*;
import java.util.List;
import org.junit.jupiter.api.Test;

class SummaryBookTest {
    private static void assertFits(List<String> pages) {
        for (String page : pages) {
            List<String> lines = List.of(page.split("\n", -1));
            assertTrue(lines.size() <= SummaryBook.PAGE_LINES, page);
            for (String line : lines) assertTrue(SummaryBook.width(line) <= SummaryBook.PAGE_WIDTH, line);
        }
    }
    @Test void wordsWrapWithinThePageWidthAndLineCount() {
        String text = "The tower rises from a cobblestone plinth with spruce framing and a spiral stair. ".repeat(40);
        List<String> pages = SummaryBook.pages(text);
        assertTrue(pages.size() > 1);
        assertFits(pages);
        assertEquals(text.strip().replace(" ", ""), String.join("", pages).replace("\n", "").replace(" ", ""));
    }
    @Test void eachSectionStartsOnANewPageAndKeepsExplicitBreaks() {
        List<String> pages = SummaryBook.pages("Cover\n\nPrompt", "Summary\n\nFirst\nSecond");
        assertEquals(List.of("Cover\n\nPrompt", "Summary\n\nFirst\nSecond"), pages);
    }
    @Test void wordsWiderThanAPageAreSplit() {
        String word = "W".repeat(60);
        List<String> pages = SummaryBook.pages(word);
        assertFits(pages);
        assertEquals(word, pages.getFirst().replace("\n", ""));
    }
    @Test void narrowGlyphsFitMoreCharactersPerLine() {
        assertEquals(SummaryBook.PAGE_WIDTH / 2, SummaryBook.pages("i".repeat(SummaryBook.PAGE_WIDTH / 2)).getFirst().length());
        assertEquals(9, SummaryBook.width('塔'));
    }
    @Test void longSummariesStopAtTheBookLimitWithAnEllipsis() {
        List<String> pages = SummaryBook.pages("word ".repeat(20_000));
        assertEquals(SummaryBook.MAX_PAGES, pages.size());
        assertFits(pages);
        assertTrue(pages.getLast().endsWith("\n…"));
    }
    @Test void formattingCodesAndControlCharactersAreRemoved() {
        assertEquals(List.of("red text\nnext"), SummaryBook.pages("§cred\u0007 text\r\nnext"));
        assertEquals(List.of(""), SummaryBook.pages(""));
    }
}
