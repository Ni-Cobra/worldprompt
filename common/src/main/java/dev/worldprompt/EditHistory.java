package dev.worldprompt;

import dev.worldprompt.kernel.RegionBounds;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Deque;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import net.minecraft.resources.ResourceKey;
import net.minecraft.world.level.Level;

/**
 * Who owns which region: every player's applied edits (the /wp undo stack, newest first), the
 * {@link AppliedEdit#base} links between edits stacked over one another, and the unfinished jobs
 * whose regions stay reserved until they end. Server tick thread only.
 */
final class EditHistory {
    private final AdapterConfig config;
    /** Running jobs by job ID, in submission order; a player may own several. */
    final Map<String, Job> active = new LinkedHashMap<>();
    /** Stopping jobs whose snapshot was already restored locally, awaiting Core's acknowledgement. */
    final Map<String, Job> cleanup = new LinkedHashMap<>();
    /** Each player's applied edits, newest first. */
    private final Map<UUID, Deque<AppliedEdit>> stacks = new HashMap<>();

    EditHistory(AdapterConfig config) { this.config = config; }

    Deque<AppliedEdit> history(UUID owner) {
        return stacks.computeIfAbsent(owner, key -> new ArrayDeque<>());
    }
    /** The player's retained edit IDs, newest first, without creating an empty stack. */
    List<String> editIds(UUID owner) {
        return stacks.getOrDefault(owner, new ArrayDeque<>()).stream().map(edit -> edit.id).toList();
    }
    void clear() { active.clear(); cleanup.clear(); stacks.clear(); }

    /** Unfinished jobs of one player, oldest submission first. */
    List<Job> ownJobs(UUID owner) {
        List<Job> jobs = new ArrayList<>();
        for (Job job : active.values()) if (job.owner.equals(owner) && job.mode != Job.Mode.DONE) jobs.add(job);
        for (Job job : cleanup.values()) if (job.owner.equals(owner) && job.mode != Job.Mode.DONE) jobs.add(job);
        jobs.sort(Comparator.comparingLong(job -> job.order));
        return jobs;
    }
    /** Every unfinished job reserves its region, including the requesting player's own jobs. */
    void ensureRegionFree(ResourceKey<Level> dimension, RegionBounds bounds, UUID owner, boolean restoring) {
        for (Job job : active.values()) checkRegionConflict(job, dimension, bounds, owner, restoring);
        for (Job job : cleanup.values()) checkRegionConflict(job, dimension, bounds, owner, restoring);
    }
    private static void checkRegionConflict(Job job, ResourceKey<Level> dimension, RegionBounds bounds, UUID owner, boolean restoring) {
        if (job.mode == Job.Mode.DONE || !job.snapshot.dimension.equals(dimension) || !job.snapshot.bounds.intersects(bounds)) return;
        boolean own = job.owner.equals(owner);
        // A player's own stopping job never writes again, so its owner may still undo locally.
        if (restoring && own && job.cancelling) return;
        throw new IllegalArgumentException("Selection intersects " + (own ? "your " : "") + "active WorldPrompt job "
                + Feedback.shortId(job.id) + ". Wait for it to finish or cancel it first.");
    }

