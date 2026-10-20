package dev.worldprompt.kernel;

/** Inclusive world bounds and the transport's x + z*w + y*w*l indexing. */
public record RegionBounds(int minX, int minY, int minZ, int width, int height, int length) {
    public static RegionBounds around(int x, int y, int z, int sizeX, int sizeY, int sizeZ, int maxVolume) {
        if (sizeX < 1 || sizeY < 1 || sizeZ < 1)
            throw new IllegalArgumentException("Selection sizes must be positive.");
        int minX = Math.subtractExact(x, sizeX / 2);
        int minY = Math.subtractExact(y, sizeY / 2);
        int minZ = Math.subtractExact(z, sizeZ / 2);
        return between(minX, minY, minZ, Math.addExact(minX, sizeX - 1), Math.addExact(minY, sizeY - 1),
                Math.addExact(minZ, sizeZ - 1), maxVolume);
    }

    public static RegionBounds between(int ax, int ay, int az, int bx, int by, int bz, int maxVolume) {
        long w = Math.abs((long) ax - bx) + 1;
        long h = Math.abs((long) ay - by) + 1;
        long l = Math.abs((long) az - bz) + 1;
        if (w > maxVolume || h > maxVolume || l > maxVolume || w * h > maxVolume || w * h * l > maxVolume)
            throw new IllegalArgumentException("Selection exceeds " + maxVolume + " blocks.");
        return new RegionBounds(Math.min(ax, bx), Math.min(ay, by), Math.min(az, bz), (int) w, (int) h, (int) l);
    }
    /** True when the region lies entirely between the dimension's lowest and highest buildable blocks. */
    public boolean withinHeight(int lowestY, int highestY) { return minY >= lowestY && maxY() <= highestY; }
    public int volume() { return Math.multiplyExact(Math.multiplyExact(width, height), length); }
    public int maxX() { return Math.addExact(minX, width - 1); }
    public int maxY() { return Math.addExact(minY, height - 1); }
    public int maxZ() { return Math.addExact(minZ, length - 1); }
    public boolean contains(int x, int y, int z) {
        return x >= minX && x <= maxX() && y >= minY && y <= maxY() && z >= minZ && z <= maxZ();
    }
    /** Inclusive selections intersect even when they share only a boundary block. */
    public boolean intersects(RegionBounds other) {
        return minX <= other.maxX() && other.minX <= maxX()
                && minY <= other.maxY() && other.minY <= maxY()
                && minZ <= other.maxZ() && other.minZ <= maxZ();
    }
    public int x(int index) { check(index); return minX + index % width; }
    public int y(int index) { check(index); return minY + index / (width * length); }
    public int z(int index) { check(index); return minZ + index / width % length; }
    private void check(int index) {
        if (index < 0 || index >= volume()) throw new IllegalArgumentException("Write outside captured bounds.");
    }
    /** Twelve outer-face edges at quarter-block spacing, with a cap per edge. */
    public void edges(int maxSegments, PointConsumer consumer) {
        for (int axis = 0; axis < 3; axis++) {
            double[] low = {minX, minY, minZ};
            int[] sizes = {width, height, length};
            int otherA = (axis + 1) % 3, otherB = (axis + 2) % 3;
            int segments = Math.max(1, (int) Math.min(maxSegments, sizes[axis] * 4L));
            for (int a = 0; a < 2; a++) for (int b = 0; b < 2; b++) {
                double[] p = low.clone();
                p[otherA] += a * sizes[otherA];
                p[otherB] += b * sizes[otherB];
                for (int i = 0; i <= segments; i++) {
                    p[axis] = low[axis] + sizes[axis] * (double) i / segments;
                    consumer.accept(p[0], p[1], p[2]);
                }
            }
        }
    }
    @FunctionalInterface public interface PointConsumer { void accept(double x, double y, double z); }
}
