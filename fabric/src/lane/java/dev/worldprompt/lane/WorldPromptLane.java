package dev.worldprompt.lane;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.Instant;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.message.v1.ClientReceiveMessageEvents;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Screenshot;
import net.minecraft.client.gui.screens.ConnectScreen;
import net.minecraft.client.gui.screens.TitleScreen;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.client.input.MouseButtonEvent;
import net.minecraft.client.input.MouseButtonInfo;
import net.minecraft.client.multiplayer.ServerData;
import net.minecraft.client.multiplayer.resolver.ServerAddress;
import net.minecraft.core.BlockPos;
import net.minecraft.network.chat.Component;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/** Test-only client controls and observations. Enabled only when worldprompt.lane is set. */
public final class WorldPromptLane implements ClientModInitializer {
    private static final Logger LOG = LoggerFactory.getLogger("worldprompt-lane");
    private static final Gson JSON = new Gson();
    private static Path root;
    private static long commandOffset;
    private static long tick;
    private static boolean onboardingDismissed;
    private static String lastScreen = "";

    @Override public void onInitializeClient() {
        String configured = System.getProperty("worldprompt.lane");
        if (configured == null || configured.isBlank()) return;
        root = Path.of(configured).toAbsolutePath().normalize();
        try {
            Files.createDirectories(root);
            Files.writeString(root.resolve("ready.json"), JSON.toJson(event("ready", null)), StandardCharsets.UTF_8);
        } catch (IOException error) {
            LOG.error("Cannot initialize WorldPrompt lane at {}", root, error);
            root = null;
            return;
        }
        ClientTickEvents.END_CLIENT_TICK.register(client -> {
            tick++;
            Screen currentScreen = client.gui.screen();
            String screenName = currentScreen == null ? "none" : currentScreen.getClass().getName();
            if (!screenName.equals(lastScreen)) { lastScreen = screenName; record("screen", screenName, false); }
            dismissFirstRunOnboarding(client);
            if (tick % 4 == 0) consume(client);
        });
        ClientReceiveMessageEvents.GAME.register((Component message, boolean overlay) -> {
            record("message", message.getString(), overlay);
        });
        record("hook", "enabled", false);
    }

    private static void dismissFirstRunOnboarding(Minecraft client) {
        Screen screen = client.gui.screen();
        if (onboardingDismissed || screen == null || client.level != null
                || !screen.getClass().getSimpleName().endsWith("OnboardingScreen")) return;
        double x = client.getWindow().getGuiScaledWidth() / 2.0;
        double y = client.getWindow().getGuiScaledHeight() - 23.0;
        MouseButtonEvent click = new MouseButtonEvent(x, y, new MouseButtonInfo(0, 0));
        boolean clicked = screen.mouseClicked(click, false);
        screen.mouseReleased(click);
        onboardingDismissed = clicked || client.gui.screen() != screen;
        if (onboardingDismissed) {
            record("onboarding-dismissed", screen.getClass().getName() + " click=" + clicked, false);
            String address = System.getProperty("worldprompt.lane.serverAddress", "").strip();
            if (!address.isEmpty()) {
                ServerData server = new ServerData("WorldPrompt lane", address, ServerData.Type.OTHER);
                ConnectScreen.startConnecting(new TitleScreen(), client, ServerAddress.parseString(address), server, false, null);
                record("connecting", address, false);
            } else {
                record("hook-error", "worldprompt.lane.serverAddress was not set", false);
            }
        }
    }

    private static void consume(Minecraft client) {
        Path commands = root.resolve("commands.jsonl");
        try {
            if (!Files.exists(commands)) return;
            byte[] bytes = Files.readAllBytes(commands);
            if (commandOffset >= bytes.length) return;
            int start = Math.toIntExact(commandOffset);
            int cursor = start;
            while (cursor < bytes.length) {
                int end = cursor;
                while (end < bytes.length && bytes[end] != '\n') end++;
                if (end == bytes.length) break;
                String line = new String(bytes, cursor, end - cursor, StandardCharsets.UTF_8).strip();
                cursor = end + 1;
                if (!line.isEmpty()) execute(client, JSON.fromJson(line, JsonObject.class));
            }
            commandOffset = cursor;
        } catch (Exception error) {
            LOG.warn("WorldPrompt lane command could not be read", error);
            record("hook-error", error.toString(), false);
        }
    }

