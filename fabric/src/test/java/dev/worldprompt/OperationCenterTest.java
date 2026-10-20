package dev.worldprompt;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.JsonParser;
import dev.worldprompt.kernel.RegionBounds;
import org.junit.jupiter.api.Test;

class OperationCenterTest {
    @Test void inclusiveCuboidCenterUsesWorldBlockCenters() {
        var region = RegionBounds.between(-10, 20, -30, 0, 30, -20, 2000);
        for (String op : new String[]{"fill", "replace", "hollow_box", "solid_ellipsoid", "hollow_ellipsoid",
                "solid_cylinder", "hollow_cylinder"})
            assertArrayEquals(new double[]{-7.5, 21, -28}, OperationCenter.of(JsonParser.parseString(
                    "{\"op\":\"" + op + "\",\"min\":[1,0,0],\"max\":[3,1,3]}").getAsJsonObject(), region));
    }
    @Test void reversedLineUsesEndpointMidpoint() {
        var region = RegionBounds.between(100, -20, 0, 110, 0, 10, 3000);
        assertArrayEquals(new double[]{104.5, -15.5, 2.5}, OperationCenter.of(JsonParser.parseString(
                "{\"op\":\"line\",\"start\":[8,2,4],\"end\":[0,6,0]}").getAsJsonObject(), region));
    }
    @Test void singleUsesItsOnlyCoordinate() {
        var region = RegionBounds.between(-10, 20, -30, 0, 30, -20, 2000);
        assertArrayEquals(new double[]{-8.5, 22.5, -26.5}, OperationCenter.of(JsonParser.parseString(
                "{\"op\":\"single\",\"at\":[1,2,3]}").getAsJsonObject(), region));
    }
}
