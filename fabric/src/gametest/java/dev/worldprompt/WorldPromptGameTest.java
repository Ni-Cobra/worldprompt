package dev.worldprompt;

import dev.worldprompt.kernel.RegionBounds;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.StairBlock;
import net.minecraft.world.level.block.entity.ChestBlockEntity;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.Half;

/** Runs against an actual disposable Minecraft level, with no model calls. */
public final class WorldPromptGameTest {
    @GameTest
    public void snapshotRestoresDirectionalStatesAndChestData(GameTestHelper context) {
        ServerLevel level = context.getLevel();
        BlockPos low = context.absolutePos(new BlockPos(1, 1, 1));
        BlockPos high = low.offset(2, 1, 1);
        RegionBounds bounds = RegionBounds.between(
                high.getX(), high.getY(), high.getZ(),
                low.getX(), low.getY(), low.getZ(), 12);
        context.assertTrue(bounds.volume() == 12, "Selection includes both corners.");
        RegionSnapshot snapshot = new RegionSnapshot(level, bounds);
        for (int i = 0; i < bounds.volume(); i++)
            level.setBlock(snapshot.position(i), Blocks.AIR.defaultBlockState(), 3);

        BlockState stair = Blocks.OAK_STAIRS.defaultBlockState()
                .setValue(StairBlock.FACING, Direction.EAST).setValue(StairBlock.HALF, Half.TOP);
        level.setBlock(low, stair, 3);
        BlockPos chestPos = snapshot.position(2);
        level.setBlock(chestPos, Blocks.CHEST.defaultBlockState(), 3);
        ChestBlockEntity chest = (ChestBlockEntity) level.getBlockEntity(chestPos);
        context.assertTrue(chest != null, "Chest block entity exists before capture.");
        chest.setItem(0, new ItemStack(Items.DIAMOND, 7));
        context.assertFalse(snapshot.capture(level, 1), "One-block capture is budgeted.");
        context.assertTrue(snapshot.capture(level, 100), "Capture completes with remaining budget.");
        context.assertTrue(snapshot.blockEntities.size() == 1, "One chest is captured.");

        chest.setItem(0, new ItemStack(Items.DIRT, 3));
        for (int i = 0; i < bounds.volume(); i++)
            level.setBlock(snapshot.position(i), Blocks.STONE.defaultBlockState(), 3);
        snapshot.restore(level);

        context.assertTrue(level.getBlockState(low).equals(stair),
                "Undo preserves directional state and upper stair half.");
        ChestBlockEntity restored = (ChestBlockEntity) level.getBlockEntity(chestPos);
        context.assertTrue(restored != null && restored.getItem(0).is(Items.DIAMOND)
                && restored.getItem(0).getCount() == 7,
                "Undo restores captured inventory, isolated from later chest changes.");
        context.assertTrue(level.getBlockState(snapshot.position(11)).isAir(),
                "Undo restores the inclusive far corner's original air.");
        context.succeed();
    }
}
