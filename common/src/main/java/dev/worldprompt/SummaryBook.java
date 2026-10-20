package dev.worldprompt;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import dev.worldprompt.kernel.PromptRequest;
import dev.worldprompt.kernel.TextSanitizer;
import java.util.ArrayList;
import java.util.List;
import net.minecraft.core.component.DataComponents;
import net.minecraft.network.chat.Component;
import net.minecraft.server.network.Filterable;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.item.component.WrittenBookContent;

/**
 * A signed written book holding an edit's prompt, model settings and the model's plan summary.
 * A Double pass book has a section for the draft plan's summary and one for the reviewed plan's.
 */
final class SummaryBook {
    /** The vanilla book screen shows 14 lines of at most 114 pixels per page. */
    static final int PAGE_WIDTH = 114, PAGE_LINES = 14, MAX_PAGES = 100;
    private static final int MAX_AUTHOR = 64;

    private SummaryBook() {}

    static ItemStack create(String id, PromptRequest prompt, ReplayPayload replay) {
        String model = text(replay.modelSettings, "model");
        List<Filterable<Component>> pages = pages(sections(id, prompt, replay))
                .stream().map(page -> Filterable.passThrough((Component) Component.literal(page))).toList();
        String author = model.isEmpty() ? "WorldPrompt" : model.substring(0, Math.min(MAX_AUTHOR, model.length()));
        ItemStack book = new ItemStack(Items.WRITTEN_BOOK);
        book.set(DataComponents.WRITTEN_BOOK_CONTENT, new WrittenBookContent(
                Filterable.passThrough("WorldPrompt " + Feedback.shortId(id)), author, 0, pages, true));
        return book;
    }

    /** The cover, then one summary section, or a draft and a review section for Double pass. */
    static String[] sections(String id, PromptRequest prompt, ReplayPayload replay) {
        JsonObject settings = replay.modelSettings;
        String model = text(settings, "model");
        String summary = text(replay.plan, "summary");
        String cover = "WorldPrompt build\n" + Feedback.shortId(id) + "\n\nPrompt\n" + prompt.prompt()
                + "\n\n" + (model.isEmpty() ? "Model unknown" : model)
                + (text(settings, "reasoning_effort").isEmpty() ? "" : "\nEffort " + text(settings, "reasoning_effort"))
                + "\n" + Feedback.passLabel(prompt.effort())
                + TokenUsage.from(settings).suffix().replaceFirst("^ · ", "\n");
        if (replay.draftSummary == null)
            return new String[]{cover, summary.isEmpty() ? "The model returned no summary." : "Summary\n\n" + summary};
        return new String[]{cover,
                "Draft\n\n" + (replay.draftSummary.isEmpty() ? "The draft returned no summary." : replay.draftSummary),
                "Review (built)\n\n" + (summary.isEmpty() ? "The review returned no summary." : summary)};
    }

    /**
     * Wrap each section onto its own pages. Widths over-estimate the default font, so the
     * client never re-wraps a line and pushes text past the last visible line of a page.
     */
    static List<String> pages(String... sections) {
        List<String> pages = new ArrayList<>();
        for (String section : sections) {
            List<String> lines = new ArrayList<>();
            for (String paragraph : TextSanitizer.clean(section, true).split("\n", -1)) lines.addAll(wrap(paragraph));
            List<String> page = new ArrayList<>();
            for (String line : lines) {
                if (page.isEmpty() && line.isEmpty()) continue;
                page.add(line);
                if (page.size() == PAGE_LINES) { pages.add(String.join("\n", page)); page.clear(); }
            }
            if (!page.isEmpty()) pages.add(String.join("\n", page));
        }
        if (pages.size() > MAX_PAGES) {
            pages.subList(MAX_PAGES, pages.size()).clear();
            List<String> last = new ArrayList<>(List.of(pages.getLast().split("\n", -1)));
            if (last.size() == PAGE_LINES) last.removeLast();
            last.add("…");
            pages.set(MAX_PAGES - 1, String.join("\n", last));
        }
        return pages.isEmpty() ? List.of("") : pages;
    }

    private static List<String> wrap(String paragraph) {
        List<String> lines = new ArrayList<>();
        StringBuilder line = new StringBuilder();
        int used = 0;
        for (String word : paragraph.strip().split(" +")) {
            if (word.isEmpty()) continue;
            int wordWidth = width(word), space = line.isEmpty() ? 0 : width(" ");
            if (used + space + wordWidth <= PAGE_WIDTH) {
                if (space > 0) line.append(' ');
                line.append(word); used += space + wordWidth;
                continue;
            }
            if (!line.isEmpty()) { lines.add(line.toString()); line.setLength(0); used = 0; }
            // A word wider than the page is split between code points.
            for (int i = 0; i < word.length(); ) {
                int point = word.codePointAt(i), glyph = width(point);
                if (used + glyph > PAGE_WIDTH && !line.isEmpty()) { lines.add(line.toString()); line.setLength(0); used = 0; }
                line.appendCodePoint(point); used += glyph;
                i += Character.charCount(point);
            }
        }
        lines.add(line.toString());
        return lines;
    }

    static int width(String text) {
        return text.codePoints().map(SummaryBook::width).sum();
    }

    /** Advance in pixels, including the one-pixel gap, for Minecraft's default font. */
    static int width(int point) {
        return switch (point) {
            case '!', '\'', ',', '.', ':', ';', 'i', '|' -> 2;
            case '`', 'l' -> 3;
            case ' ', '"', '(', ')', '*', 'I', '[', ']', 't', '{', '}' -> 4;
            case '<', '>', 'f', 'k' -> 5;
            case '@', '~' -> 7;
            default -> point < 0x250 ? 6 : 9;
        };
    }

    private static String text(JsonObject object, String key) {
        JsonElement value = object.get(key);
        return value == null || !value.isJsonPrimitive() ? "" : value.getAsString().strip();
    }
}
