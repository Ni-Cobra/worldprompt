package dev.worldprompt;

import net.minecraft.core.registries.Registries;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.registries.RegisterEvent;

/** Test-only native NeoForge registry setup, in the same development module as the GameTests. */
@EventBusSubscriber(modid = "worldprompt")
public final class NeoForgeCatalogTestSetup {
    @SubscribeEvent
    public static void register(RegisterEvent event) {
        event.register(Registries.BLOCK, helper -> helper.register(CatalogFixtureBlock.ID, new CatalogFixtureBlock()));
    }
}
