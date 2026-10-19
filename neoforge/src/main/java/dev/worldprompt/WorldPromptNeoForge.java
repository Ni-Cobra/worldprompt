package dev.worldprompt;

import dev.worldprompt.kernel.CommandPolicy;

import dev.worldprompt.mycelium.bridges.command_authorization.CommandPermissions;
import dev.worldprompt.mycelium.bridges.command_authorization.NeoForgePermissions;
import net.neoforged.fml.common.Mod;
import net.neoforged.fml.loading.FMLPaths;
import net.neoforged.neoforge.common.NeoForge;
import net.neoforged.neoforge.event.RegisterCommandsEvent;
import net.neoforged.neoforge.event.entity.player.PlayerEvent;
import net.neoforged.neoforge.event.server.ServerStartedEvent;
import net.neoforged.neoforge.event.server.ServerStoppingEvent;
import net.neoforged.neoforge.event.tick.ServerTickEvent;
import net.neoforged.neoforge.server.permission.events.PermissionGatherEvent;

/** Native NeoForge binding of the same command tree, feature host and authorization contract. */
@Mod("worldprompt")
public final class WorldPromptNeoForge {
    public WorldPromptNeoForge() {
        var permissions = NeoForgePermissions.create(CommandPolicy.permissionDefaults());
        WorldPrompt.permissions(new CommandPermissions(permissions));
        NeoForge.EVENT_BUS.addListener((PermissionGatherEvent.Nodes event) -> permissions.register(event));
        NeoForge.EVENT_BUS.addListener((RegisterCommandsEvent event) -> WorldPrompt.register(event.getDispatcher()));
        NeoForge.EVENT_BUS.addListener((ServerStartedEvent event) ->
                WorldPrompt.started(event.getServer(), FMLPaths.CONFIGDIR.get()));
        NeoForge.EVENT_BUS.addListener((ServerStoppingEvent event) -> WorldPrompt.stopping(event.getServer()));
        NeoForge.EVENT_BUS.addListener((ServerTickEvent.Post event) -> WorldPrompt.tick(event.getServer()));
        NeoForge.EVENT_BUS.addListener((PlayerEvent.PlayerLoggedOutEvent event) ->
                WorldPrompt.disconnected(event.getEntity().getUUID()));
    }
}
