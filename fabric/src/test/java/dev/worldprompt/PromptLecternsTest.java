package dev.worldprompt;

import dev.worldprompt.kernel.RegionBounds;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.util.List;
import org.junit.jupiter.api.Test;

class PromptLecternsTest {
    private static final RegionBounds BOUNDS = new RegionBounds(10, 60, -30, 20, 10, 20);

    @Test void promptNamesAreTrimmedAndCaseInsensitive() {
        for (String name : List.of("prompt", "Prompt", " PROMPT ", "\tpRoMpT\n"))
            assertTrue(PromptLecterns.isPromptName(name), name);
        for (String name : List.of("prompts", "my prompt", "prompt 2", "", "p r o m p t"))
            assertFalse(PromptLecterns.isPromptName(name), name);
        assertFalse(PromptLecterns.isPromptName(null));
    }

    @Test void textStartingWithThePromptWordCountsAndDropsTheMarker() {
        assertEquals(List.of("Kitchen"), PromptLecterns.withoutMarker(List.of("prompt Kitchen")));
        assertEquals(List.of("Kitchen: stone"), PromptLecterns.withoutMarker(List.of("  PROMPT: Kitchen: stone")));
        assertEquals(List.of("\n", "Bedroom\nRed bed", "Hall"),
                PromptLecterns.withoutMarker(List.of("\n", " Prompt\nBedroom\nRed bed", "Hall")));
        assertEquals(List.of(""), PromptLecterns.withoutMarker(List.of("prompt")));
        for (String text : List.of("prompts", "prompting is fun", "my prompt", "prompt2", "", " "))
            assertNull(PromptLecterns.withoutMarker(List.of(text)), text);
        assertNull(PromptLecterns.withoutMarker(List.of()));
    }

    @Test void blankMatchesWhitespaceOnlyText() {
        assertTrue(PromptLecterns.blank("  \n "));
        assertFalse(PromptLecterns.blank(" x "));
    }

    @Test void pagesAreJoinedInOrderWithClearBreaks() {
        assertEquals("Only page", PromptLecterns.joinPages(List.of("  Only page \n")));
        assertEquals("Second only", PromptLecterns.joinPages(List.of(" ", "Second only")));
        assertEquals("--- page 1 ---\nKitchen\n\n--- page 3 ---\nStone counters",
                PromptLecterns.joinPages(List.of("Kitchen", "  \n", "Stone counters")));
        assertEquals("", PromptLecterns.joinPages(List.of()));
        assertEquals("", PromptLecterns.joinPages(List.of("  ", "\n")));
    }

    @Test void recordsLocalAndWorldCoordinatesInsideTheSelectionOnly() {
        PromptLecterns lecterns = new PromptLecterns(BOUNDS);
        lecterns.record(12, 64, -30, List.of("Kitchen"));
        lecterns.record(20, 64, -30, List.of("Bedroom", "Red bed"));
        lecterns.record(9, 64, -30, List.of("Outside, west"));
        lecterns.record(30, 64, -30, List.of("Outside, east"));
        lecterns.record(15, 61, -25, List.of(" "));
        lecterns.validate();
        assertEquals(List.of("2 prompt lecterns: (12, 64, -30), (20, 64, -30)",
                "Ignored empty prompt book: (15, 61, -25)"), lecterns.summary());
        JsonArray payload = new Gson().toJsonTree(lecterns.transport()).getAsJsonArray();
        assertEquals(2, payload.size());
        JsonObject first = payload.get(0).getAsJsonObject();
        assertEquals("[2,4,0]", first.get("position").toString());
        assertEquals("[12,64,-30]", first.get("world_position").toString());
        assertEquals("Kitchen", first.get("text").getAsString());
        assertEquals("--- page 1 ---\nBedroom\n\n--- page 2 ---\nRed bed",
                payload.get(1).getAsJsonObject().get("text").getAsString());
        PromptLecterns one = new PromptLecterns(BOUNDS);
        one.record(10, 60, -30, List.of("Corner"));
        assertEquals(List.of("1 prompt lectern: (10, 60, -30)"), one.summary());
        assertTrue(new PromptLecterns(BOUNDS).summary().isEmpty());
        assertTrue(new PromptLecterns(BOUNDS).transport().isEmpty());
    }

    @Test void booksWithTextThatAreNotPromptsAreWarnedAbout() {
        PromptLecterns lecterns = new PromptLecterns(BOUNDS);
        assertTrue(lecterns.warnings().isEmpty());
        lecterns.ignore(12, 64, -30, List.of("Shopping list"));
        lecterns.ignore(13, 64, -30, List.of(" ", "\n"));
        lecterns.ignore(14, 64, -30, null);
        lecterns.ignore(9, 64, -30, List.of("Outside"));
        assertEquals(List.of("The lectern at (12, 64, -30) holds a book not marked as a prompt, so it was ignored. "
                + "If it is meant as one, start its first page with \"prompt:\"."), lecterns.warnings());
        lecterns.ignore(20, 64, -30, List.of("Notes"));
        assertEquals(List.of("2 lecterns hold books not marked as prompts, so they were ignored: (12, 64, -30), "
                + "(20, 64, -30). If one is meant as a prompt, start its first page with \"prompt:\"."), lecterns.warnings());
        assertTrue(lecterns.summary().isEmpty() && lecterns.transport().isEmpty());
    }

    @Test void limitsRejectWithClearMessages() {
        PromptLecterns many = new PromptLecterns(BOUNDS);
        for (int i = 0; i <= PromptLecterns.MAX_LECTERNS; i++) many.record(10 + i % 20, 60 + i / 20, -30, List.of("x"));
        var tooMany = assertThrows(IllegalArgumentException.class, many::validate);
        assertTrue(tooMany.getMessage().contains("65 prompt lecterns; at most 64"), tooMany.getMessage());
        assertEquals(PromptLecterns.MAX_LECTERNS, many.transport().size());

        PromptLecterns longBook = new PromptLecterns(BOUNDS);
        longBook.record(12, 64, -30, List.of("y".repeat(PromptLecterns.MAX_BOOK_CHARACTERS + 1)));
        var tooLong = assertThrows(IllegalArgumentException.class, longBook::validate);
        assertTrue(tooLong.getMessage().contains("(12, 64, -30) has 4001 characters; at most 4000"), tooLong.getMessage());
        assertEquals(PromptLecterns.MAX_BOOK_CHARACTERS, ((String) longBook.transport().getFirst().get("text")).length(),
                "Over-limit text is never retained in full.");

        PromptLecterns exact = new PromptLecterns(BOUNDS);
        // Characters are code points, as in Core: each emoji is one character.
        for (int i = 0; i < 4; i++) exact.record(10 + i, 60, -30, List.of("🏠".repeat(PromptLecterns.MAX_BOOK_CHARACTERS)));
        exact.validate();
        exact.record(15, 60, -30, List.of("z"));
        var tooMuch = assertThrows(IllegalArgumentException.class, exact::validate);
        assertTrue(tooMuch.getMessage().contains("16001 characters in total; at most 16000"), tooMuch.getMessage());
    }
}
