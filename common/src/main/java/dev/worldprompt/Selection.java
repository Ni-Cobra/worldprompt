package dev.worldprompt;

import dev.worldprompt.kernel.RegionBounds;
import net.minecraft.core.BlockPos;
import net.minecraft.resources.ResourceKey;
import net.minecraft.world.level.Level;

/** A player's /wp pos1 and pos2 corners; either may still be unset. */
record Selection(ResourceKey<Level> dimension, BlockPos a, BlockPos b) {
    boolean complete() { return a != null && b != null; }

    /** The inclusive region between both corners, limited to {@code maxVolume} blocks. */
    RegionBounds bounds(int maxVolume) {
        return RegionBounds.between(a.getX(), a.getY(), a.getZ(), b.getX(), b.getY(), b.getZ(), maxVolume);
    }
}
