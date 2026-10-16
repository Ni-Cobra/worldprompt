package dev.worldprompt;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.Gson;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class AdapterConfigTest {
    @TempDir Path temporary;

    private Path connectionFile(int port, Path tokenFile) throws IOException {
        Path path = temporary.resolve("connection.json");
        Files.writeString(path, new Gson().toJson(Map.of(
                "serviceUrl", "http://127.0.0.1:" + port, "tokenFile", tokenFile.toString())));
        return path;
    }

    @Test void discoversCoreAndReloadsChangedConnectionWithoutMinecraftRestart() throws Exception {
        AdapterConfig config = new AdapterConfig();
        Path token = temporary.resolve("token.txt");
        Files.writeString(token, "first-token\n");
        Path discovery = connectionFile(9876, token);
        assertEquals(new AdapterConfig.Connection("http://127.0.0.1:9876", "first-token"),
                config.connection(discovery));
        Files.writeString(token, "second-token\n");
        connectionFile(9877, token);
        assertEquals(new AdapterConfig.Connection("http://127.0.0.1:9877", "second-token"),
                config.connection(discovery));
        assertEquals("", config.tokenFile, "Discovery must not pin transient paths into config");
    }

    @Test void explicitCredentialsDoNotRequireDiscoveryAndTokenFileTakesPriority() throws Exception {
        AdapterConfig config = new AdapterConfig();
        config.serviceUrl = "http://localhost:9876/";
        config.token = " manual-token ";
        Path missing = temporary.resolve("missing.json");
        assertEquals(new AdapterConfig.Connection("http://localhost:9876", "manual-token"),
                config.connection(missing));
        Path token = temporary.resolve("manual-token.txt");
        Files.writeString(token, "file-token\n");
        config.tokenFile = token.toString();
        assertEquals("file-token", config.connection(missing).bearerToken());
    }

    @Test void preservesCustomServiceUrlWhileDiscoveringCredentials() throws Exception {
        AdapterConfig config = new AdapterConfig();
        config.serviceUrl = "http://localhost:4321";
        Path token = temporary.resolve("token.txt");
        Files.writeString(token, "test-token");
        assertEquals("http://localhost:4321", config.connection(connectionFile(8768, token)).serviceUrl());
    }

    @Test void missingCoreProvidesActionableErrorAndCanRecoverLater() throws Exception {
        AdapterConfig config = new AdapterConfig();
        Path discovery = temporary.resolve("connection.json");
        IOException error = assertThrows(IOException.class, () -> config.connection(discovery));
        assertTrue(error.getMessage().contains("Start WorldPrompt Core"));
        Path token = temporary.resolve("token.txt");
        Files.writeString(token, "ready");
        connectionFile(8768, token);
        assertEquals("ready", config.connection(discovery).bearerToken());
    }

    @Test void rejectsInvalidDiscoveryAndRelativeTokenFiles() throws Exception {
        AdapterConfig config = new AdapterConfig();
        Path discovery = temporary.resolve("connection.json");
        Files.writeString(discovery, "not-json");
        assertThrows(IOException.class, () -> config.connection(discovery));
        connectionFile(8768, Path.of("relative-token.txt"));
        assertTrue(assertThrows(IOException.class, () -> config.connection(discovery))
                .getMessage().contains("absolute path"));
        Files.writeString(discovery, "{\"serviceUrl\":\"file:///tmp/socket\",\"tokenFile\":\"/missing\"}");
        assertTrue(assertThrows(IOException.class, () -> config.connection(discovery))
                .getMessage().contains("HTTP(S)"));
    }

    @Test void discoversWindowsAndLinuxPathsAndEnvironmentOverride() {
        Path home = temporary.resolve("home");
        assertEquals(home.resolve(".local/share/worldprompt/connection.json"),
                AdapterConfig.discoveryPath("Linux", Map.of(), home));
        Path xdg = temporary.resolve("data");
        assertEquals(xdg.resolve("worldprompt/connection.json"),
                AdapterConfig.discoveryPath("Linux", Map.of("XDG_DATA_HOME", xdg.toString()), home));
        assertEquals(home.resolve(".local/share/worldprompt/connection.json"),
                AdapterConfig.discoveryPath("Linux", Map.of("XDG_DATA_HOME", "relative"), home));
        Path local = temporary.resolve("LocalAppData");
        assertEquals(local.resolve("WorldPrompt/connection.json"),
                AdapterConfig.discoveryPath("Windows 11", Map.of("LOCALAPPDATA", local.toString()), home));
        assertEquals(home.resolve("AppData/Local/WorldPrompt/connection.json"),
                AdapterConfig.discoveryPath("Windows 11", Map.of(), home));
        Path override = temporary.resolve("custom.json");
        assertEquals(override, AdapterConfig.discoveryPath("Linux",
                Map.of("WORLDPROMPT_CONNECTION_FILE", override.toString()), home));
    }
}