    private static void execute(Minecraft client, JsonObject command) {
        String action = command.get("action").getAsString();
        String id = command.has("id") ? command.get("id").getAsString() : "";
        try {
            switch (action) {
                case "command" -> {
                    String value = command.get("text").getAsString().strip();
                    if (client.player == null || client.getConnection() == null) throw new IllegalStateException("player is not connected");
                    client.getConnection().sendCommand(value.startsWith("/") ? value.substring(1) : value);
                    record("sent-command", value, false, id);
                }
                case "chat" -> {
                    String value = command.get("text").getAsString();
                    if (client.player == null || client.getConnection() == null) throw new IllegalStateException("player is not connected");
                    client.getConnection().sendChat(value);
                    record("sent-chat", value, false, id);
                }
                case "probe" -> probe(client, command, id);
                case "screenshot" -> {
                    String name = command.get("name").getAsString().replaceAll("[^A-Za-z0-9_-]", "_");
                    Files.createDirectories(root.resolve("screenshots"));
                    Screenshot.grab(root.toFile(), name, client.gameRenderer.mainRenderTarget(), 1,
                            message -> record("screenshot", message.getString(), false, id));
                }
                case "disconnect" -> {
                    if (client.getConnection() != null)
                        client.getConnection().getConnection().disconnect(Component.literal("WorldPrompt lane disconnect"));
                    client.disconnect(new TitleScreen(), false);
                    record("disconnected", "client requested disconnect", false, id);
                }
                default -> throw new IllegalArgumentException("unknown action: " + action);
            }
        } catch (Exception error) {
            record("hook-error", error.toString(), false, id);
        }
    }

    private static void probe(Minecraft client, JsonObject command, String id) {
        JsonObject result = event("probe", command.has("name") ? command.get("name").getAsString() : "", false, id);
        if (client.player != null) {
            result.addProperty("player", client.player.getGameProfile().name());
            result.addProperty("x", client.player.getX());
            result.addProperty("y", client.player.getY());
            result.addProperty("z", client.player.getZ());
            result.addProperty("yaw", client.player.getYRot());
            result.addProperty("pitch", client.player.getXRot());
        }
        if (client.level != null && command.has("x")) {
            BlockPos pos = new BlockPos(command.get("x").getAsInt(), command.get("y").getAsInt(), command.get("z").getAsInt());
            result.addProperty("block", client.level.getBlockState(pos).toString());
            result.addProperty("block_pos", pos.toShortString());
        }
        append(result);
    }

    private static void record(String type, String text, boolean overlay) { record(type, text, overlay, ""); }

    private static void record(String type, String text, boolean overlay, String id) {
        JsonObject value = event(type, text, overlay, id);
        if (Minecraft.getInstance().player != null) {
            value.addProperty("player_x", Minecraft.getInstance().player.getX());
            value.addProperty("player_y", Minecraft.getInstance().player.getY());
            value.addProperty("player_z", Minecraft.getInstance().player.getZ());
        }
        append(value);
    }

    private static JsonObject event(String type, String text) { return event(type, text, false, ""); }

    private static JsonObject event(String type, String text, boolean overlay, String id) {
        JsonObject value = new JsonObject();
        value.addProperty("at", Instant.now().toString());
        value.addProperty("tick", tick);
        value.addProperty("type", type);
        if (text != null) value.addProperty("text", text);
        value.addProperty("overlay", overlay);
        if (!id.isEmpty()) value.addProperty("command_id", id);
        return value;
    }

    private static void append(JsonObject value) {
        if (root == null) return;
        try {
            Files.writeString(root.resolve("events.jsonl"), JSON.toJson(value) + "\n", StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (IOException error) { LOG.warn("Cannot record WorldPrompt lane event", error); }
    }
}
