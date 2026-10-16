package dev.worldprompt;

import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.fabric.api.networking.v1.ServerPlayConnectionEvents;
import net.minecraft.commands.Commands;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.commands.arguments.coordinates.BlockPosArgument;
import net.minecraft.network.chat.Component;
import net.minecraft.server.permissions.Permissions;
import com.mojang.brigadier.arguments.IntegerArgumentType;
import com.mojang.brigadier.arguments.StringArgumentType;
import org.slf4j.LoggerFactory;

public final class WorldPrompt implements ModInitializer {
    static final String PROJECT_URL = "https://github.com/Ni-Cobra/worldprompt";
    static PromptManager manager;
    @Override public void onInitialize() {
        ServerLifecycleEvents.SERVER_STARTED.register(server -> {
            try {
                manager = new PromptManager(server, AdapterConfig.load());
            }
            catch (Exception e) { LoggerFactory.getLogger("worldprompt").error("WorldPrompt configuration failed", e); }
        });
        ServerLifecycleEvents.SERVER_STOPPING.register(server -> { if (manager != null) { manager.close(); manager = null; } });
        ServerTickEvents.END_SERVER_TICK.register(server -> { if (manager != null) manager.tick(); });
        ServerPlayConnectionEvents.DISCONNECT.register((handler, server) -> { if (manager != null) manager.disconnected(handler.player.getUUID()); });
        CommandRegistrationCallback.EVENT.register((dispatcher, registryAccess, environment) -> {
            var root = Commands.literal("wp");
            for (boolean first : new boolean[]{true, false}) root.then(Commands.literal(first ? "pos1" : "pos2").requires(allowed(first ? "pos1" : "pos2", false))
                    .executes(context -> run(context.getSource(), m -> m.corner(context.getSource().getPlayerOrException(), first,
                            context.getSource().getPlayerOrException().blockPosition())))
                    .then(Commands.argument("position", BlockPosArgument.blockPos()).executes(context -> run(context.getSource(),
                            m -> m.corner(context.getSource().getPlayerOrException(), first, BlockPosArgument.getBlockPos(context, "position"))))));
            root.then(Commands.literal("select").requires(allowed("select", false))
                    .executes(context -> run(context.getSource(), m -> m.selectLookedAt(context.getSource().getPlayerOrException())))
                    .then(Commands.argument("sizeX", IntegerArgumentType.integer(1))
                    .then(Commands.argument("sizeY", IntegerArgumentType.integer(1))
                    .then(Commands.argument("sizeZ", IntegerArgumentType.integer(1)).executes(context -> run(context.getSource(),
                            m -> m.selectLookedAt(context.getSource().getPlayerOrException(),
                                    IntegerArgumentType.getInteger(context, "sizeX"),
                                    IntegerArgumentType.getInteger(context, "sizeY"),
                                    IntegerArgumentType.getInteger(context, "sizeZ"))))))));
            var prompt = Commands.literal("prompt").requires(allowed("prompt", true))
                    .then(Commands.argument("text", StringArgumentType.greedyString())
                    .executes(context -> run(context.getSource(), m -> m.submit(context.getSource().getPlayerOrException(),
                            StringArgumentType.getString(context, "text")))));
            for (String effort : new String[]{"single", "double"})
                prompt.then(Commands.literal(effort).then(Commands.argument("text", StringArgumentType.greedyString())
                        .executes(context -> run(context.getSource(), m -> m.submit(context.getSource().getPlayerOrException(),
                                StringArgumentType.getString(context, "text"), effort)))));
            root.then(prompt);
            root.then(Commands.literal("info").requires(allowed("info", false)).executes(context -> run(context.getSource(), m -> m.info(context.getSource()))));
            root.then(Commands.literal("delay").requires(allowed("delay", false)).then(Commands.argument("ticks", IntegerArgumentType.integer(0, 72_000))
                    .executes(context -> run(context.getSource(), m -> m.delay(context.getSource().getPlayerOrException(),
                            IntegerArgumentType.getInteger(context, "ticks"))))));
            // Each takes an optional short or full job ID; without one it targets the newest job or edit.
            root.then(Commands.literal("undo").requires(allowed("undo", true))
                    .executes(context -> run(context.getSource(), m -> m.undo(context.getSource().getPlayerOrException())))
                    .then(Commands.argument("id", StringArgumentType.word()).suggests((context, builder) -> suggestIds(context.getSource(), builder, false))
                    .executes(context -> run(context.getSource(), m -> m.undo(context.getSource().getPlayerOrException(),
                            StringArgumentType.getString(context, "id"))))));
            root.then(Commands.literal("cancel").requires(allowed("cancel", false)).executes(context -> run(context.getSource(), m -> m.cancel(context.getSource(), null)))
                    .then(Commands.argument("id", StringArgumentType.word()).suggests((context, builder) -> suggestIds(context.getSource(), builder, true))
                    .executes(context -> run(context.getSource(), m -> m.cancel(context.getSource(),
                            StringArgumentType.getString(context, "id"))))));
            root.then(Commands.literal("rebuild").requires(allowed("rebuild", true))
                    .executes(context -> run(context.getSource(), m -> m.rebuild(context.getSource().getPlayerOrException())))
                    .then(Commands.argument("id", StringArgumentType.word()).suggests((context, builder) -> suggestIds(context.getSource(), builder, false))
                    .executes(context -> run(context.getSource(), m -> m.rebuild(context.getSource().getPlayerOrException(),
                            StringArgumentType.getString(context, "id"))))));
            root.then(Commands.literal("book").requires(allowed("book", false)).executes(context -> run(context.getSource(), m -> m.book(context.getSource().getPlayerOrException(), null)))
                    .then(Commands.argument("id", StringArgumentType.word()).suggests((context, builder) -> suggestIds(context.getSource(), builder, false))
                    .executes(context -> run(context.getSource(), m -> m.book(context.getSource().getPlayerOrException(),
                            StringArgumentType.getString(context, "id"))))));
            root.then(Commands.literal("jobs").requires(allowed("jobs", false)).executes(context -> run(context.getSource(), m -> m.jobs(context.getSource(), 1)))
                    .then(Commands.argument("page", IntegerArgumentType.integer(1)).executes(context -> run(context.getSource(),
                            m -> m.jobs(context.getSource(), IntegerArgumentType.getInteger(context, "page"))))));
            var node = dispatcher.register(root);
            dispatcher.register(Commands.literal("worldprompt").redirect(node));
        });
    }
    /** Node prefix; each subcommand checks {@code worldprompt.command.<name>}. */
    static final String PERMISSION_PREFIX = "worldprompt.command.";
    /**
     * A permissions mod such as LuckPerms decides when it sets the node. Otherwise prompt, undo
     * and rebuild keep requiring operator (moderator) permission and the other commands stay open.
     */
    static java.util.function.Predicate<CommandSourceStack> allowed(String command, boolean operatorDefault) {
        String node = PERMISSION_PREFIX + command;
        return source -> me.lucko.fabric.api.permissions.v0.Permissions.getPermissionValue(source, node)
                .orElseGet(() -> !operatorDefault || source.permissions().hasPermission(Permissions.COMMANDS_MODERATOR));
    }
    private static int run(CommandSourceStack source, Action action) {
        try {
            if (manager == null) {
                source.sendFailure(Feedback.message("WorldPrompt is unavailable; check server configuration/logs.", net.minecraft.ChatFormatting.RED));
                source.sendSystemMessage(Feedback.setupHelp());
                return 0;
            }
            action.run(manager); return 1;
        } catch (Exception e) { source.sendFailure(Feedback.message(e.getMessage(), net.minecraft.ChatFormatting.RED)); return 0; }
    }
    /** Suggest the player's own short job IDs: running jobs for cancel, retained edits otherwise. */
    private static java.util.concurrent.CompletableFuture<com.mojang.brigadier.suggestion.Suggestions> suggestIds(
            CommandSourceStack source, com.mojang.brigadier.suggestion.SuggestionsBuilder builder, boolean jobs) {
        PromptManager current = manager;
        var player = source.getPlayer();
        if (current != null && player != null)
            for (String id : jobs ? current.ownJobs(player.getUUID()).stream().map(job -> job.id).toList()
                    : current.editIds(player.getUUID()))
                if (id.startsWith(builder.getRemainingLowerCase())) builder.suggest(id.substring(0, 8));
        return builder.buildFuture();
    }
    @FunctionalInterface private interface Action { void run(PromptManager manager) throws Exception; }
}
