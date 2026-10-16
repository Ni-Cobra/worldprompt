package dev.worldprompt;

import java.util.List;
import java.util.UUID;
import net.minecraft.world.level.block.state.BlockState;

/** An edit that wrote blocks: what /wp undo, /wp rebuild and /wp book need, retained in the player's history. */
final class AppliedEdit {
    final String id;
    final UUID owner;
    final String ownerName;
    final RegionSnapshot snapshot;
    final PromptRequest prompt;
    final ReplayPayload replay;
    final String modelLabel;
    final List<BlockState> palette;
    /**
     * IDs of the edits standing in this region when the snapshot was captured; they must outlive this edit.
     * IDs rather than edit objects, so an edit evicted from the history frees its snapshot and replay.
     */
    final List<String> base;
    /** Earlier job IDs this edit was rebuilt from. */
    final List<String> aliases;
    /** True once the original snapshot is restored, i.e. the edit is undone. */
    boolean restored;

    AppliedEdit(String id, UUID owner, String ownerName, RegionSnapshot snapshot, PromptRequest prompt, ReplayPayload replay,
                List<BlockState> palette, String modelLabel, List<String> base, List<String> aliases) {
        this.id = id; this.owner = owner; this.ownerName = ownerName; this.snapshot = snapshot; this.prompt = prompt;
        this.replay = replay; this.palette = palette; this.modelLabel = modelLabel;
        this.base = List.copyOf(base); this.aliases = List.copyOf(aliases);
    }
}
