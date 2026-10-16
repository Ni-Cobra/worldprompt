package dev.worldprompt;

import com.google.gson.JsonObject;

/** Geometric center of the authentic operation, including no-op replacements. */
final class OperationCenter {
    static double[] of(JsonObject operation, RegionBounds region) {
        boolean line = operation.get("op").getAsString().equals("line");
        boolean single = operation.get("op").getAsString().equals("single");
        var start = operation.getAsJsonArray(single ? "at" : line ? "start" : "min");
        var end = single ? start : operation.getAsJsonArray(line ? "end" : "max");
        int[] origin = {region.minX(), region.minY(), region.minZ()};
        double[] center = new double[3];
        for (int axis = 0; axis < 3; axis++)
            center[axis] = origin[axis] + (start.get(axis).getAsDouble() + end.get(axis).getAsDouble()) / 2 + 0.5;
        return center;
    }
}
