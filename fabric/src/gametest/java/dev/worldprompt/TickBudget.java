package dev.worldprompt;

import java.util.function.Supplier;
import net.minecraft.gametest.framework.GameTestHelper;

/**
 * Bounds a GameTest in server ticks rather than wall-clock time, so a slow or busy machine cannot fail it
 * and a stuck job fails with a message that says where it stopped.
 */
final class TickBudget {
    private TickBudget() {}

    /** Fails the test when more than {@code ticks} server ticks have passed since it started. */
    static void within(GameTestHelper context, int ticks, Supplier<String> where) {
        if (context.getTick() > ticks) context.fail("Not finished within " + ticks + " ticks: " + where.get());
    }
}
