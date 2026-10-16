package dev.worldprompt;

import io.netty.channel.embedded.EmbeddedChannel;
import java.util.ArrayList;
import java.util.List;
import net.minecraft.core.particles.DustParticleOptions;
import net.minecraft.core.particles.ParticleTypes;
import net.minecraft.network.Connection;
import net.minecraft.network.protocol.game.ClientboundLevelParticlesPacket;
import net.minecraft.network.protocol.game.ClientboundSetActionBarTextPacket;
import net.minecraft.network.protocol.game.ClientboundSoundPacket;
import net.minecraft.network.protocol.game.ClientboundSystemChatPacket;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.server.network.ServerCommonPacketListenerImpl;
import net.minecraft.sounds.SoundEvents;
import net.minecraft.sounds.SoundSource;

/**
 * Inspects the actual packets a mock player receives (chat, action bar, sounds, particles), without a
 * visible client or test hooks in production. {@link #drain()} reads what arrived since the last call.
 */
final class Chat {
    private static final String PREFIX = "[WorldPrompt] ";
    final EmbeddedChannel channel;
    /** Chat lines with the mod's "[WorldPrompt] " prefix removed, and action bar texts. */
    final List<String> messages = new ArrayList<>(), overlays = new ArrayList<>();
    final List<ClientboundSoundPacket> bassPackets = new ArrayList<>();
    final List<ClientboundLevelParticlesPacket> poofs = new ArrayList<>();
    boolean coloredChat, coloredOverlay;
    int basses, particles, otherSounds;
    boolean whiteForced = true, lightDust, fullDust;

    Chat(ServerPlayer player) throws Exception {
        var connectionField = ServerCommonPacketListenerImpl.class.getDeclaredField("connection");
        connectionField.setAccessible(true);
        var channelField = Connection.class.getDeclaredField("channel");
        channelField.setAccessible(true);
        channel = (EmbeddedChannel) channelField.get(connectionField.get(player.connection));
        while (channel.readOutbound() != null) { }
    }

    void drain() {
        channel.runPendingTasks();
        Object packet;
        while ((packet = channel.readOutbound()) != null) {
            if (packet instanceof ClientboundSoundPacket sound
                    && sound.getSound().equals(SoundEvents.NOTE_BLOCK_BASEDRUM)
                    && sound.getSource() == SoundSource.BLOCKS) { basses++; bassPackets.add(sound); }
            else if (packet instanceof ClientboundSoundPacket) otherSounds++;
            if (packet instanceof ClientboundSystemChatPacket chat) {
                String content = chat.content().getString();
                if (content.startsWith(PREFIX)) content = content.substring(PREFIX.length());
                if (chat.overlay()) { overlays.add(content); coloredOverlay |= chat.content().getStyle().getColor() != null; }
                else { messages.add(content); coloredChat |= chat.content().getStyle().getColor() != null; }
            }
            if (packet instanceof ClientboundSetActionBarTextPacket bar) {
                overlays.add(bar.text().getString()); coloredOverlay |= bar.text().getStyle().getColor() != null;
            }
            if (packet instanceof ClientboundLevelParticlesPacket particle && particle.particle() == ParticleTypes.POOF)
                poofs.add(particle);
            if (packet instanceof ClientboundLevelParticlesPacket particle
                    && particle.particle() instanceof DustParticleOptions dust) {
                particles++;
                lightDust |= dust.getScale() == 0.4f;
                fullDust |= dust.getScale() == 0.9f;
                whiteForced &= particle.overrideLimiter() && particle.alwaysShow()
                        && dust.getColor().equals(new org.joml.Vector3f(1, 1, 1));
            }
        }
    }
}
