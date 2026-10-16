package dev.worldprompt;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import net.minecraft.core.BlockPos;
import net.minecraft.core.component.DataComponents;
import net.minecraft.network.chat.Component;
import net.minecraft.server.network.Filterable;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.component.WritableBookContent;
import net.minecraft.world.item.component.WrittenBookContent;
import net.minecraft.world.level.block.entity.BlockEntity;
import net.minecraft.world.level.block.entity.LecternBlockEntity;

/**
 * Lecterns inside a captured selection holding a prompt book. Each book is a
 * location-specific part of the player's request. Capture already reads every block
 * entity in the selection, so detection needs no separate world scan.
 *
 * <p>A book counts when it is a signed written book titled "prompt", any written book or
 * book and quill whose custom (anvil) name is "prompt", or any written book or book and
 * quill whose text starts with the word "prompt". All comparisons trim whitespace and
 * ignore case; a leading marker word, with an optional colon, is removed from the text.
 * Text is the raw, unfiltered page text in page order. Other books with text are listed in
 * a warning, since one may be a prompt the player forgot to mark.
 */
final class PromptLecterns {
    static final int MAX_LECTERNS = CoreLimits.MAX_LECTERNS;
    static final int MAX_BOOK_CHARACTERS = CoreLimits.MAX_BOOK_CHARACTERS;
    static final int MAX_TOTAL_CHARACTERS = CoreLimits.MAX_TOTAL_LECTERN_CHARACTERS;

    /** World and selection-local coordinates; text is kept only up to the per-book limit. */
    record Lectern(int worldX, int worldY, int worldZ, int x, int y, int z, String text, int characters) {
        String world() { return "(" + worldX + ", " + worldY + ", " + worldZ + ")"; }
    }

    private final RegionBounds bounds;
    private final List<Lectern> lecterns = new ArrayList<>();
    private final List<String> empty = new ArrayList<>();
    private final List<String> ignored = new ArrayList<>();
    private int found;

    PromptLecterns(RegionBounds bounds) { this.bounds = bounds; }

    void inspect(BlockPos pos, BlockEntity entity) {
        if (!(entity instanceof LecternBlockEntity lectern) || !lectern.hasBook()) return;
        List<String> pages = promptPages(lectern.getBook());
        if (pages != null) record(pos.getX(), pos.getY(), pos.getZ(), pages);
        else ignore(pos.getX(), pos.getY(), pos.getZ(), bookPages(lectern.getBook()));
    }

    /** Raw pages of a written book or book and quill, or null for any other item. */
    static List<String> bookPages(ItemStack book) {
        WrittenBookContent written = book.get(DataComponents.WRITTEN_BOOK_CONTENT);
        if (written != null) return written.pages().stream().map(page -> page.raw().getString()).toList();
        WritableBookContent writable = book.get(DataComponents.WRITABLE_BOOK_CONTENT);
        if (writable != null) return writable.pages().stream().map(Filterable::raw).toList();
        return null;
    }

    /** Pages of a prompt book, or null when the item is not a prompt book. */
    static List<String> promptPages(ItemStack book) {
        List<String> pages = bookPages(book);
        if (pages == null) return null;
        Component customName = book.get(DataComponents.CUSTOM_NAME);
        WrittenBookContent written = book.get(DataComponents.WRITTEN_BOOK_CONTENT);
        boolean named = customName != null && isPromptName(customName.getString())
                || written != null && isPromptName(written.title().raw());
        return named ? pages : withoutMarker(pages);
    }

    static boolean isPromptName(String name) { return name != null && name.strip().equalsIgnoreCase("prompt"); }

    /**
     * Pages with the leading "prompt" word (and an optional colon) removed from the first
     * nonblank page, or null when the text does not start with that word.
     */
    static List<String> withoutMarker(List<String> pages) {
        for (int i = 0; i < pages.size(); i++) {
            String page = TextSanitizer.clean(pages.get(i), false).strip();
            if (blank(page)) continue;
            if (!page.regionMatches(true, 0, "prompt", 0, 6)) return null;
            if (page.length() > 6 && Character.isLetterOrDigit(page.codePointAt(6))) return null;
            String rest = page.substring(6).stripLeading();
            if (rest.startsWith(":")) rest = rest.substring(1).stripLeading();
            List<String> kept = new ArrayList<>(pages);
            kept.set(i, rest);
            return kept;
        }
        return null;
    }

