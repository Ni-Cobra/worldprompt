package dev.worldprompt;

import net.minecraft.core.registries.Registries;
import net.minecraft.resources.Identifier;
import net.minecraft.resources.ResourceKey;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.state.BlockBehaviour;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.StateDefinition;
import net.minecraft.world.level.block.state.properties.BooleanProperty;
import net.minecraft.world.level.block.state.properties.EnumProperty;
import net.minecraft.util.StringRepresentable;

/** Registered only by test-loader setup, so a vanilla-only capture cannot accidentally satisfy the tests. */
public final class CatalogFixtureBlock extends Block {
    public static final Identifier ID = Identifier.fromNamespaceAndPath("worldprompt-test", "catalog_probe");
    public static final BooleanProperty POWERED = BooleanProperty.create("powered");
    public static final EnumProperty<Label> LABEL = EnumProperty.create("label", Label.class);

    public enum Label implements StringRepresentable {
        AMBER("amber"), BLUE("blue");
        private final String name;
        Label(String name) { this.name = name; }
        @Override public String getSerializedName() { return name; }
    }

    public CatalogFixtureBlock() {
        super(BlockBehaviour.Properties.of().setId(ResourceKey.create(Registries.BLOCK, ID)));
        registerDefaultState(defaultBlockState().setValue(POWERED, false).setValue(LABEL, Label.AMBER));
    }

    @Override
    protected void createBlockStateDefinition(StateDefinition.Builder<Block, BlockState> builder) {
        builder.add(POWERED, LABEL);
    }
}
