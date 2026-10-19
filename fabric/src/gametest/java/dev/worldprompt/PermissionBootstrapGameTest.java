package dev.worldprompt;

import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.resources.Identifier;

public final class PermissionBootstrapGameTest {
    @GameTest
    public void commandFunctionsParseAtColdStart(GameTestHelper context) {
        var id = Identifier.fromNamespaceAndPath("worldprompt-test", "permission_bootstrap");
        context.assertTrue(context.getLevel().getServer().getFunctions().get(id).isPresent(),
                "WorldPrompt commands must parse before the permission handler starts.");
        context.succeed();
    }
}
