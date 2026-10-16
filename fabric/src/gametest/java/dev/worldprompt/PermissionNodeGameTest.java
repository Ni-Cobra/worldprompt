package dev.worldprompt;

import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import me.lucko.fabric.api.permissions.v0.PermissionCheckEvent;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.fabricmc.fabric.api.util.TriState;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.server.permissions.PermissionSet;

/** Stands in for LuckPerms: answers permission checks through the bundled fabric-permissions-api. */
public final class PermissionNodeGameTest {
    private static final Map<UUID, Map<String, Boolean>> GRANTS = new ConcurrentHashMap<>();
    static {
        PermissionCheckEvent.EVENT.register((source, node) -> {
            if (!(source instanceof CommandSourceStack stack) || stack.getEntity() == null) return TriState.DEFAULT;
            Boolean value = GRANTS.getOrDefault(stack.getEntity().getUUID(), Map.of()).get(node);
            return value == null ? TriState.DEFAULT : TriState.of(value);
        });
    }

    @GameTest
    public void permissionNodesOverrideOperatorDefaults(GameTestHelper context) {
        var wp = context.getLevel().getServer().getCommands().getDispatcher().getRoot().getChild("wp");
        ServerPlayer builder = context.makeMockServerPlayerInLevel();
        ServerPlayer operator = context.makeMockServerPlayerInLevel();
        GRANTS.put(builder.getUUID(), Map.of("worldprompt.command.prompt", true, "worldprompt.command.info", false));
        GRANTS.put(operator.getUUID(), Map.of("worldprompt.command.rebuild", false));
        try {
            var plain = builder.createCommandSourceStack().withPermission(PermissionSet.NO_PERMISSIONS);
            context.assertTrue(wp.getChild("prompt").canUse(plain), "A granted node lets a non-operator prompt.");
            context.assertFalse(wp.getChild("info").canUse(plain), "A denied node hides an otherwise open command.");
            context.assertFalse(wp.getChild("undo").canUse(plain), "An unset node keeps the operator requirement.");
            context.assertTrue(wp.getChild("pos1").canUse(plain) && wp.getChild("book").canUse(plain),
                    "Unset nodes keep open commands available.");
            var op = operator.createCommandSourceStack().withPermission(PermissionSet.ALL_PERMISSIONS);
            context.assertFalse(wp.getChild("rebuild").canUse(op), "A denied node overrides operator status.");
            context.assertTrue(wp.getChild("prompt").canUse(op) && wp.getChild("undo").canUse(op),
                    "Operators keep commands whose nodes are unset.");
        } finally {
            GRANTS.remove(builder.getUUID());
            GRANTS.remove(operator.getUUID());
        }
        context.succeed();
    }
}
