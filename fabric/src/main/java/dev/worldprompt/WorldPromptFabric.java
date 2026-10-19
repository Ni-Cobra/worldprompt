package dev.worldprompt;

import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.fabric.api.networking.v1.ServerPlayConnectionEvents;
import net.fabricmc.loader.api.FabricLoader;

/** Fabric's native events bind the shared host; all commands and features are shared. */
public final class WorldPromptFabric implements ModInitializer {
    @Override public void onInitialize() {
        WorldPrompt.permissions(new dev.worldprompt.mycelium.bridges.command_authorization.CommandPermissions(
                new dev.worldprompt.mycelium.bridges.command_authorization.FabricPermissions()));
        ServerLifecycleEvents.SERVER_STARTED.register(server ->
                WorldPrompt.started(server, FabricLoader.getInstance().getConfigDir()));
        ServerLifecycleEvents.SERVER_STOPPING.register(WorldPrompt::stopping);
        ServerTickEvents.END_SERVER_TICK.register(WorldPrompt::tick);
        ServerPlayConnectionEvents.DISCONNECT.register((handler, server) ->
                WorldPrompt.disconnected(handler.player.getUUID()));
        CommandRegistrationCallback.EVENT.register((dispatcher, registry, environment) ->
                WorldPrompt.register(dispatcher));
    }
}
