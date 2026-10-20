package dev.worldprompt;

import net.fabricmc.api.ModInitializer;
import net.minecraft.core.Registry;
import net.minecraft.core.registries.BuiltInRegistries;

/** Early Fabric registry setup, separate from the shared GameTest bodies adapted for NeoForge. */
public final class FabricCatalogTestSetup implements ModInitializer {
    @Override
    public void onInitialize() {
        Registry.register(BuiltInRegistries.BLOCK, CatalogFixtureBlock.ID, new CatalogFixtureBlock());
    }
}