    void record(int worldX, int worldY, int worldZ, List<String> pages) {
        if (!bounds.contains(worldX, worldY, worldZ)) return;
        String text = joinPages(pages);
        if (text.isEmpty()) {
            if (empty.size() < MAX_LECTERNS) empty.add("(" + worldX + ", " + worldY + ", " + worldZ + ")");
            return;
        }
        found++;
        if (lecterns.size() >= MAX_LECTERNS) return;
        int characters = text.codePointCount(0, text.length());
        // Over-limit books are rejected by validate(); never retain their full text.
        if (characters > MAX_BOOK_CHARACTERS) text = text.substring(0, text.offsetByCodePoints(0, MAX_BOOK_CHARACTERS));
        lecterns.add(new Lectern(worldX, worldY, worldZ, worldX - bounds.minX(), worldY - bounds.minY(),
                worldZ - bounds.minZ(), text, characters));
    }

    /** A book with text that is not a prompt book; listed by warnings(). */
    void ignore(int worldX, int worldY, int worldZ, List<String> pages) {
        if (pages == null || !bounds.contains(worldX, worldY, worldZ) || joinPages(pages).isEmpty()) return;
        if (ignored.size() < MAX_LECTERNS) ignored.add("(" + worldX + ", " + worldY + ", " + worldZ + ")");
    }

    /** Nonblank pages in order; with more than one, each starts with a "--- page N ---" line. */
    static String joinPages(List<String> pages) {
        List<String> kept = new ArrayList<>();
        List<Integer> numbers = new ArrayList<>();
        for (int i = 0; i < pages.size(); i++) {
            String page = TextSanitizer.clean(pages.get(i), false);
            if (!blank(page)) { kept.add(page.strip()); numbers.add(i + 1); }
        }
        if (kept.size() == 1) return kept.getFirst();
        StringBuilder text = new StringBuilder();
        for (int i = 0; i < kept.size(); i++) {
            if (i > 0) text.append("\n\n");
            text.append("--- page ").append(numbers.get(i)).append(" ---\n").append(kept.get(i));
        }
        return text.toString();
    }

    /** Matches Core's notion of whitespace-only text, including non-breaking spaces. */
    static boolean blank(String text) {
        return text.codePoints().allMatch(point -> Character.isWhitespace(point) || Character.isSpaceChar(point));
    }

    /** Reject the whole submission rather than silently dropping or truncating instructions. */
    void validate() {
        if (found > MAX_LECTERNS)
            throw new IllegalArgumentException("The selection contains " + found + " prompt lecterns; at most "
                    + MAX_LECTERNS + " are supported. Remove some prompt books and retry.");
        int total = 0;
        for (Lectern lectern : lecterns) {
            if (lectern.characters() > MAX_BOOK_CHARACTERS)
                throw new IllegalArgumentException("The prompt book on the lectern at " + lectern.world() + " has "
                        + lectern.characters() + " characters; at most " + MAX_BOOK_CHARACTERS
                        + " are allowed per book. Shorten it and retry.");
            total += lectern.characters();
        }
        if (total > MAX_TOTAL_CHARACTERS)
            throw new IllegalArgumentException("Prompt lectern books contain " + total + " characters in total; at most "
                    + MAX_TOTAL_CHARACTERS + " are allowed. Shorten them and retry.");
    }

    /** Informational chat lines, e.g. "2 prompt lecterns: (12, 64, -30), (20, 64, -30)". */
    List<String> summary() {
        List<String> lines = new ArrayList<>();
        if (!lecterns.isEmpty())
            lines.add(lecterns.size() + (lecterns.size() == 1 ? " prompt lectern: " : " prompt lecterns: ")
                    + String.join(", ", lecterns.stream().map(Lectern::world).toList()));
        if (!empty.isEmpty())
            lines.add("Ignored empty prompt book" + (empty.size() == 1 ? "" : "s") + ": " + String.join(", ", empty));
        return lines;
    }

    /** Warning chat lines for lecterns whose books have text but are not marked as prompts. */
    List<String> warnings() {
        if (ignored.isEmpty()) return List.of();
        String marker = "start its first page with \"prompt:\".";
        if (ignored.size() == 1)
            return List.of("The lectern at " + ignored.getFirst() + " holds a book not marked as a prompt, so it was "
                    + "ignored. If it is meant as one, " + marker);
        return List.of(ignored.size() + " lecterns hold books not marked as prompts, so they were ignored: "
                + String.join(", ", ignored) + ". If one is meant as a prompt, " + marker);
    }

    /** Core's optional "lecterns" field: local position (plan coordinates), world position and text. */
    List<Map<String, Object>> transport() {
        List<Map<String, Object>> entries = new ArrayList<>(lecterns.size());
        for (Lectern lectern : lecterns) {
            Map<String, Object> entry = new LinkedHashMap<>();
            entry.put("position", new int[]{lectern.x(), lectern.y(), lectern.z()});
            entry.put("world_position", new int[]{lectern.worldX(), lectern.worldY(), lectern.worldZ()});
            entry.put("text", lectern.text());
            entries.add(entry);
        }
        return entries;
    }
}