    private static boolean idMatches(String query, String id) {
        return id.equalsIgnoreCase(query) || Feedback.shortId(id).equalsIgnoreCase(query);
    }
    Job findJob(UUID owner, String query) {
        List<Job> matches = ownJobs(owner).stream().filter(job -> idMatches(query, job.id)).toList();
        if (matches.size() > 1) throw new IllegalArgumentException("Job ID " + query + " is ambiguous; use the full ID from /wp jobs.");
        if (matches.isEmpty()) throw new IllegalArgumentException("You have no active WorldPrompt job " + query + ". Use /wp jobs to list your jobs.");
        return matches.getFirst();
    }
    /** Accepts an edit's own job ID or the ID of any earlier job it was rebuilt from. */
    AppliedEdit findEdit(UUID owner, String query) {
        List<AppliedEdit> matches = history(owner).stream().filter(edit -> idMatches(query, edit.id)
                || edit.aliases.stream().anyMatch(alias -> idMatches(query, alias))).toList();
        if (matches.size() > 1) throw new IllegalArgumentException("Edit ID " + query + " is ambiguous; use the full job ID.");
        if (matches.isEmpty()) throw new IllegalArgumentException("You have no retained WorldPrompt edit " + query + ".");
        return matches.getFirst();
    }
    /** The newest edit whose job has stopped writing; undo also skips edits that are already undone. */
    AppliedEdit newestEdit(UUID owner, boolean forUndo, String command) {
        boolean building = false;
        for (AppliedEdit edit : history(owner)) {
            if (forUndo && edit.restored) continue;
            if (building(edit)) { building = true; continue; }
            return edit;
        }
        if (building) throw new IllegalArgumentException("Your newest WorldPrompt edit is still building. Use /wp cancel <id> first, or "
                + command + " <id> for another edit.");
        throw new IllegalArgumentException(forUndo ? (history(owner).isEmpty() ? "No WorldPrompt snapshot is available for /wp undo."
                : "Every retained WorldPrompt edit is already undone. Use /wp rebuild <id> to replay one.")
                : "No applied WorldPrompt plan is available for /wp rebuild.");
    }
    /** True while the edit's own job is still writing blocks (a stopping job no longer is). */
    boolean building(AppliedEdit edit) {
        Job job = active.get(edit.id);
        return job != null && !job.cancelling && job.mode != Job.Mode.DONE;
    }
    /** Edits still standing (not undone) in a region, from every player's history. */
    List<AppliedEdit> liveEdits(ResourceKey<Level> dimension, RegionBounds bounds, AppliedEdit except) {
        List<AppliedEdit> edits = new ArrayList<>();
        for (Deque<AppliedEdit> stack : stacks.values()) for (AppliedEdit edit : stack)
            if (edit != except && !edit.restored && edit.snapshot.dimension.equals(dimension) && edit.snapshot.bounds.intersects(bounds))
                edits.add(edit);
        return List.copyOf(edits);
    }
    private static String describe(AppliedEdit edit, UUID viewer) {
        return (viewer.equals(edit.owner) ? "Your edit " : edit.ownerName + "'s edit ") + Feedback.shortId(edit.id);
    }
    /**
     * Restoring an edit's snapshot would erase a later edit captured on top of it, and undoing that
     * later edit afterwards would bring the earlier blocks back. Require newest-first order instead.
     */
    void ensureNoDependents(AppliedEdit edit, UUID viewer) {
        for (Deque<AppliedEdit> stack : stacks.values()) for (AppliedEdit other : stack)
            if (other != edit && !other.restored && other.base.contains(edit.id))
                throw new IllegalArgumentException(describe(other, viewer) + " was built later over part of " + Feedback.shortId(edit.id)
                        + ". Undo " + Feedback.shortId(other.id) + " first.");
    }
    /** Replaying an undone edit is safe only while its region holds exactly the edits it was captured over. */
    void ensureRebuildBase(AppliedEdit edit, UUID viewer) {
        for (AppliedEdit other : liveEdits(edit.snapshot.dimension, edit.snapshot.bounds, edit))
            if (!edit.base.contains(other.id))
                throw new IllegalArgumentException(describe(other, viewer) + " changed this region after " + Feedback.shortId(edit.id)
                        + " was undone. Undo " + Feedback.shortId(other.id) + " first.");
        for (String dependencyId : edit.base) {
            // An edit that left the history can no longer be undone, so it counts as committed.
            AppliedEdit dependency = editById(dependencyId);
            if (dependency != null && dependency.restored)
                throw new IllegalArgumentException(Feedback.shortId(edit.id) + " was built over " + Feedback.shortId(dependency.id)
                        + ", which has since been undone or rebuilt, so its saved snapshot no longer matches this region.");
        }
    }
    /** The retained edit with exactly this job ID, or null once it was evicted or replaced by a rebuild. */
    private AppliedEdit editById(String id) {
        for (Deque<AppliedEdit> stack : stacks.values()) for (AppliedEdit edit : stack)
            if (edit.id.equals(id)) return edit;
        return null;
    }

    /** The job's edit enters its owner's stack at START (the first time it may write blocks) and is marked standing. */
    void markApplied(Job job) {
        if (!job.applied) {
            // A rebuild replaces its source edit on the stack; the source's IDs stay usable as aliases.
            AppliedEdit source = job.rebuildFrom != null ? job.edit : null;
            List<String> aliases = new ArrayList<>();
            if (source != null) { aliases.addAll(source.aliases); aliases.add(source.id); }
            job.edit = new AppliedEdit(job.id, job.owner, job.ownerName, job.snapshot, job.prompt, job.replay,
                    java.util.Collections.unmodifiableList(new ArrayList<>(job.palette)), job.modelLabel, job.base, aliases);
            Deque<AppliedEdit> edits = history(job.owner);
            if (source != null) edits.remove(source);
            edits.addFirst(job.edit);
            while (edits.size() > config.maxUndoEdits) edits.removeLast();
            job.applied = true;
        }
        job.edit.restored = false;
    }
}
