package dev.worldprompt;

import static org.junit.jupiter.api.Assertions.*;
import java.util.HashSet;
import java.util.Set;
import org.junit.jupiter.api.Test;

class RegionBoundsTest {
    @Test void reversedCornersAreInclusiveAndTransportIndicesMatchXZY() {
        RegionBounds b = RegionBounds.between(12, 25, -7, 10, 24, -10, 100);
        assertEquals(new RegionBounds(10, 24, -10, 3, 2, 4), b);
        assertEquals(24, b.volume());
        assertEquals(10, b.x(12)); assertEquals(25, b.y(12)); assertEquals(-10, b.z(12));
        assertEquals(12, b.x(23)); assertEquals(25, b.y(23)); assertEquals(-7, b.z(23));
        assertThrows(IllegalArgumentException.class, () -> b.x(24));
    }
    @Test void volumeLimitsRejectOverflow() {
        assertThrows(IllegalArgumentException.class, () -> RegionBounds.between(Integer.MIN_VALUE, 0, 0, Integer.MAX_VALUE, 0, 0, 100));
        assertThrows(IllegalArgumentException.class, () -> RegionBounds.between(0, 0, 0, 1000, 1000, 1000, 2_000_000));
    }
    @Test void twelveEdgesUseOuterFacesAndBoundedSamples() {
        RegionBounds b = RegionBounds.between(-10, 3, -5, 89, 22, 44, 200000);
        Set<String> corners = new HashSet<>();
        int[] samples = {0};
        b.edges(5, (x, y, z) -> {
            samples[0]++;
            int boundaryAxes = (x == -10 || x == 90 ? 1 : 0) + (y == 3 || y == 23 ? 1 : 0) + (z == -5 || z == 45 ? 1 : 0);
            assertTrue(boundaryAxes >= 2);
            if (boundaryAxes == 3) corners.add(x + "," + y + "," + z);
        });
        assertEquals(12 * 6, samples[0]); assertEquals(8, corners.size());
    }
    @Test void singleBlockSelectionOutlinesAUnitCube() {
        RegionBounds b = RegionBounds.between(2, 3, 4, 2, 3, 4, 1);
        assertEquals(1, b.volume());
        int[] points = {0};
        b.edges(24, (x, y, z) -> {
            points[0]++;
            int boundaryAxes = (x == 2 || x == 3 ? 1 : 0) + (y == 3 || y == 4 ? 1 : 0) + (z == 4 || z == 5 ? 1 : 0);
            assertTrue(boundaryAxes >= 2);
            assertTrue(x >= 2 && x <= 3 && y >= 3 && y <= 4 && z >= 4 && z <= 5);
        });
        assertEquals(12 * 5, points[0]);
    }
    @Test void automaticCubeHasTheConfiguredExactSizeAroundItsCenter() {
        assertEquals(30, new AdapterConfig().selectionCubeSize);
        RegionBounds even = RegionBounds.around(10, 64, -5, 30, 30, 30, 30 * 30 * 30);
        assertEquals(new RegionBounds(-5, 49, -20, 30, 30, 30), even);
        assertEquals(24, even.maxX()); assertEquals(78, even.maxY()); assertEquals(9, even.maxZ());
        assertEquals(27_000, even.volume());
        assertEquals(new RegionBounds(8, 62, -7, 5, 5, 5), RegionBounds.around(10, 64, -5, 5, 5, 5, 125));
        assertThrows(IllegalArgumentException.class, () -> RegionBounds.around(0, 0, 0, 30, 30, 30, 26_999));
        assertThrows(ArithmeticException.class, () -> RegionBounds.around(Integer.MIN_VALUE, 0, 0, 30, 30, 30, Integer.MAX_VALUE));
    }
    @Test void rectangularSelectionUsesExactDimensionsAroundTarget() {
        RegionBounds bounds = RegionBounds.around(10, 64, -5, 50, 20, 30, 30_000);
        assertEquals(new RegionBounds(-15, 54, -20, 50, 20, 30), bounds);
        assertEquals(30_000, bounds.volume());
        assertEquals(new RegionBounds(9, 63, -6, 3, 2, 3),
                RegionBounds.around(10, 64, -5, 3, 2, 3, 18));
        assertThrows(IllegalArgumentException.class, () -> RegionBounds.around(0, 64, 0, 50, 20, 30, 29_999));
        assertThrows(IllegalArgumentException.class, () -> RegionBounds.around(0, 64, 0, 50, 0, 30, 30_000));
    }
    @Test void intersectionIncludesSharedBoundaryAndExcludesAdjacentOrSeparatedAxes() {
        RegionBounds first = RegionBounds.between(0, 0, 0, 2, 2, 2, 27);
        assertTrue(first.intersects(RegionBounds.between(2, 2, 2, 4, 4, 4, 27)));
        assertFalse(first.intersects(RegionBounds.between(3, 0, 0, 4, 1, 1, 27)));
        assertFalse(first.intersects(RegionBounds.between(0, 3, 0, 1, 4, 1, 27)));
        assertFalse(first.intersects(RegionBounds.between(0, 0, 3, 1, 1, 4, 27)));
    }
    @Test void idleOutlineUsesFewerSamplesThanRunningOutlineForTypicalCube() {
        RegionBounds b = RegionBounds.around(0, 64, 0, 30, 30, 30, 30 * 30 * 30);
        int[] idle = {0}, running = {0};
        b.edges(96 / 3, (x, y, z) -> idle[0]++);
        b.edges(96, (x, y, z) -> running[0]++);
        assertTrue(idle[0] < running[0]);
    }
    @Test void heightCheckIsInclusiveAtBothBuildLimits() {
        RegionBounds b = RegionBounds.between(0, -64, 0, 1, 319, 1, 100_000);
        assertTrue(b.withinHeight(-64, 319));
        assertFalse(b.withinHeight(-63, 319));
        assertFalse(b.withinHeight(-64, 318));
        assertTrue(RegionBounds.between(0, 5, 0, 0, 5, 0, 1).withinHeight(5, 5));
    }
}
