package dev.worldprompt;

import dev.worldprompt.kernel.RegionBounds;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import net.minecraft.SharedConstants;
import net.minecraft.commands.arguments.blocks.BlockStateParser;
import net.minecraft.core.BlockPos;
import net.minecraft.nbt.CompoundTag;
import net.minecraft.resources.ResourceKey;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.Level;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.entity.BlockEntity;
import net.minecraft.world.level.block.state.BlockState;

/** Frozen after capture; kept in memory with its edit in the player's undo stack, for undo and rebuild. */
final class RegionSnapshot {
    /**
     * Placement flags: normal neighbor and client updates, without block-entity removal
     * side effects. Overwriting a chest or a lectern therefore does not drop its items
     * or book into the world, so undo restoring the snapshot cannot duplicate them.
     */
    static final int PLACE_FLAGS = Block.UPDATE_ALL | Block.UPDATE_SKIP_BLOCK_ENTITY_SIDEEFFECTS;
    final ResourceKey<Level> dimension;
    final RegionBounds bounds;
    final List<BlockState> palette = new ArrayList<>();
    /** Palette lookup used only while capturing; released once the last block is read. */
    private Map<BlockState, Integer> paletteIds = new HashMap<>();
    final int[] blocks;
    final Map<Integer, CompoundTag> blockEntities = new HashMap<>();
    final PromptLecterns lecterns;
    int captured;

    RegionSnapshot(ServerLevel level, RegionBounds bounds) {
        dimension = level.dimension(); this.bounds = bounds; blocks = new int[bounds.volume()];
        lecterns = new PromptLecterns(bounds);
    }
    BlockPos position(int index) { return new BlockPos(bounds.x(index), bounds.y(index), bounds.z(index)); }
    boolean capture(ServerLevel level, int budget) {
        for (int stop = Math.min(blocks.length, captured + budget); captured < stop; captured++) {
            BlockPos pos = position(captured);
            if (!level.hasChunkAt(pos)) throw new IllegalStateException("Selection contains unloaded chunks. Keep the region loaded and retry.");
            BlockState state = level.getBlockState(pos);
            blocks[captured] = paletteIds.computeIfAbsent(state, key -> { palette.add(key); return palette.size() - 1; });
            BlockEntity entity = level.getBlockEntity(pos);
            if (entity != null) {
                blockEntities.put(captured, entity.saveWithFullMetadata(level.registryAccess()));
                lecterns.inspect(pos, entity);
            }
        }
        if (captured == blocks.length) paletteIds = null;
        return captured == blocks.length;
    }
    Map<String, Object> transport() {
        return Map.of("dimensions", new int[]{bounds.width(), bounds.height(), bounds.length()},
                "palette", palette.stream().map(BlockStateParser::serialize).toList(), "blocks", blocks,
                "data_version", SharedConstants.getCurrentVersion().dataVersion().version());
    }
    void restoreState(ServerLevel level, int index) {
        BlockPos pos = position(index);
        if (!level.hasChunkAt(pos)) throw new IllegalStateException("Undo region contains unloaded chunks. Load it and retry /wp undo.");
        level.removeBlockEntity(pos);
        BlockState state = palette.get(blocks[index]);
        boolean changed = level.setBlock(pos, state, 3);
        if (!changed && !level.getBlockState(pos).equals(state))
            throw new IllegalStateException("Minecraft refused an undo write at " + pos.toShortString());
    }
    void restoreEntity(ServerLevel level, int index) {
        CompoundTag saved = blockEntities.get(index);
        if (saved == null) return;
        BlockPos pos = position(index);
        if (!level.hasChunkAt(pos)) throw new IllegalStateException("Undo region unloaded while restoring block entities. Load it and retry /wp undo.");
        BlockEntity entity = BlockEntity.loadStatic(pos, palette.get(blocks[index]), saved.copy(), level.registryAccess());
        if (entity == null) throw new IllegalStateException("Could not restore block entity at " + pos.toShortString());
        level.setBlockEntity(entity);
        entity.setChanged();
        level.sendBlockUpdated(pos, level.getBlockState(pos), level.getBlockState(pos), 3);
    }
    /**
     * Refuse before the first write unless every chunk of the region is loaded, so a restore
     * cannot stop halfway and leave a mix of old and new blocks.
     */
    void ensureLoaded(ServerLevel level) {
        for (int chunkX = bounds.minX() >> 4; chunkX <= bounds.maxX() >> 4; chunkX++)
            for (int chunkZ = bounds.minZ() >> 4; chunkZ <= bounds.maxZ() >> 4; chunkZ++)
                if (!level.hasChunkAt(new BlockPos(chunkX << 4, bounds.minY(), chunkZ << 4)))
                    throw new IllegalStateException("The region contains unloaded chunks (first missing: chunk " + chunkX + ", " + chunkZ
                            + "). Stay near it so it loads, then retry; nothing was changed.");
    }
    /** Restore in this server tick, with no animation or per-operation delay. */
    void restore(ServerLevel level) {
        if (captured != blocks.length) throw new IllegalStateException("Snapshot capture is incomplete.");
        for (int index = 0; index < blocks.length; index++) restoreState(level, index);
        for (int index : blockEntities.keySet()) restoreEntity(level, index);
    }
}
