package dev.worldprompt;

import java.util.Collection;
import java.util.Collections;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.resources.Identifier;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.server.permissions.Permissions;
import net.minecraft.server.players.NameAndId;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.server.permission.PermissionAPI;
import net.neoforged.neoforge.server.permission.events.PermissionGatherEvent;
import net.neoforged.neoforge.server.permission.handler.IPermissionHandler;
import net.neoforged.neoforge.server.permission.nodes.PermissionDynamicContext;
import net.neoforged.neoforge.server.permission.nodes.PermissionNode;
import net.neoforged.neoforge.server.permission.nodes.PermissionTypes;

/** In-memory permission provider for the NeoForge GameTests, selected by the test server config. */
@EventBusSubscriber(modid = "worldprompt")
public final class NeoForgeTestPermissionHandler implements IPermissionHandler {
    public static final Identifier IDENTIFIER = Identifier.fromNamespaceAndPath("worldprompt", "gametest");

    private final Set<PermissionNode<?>> registeredNodes;

    public NeoForgeTestPermissionHandler(Collection<PermissionNode<?>> permissions) {
        this.registeredNodes = Collections.unmodifiableSet(new HashSet<>(permissions));
    }

    @SubscribeEvent
    public static void register(PermissionGatherEvent.Handler event) {
        event.addPermissionHandler(IDENTIFIER, NeoForgeTestPermissionHandler::new);
    }

    /** Exercises NeoForge's actual registered nodes with no dynamic fallback context. */
    public static void verifyNativeDefaults(GameTestHelper context) {
        var server = context.getLevel().getServer();
        PermissionNode<Boolean> open = booleanNode("worldprompt.command.info");
        PermissionNode<Boolean> moderator = booleanNode("worldprompt.command.undo");

        ServerPlayer ordinary = context.makeMockServerPlayerInLevel();
        context.assertTrue(PermissionAPI.getPermission(ordinary, open),
                "NeoForge permission API keeps open nodes available without dynamic context.");
        context.assertFalse(PermissionAPI.getPermission(ordinary, moderator),
                "NeoForge permission API applies the non-operator default without dynamic context.");

        UUID offline = UUID.randomUUID();
        context.assertTrue(PermissionAPI.getOfflinePermission(offline, open),
                "NeoForge offline permission API keeps open nodes available without dynamic context.");
        context.assertFalse(PermissionAPI.getOfflinePermission(offline, moderator),
                "NeoForge offline permission API denies an unset moderator node without dynamic context.");

        ServerPlayer operator = context.makeMockServerPlayerInLevel();
        NameAndId operatorId = new NameAndId(operator.getGameProfile());
        server.getPlayerList().op(operatorId);
        try {
            boolean sourceModerator = operator.createCommandSourceStack().permissions()
                    .hasPermission(Permissions.COMMANDS_MODERATOR);
            context.assertTrue(PermissionAPI.getPermission(operator, moderator) == sourceModerator,
                    "NeoForge permission API matches the player's actual moderator privilege without dynamic context.");
        } finally {
            server.getPlayerList().deop(operatorId);
        }
    }

    private static PermissionNode<Boolean> booleanNode(String name) {
        for (PermissionNode<?> node : PermissionAPI.getRegisteredNodes()) {
            if (!node.getNodeName().equals(name)) {
                continue;
            }
            if (node.getType() != PermissionTypes.BOOLEAN) {
                throw new AssertionError("Expected a Boolean permission node: " + name);
            }
            @SuppressWarnings("unchecked")
            PermissionNode<Boolean> booleanNode = (PermissionNode<Boolean>) node;
            return booleanNode;
        }
        throw new AssertionError("Permission API did not register node " + name);
    }

    @Override
    public Identifier getIdentifier() {
        return IDENTIFIER;
    }

    @Override
    public Set<PermissionNode<?>> getRegisteredNodes() {
        return registeredNodes;
    }

    @Override
    public <T> T getPermission(ServerPlayer player, PermissionNode<T> node, PermissionDynamicContext<?>... context) {
        if (!registeredNodes.contains(node)) {
            throw new IllegalArgumentException("Unregistered test permission node: " + node.getNodeName());
        }
        Boolean explicit = PermissionNodeGameTest.GRANTS
                .getOrDefault(player.getUUID(), Map.of())
                .get(node.getNodeName());
        if (explicit != null) {
            @SuppressWarnings("unchecked")
            T decision = (T) explicit;
            return decision;
        }
        return node.getDefaultResolver().resolve(player, player.getUUID(), context);
    }

    @Override
    public <T> T getOfflinePermission(UUID player, PermissionNode<T> node, PermissionDynamicContext<?>... context) {
        if (!registeredNodes.contains(node)) {
            throw new IllegalArgumentException("Unregistered test permission node: " + node.getNodeName());
        }
        Boolean explicit = PermissionNodeGameTest.GRANTS.getOrDefault(player, Map.of()).get(node.getNodeName());
        if (explicit != null) {
            @SuppressWarnings("unchecked")
            T decision = (T) explicit;
            return decision;
        }
        return node.getDefaultResolver().resolve(null, player, context);
    }
}
