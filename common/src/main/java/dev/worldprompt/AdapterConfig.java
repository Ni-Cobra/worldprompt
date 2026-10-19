package dev.worldprompt;

import dev.worldprompt.kernel.CoreLimits;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import java.io.IOException;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Locale;
import java.util.Map;

final class AdapterConfig {
    private static final Gson GSON = new GsonBuilder().setPrettyPrinting().create();
    private static final String DEFAULT_SERVICE_URL = "http://127.0.0.1:8768";
    String serviceUrl = DEFAULT_SERVICE_URL;
    String tokenFile = "";
    String token = "";
    int maxSelectionBlocks = 2_000_000;
    int blocksPerTick = 4096;
    int pollMilliseconds = 1000;
    int maxParticleSegments = 96;
    int selectionCubeSize = 30;
    int maxUndoEdits = 16;
    /** Give the player a signed book with the model's summary after each completed prompt. */
    boolean summaryBook = true;

    static AdapterConfig load(Path configDirectory) throws IOException {
        Path path = configDirectory.resolve("worldprompt.json");
        if (!Files.exists(path)) {
            Files.createDirectories(path.getParent());
            Files.writeString(path, GSON.toJson(new AdapterConfig()) + "\n");
        }
        AdapterConfig config = GSON.fromJson(Files.readString(path), AdapterConfig.class);
        if (config == null) throw new IOException("Empty WorldPrompt config.");
        config.serviceUrl = normalizeUrl(config.serviceUrl);
        if (config.tokenFile == null || config.token == null)
            throw new IOException("tokenFile and token must be strings (leave them empty for automatic discovery).");
        if (config.maxSelectionBlocks < 1 || config.maxSelectionBlocks > CoreLimits.MAX_BLOCKS
                || config.blocksPerTick < 1 || config.blocksPerTick > 65536
                || config.pollMilliseconds < 250 || config.pollMilliseconds > 10000
                || config.maxParticleSegments < 1 || config.maxParticleSegments > 256
                || config.selectionCubeSize < 1 || config.selectionCubeSize > 512
                || config.maxUndoEdits < 1 || config.maxUndoEdits > 256)
            throw new IOException("WorldPrompt config limits are out of range.");
        return config;
    }

    /** Resolve on every request so Core can start or restart after Minecraft. */
    Connection connection() throws IOException {
        return connection(discoveryPath(System.getProperty("os.name"), System.getenv(),
                Path.of(System.getProperty("user.home"))));
    }

    Connection connection(Path discoveryFile) throws IOException {
        if (!tokenFile.isBlank() || !token.isBlank())
            return new Connection(normalizeUrl(serviceUrl), readToken(tokenFile, token));
        if (!Files.isRegularFile(discoveryFile))
            throw new IOException("Start WorldPrompt Core on this computer first. No connection file at "
                    + discoveryFile + ". For a custom setup, set serviceUrl and tokenFile in config/worldprompt.json.");
        try {
            Discovery discovered = GSON.fromJson(Files.readString(discoveryFile), Discovery.class);
            if (discovered == null || discovered.tokenFile == null || discovered.tokenFile.isBlank())
                throw new IOException("Core connection file has no tokenFile; restart WorldPrompt Core.");
            Path tokenPath = Path.of(discovered.tokenFile);
            if (!tokenPath.isAbsolute())
                throw new IOException("Core connection tokenFile must be an absolute path.");
            String url = serviceUrl.equals(DEFAULT_SERVICE_URL) ? discovered.serviceUrl : serviceUrl;
            return new Connection(normalizeUrl(url), readToken(discovered.tokenFile, ""));
        } catch (RuntimeException e) {
            throw new IOException("Invalid Core connection file at " + discoveryFile + "; restart WorldPrompt Core.", e);
        }
    }

    static Path discoveryPath(String osName, Map<String, String> environment, Path home) {
        String override = environment.getOrDefault("WORLDPROMPT_CONNECTION_FILE", "").strip();
        if (!override.isEmpty()) return Path.of(override);
        if (osName.toLowerCase(Locale.ROOT).startsWith("windows")) {
            String local = environment.getOrDefault("LOCALAPPDATA", "").strip();
            Path base = local.isEmpty() ? home.resolve("AppData/Local") : Path.of(local);
            return base.resolve("WorldPrompt/connection.json");
        }
        String xdg = environment.getOrDefault("XDG_DATA_HOME", "").strip();
        Path base = xdg.isEmpty() || !Path.of(xdg).isAbsolute() ? home.resolve(".local/share") : Path.of(xdg);
        return base.resolve("worldprompt/connection.json");
    }

    private static String normalizeUrl(String value) throws IOException {
        try {
            URI uri = URI.create(value);
            if (!java.util.Set.of("http", "https").contains(uri.getScheme()) || uri.getHost() == null
                    || uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null)
                throw new IllegalArgumentException();
            return value.replaceAll("/+$", "");
        } catch (RuntimeException e) {
            throw new IOException("serviceUrl must be an HTTP(S) URL without credentials, query or fragment.", e);
        }
    }

    private static String readToken(String file, String inline) throws IOException {
        String value;
        try { value = file.isBlank() ? inline.strip() : Files.readString(Path.of(file)).strip(); }
        catch (IOException | RuntimeException e) {
            throw new IOException("Cannot read the Core token file. Start WorldPrompt Core and check tokenFile.", e);
        }
        if (value.isBlank()) throw new IOException("The Core token is empty; restart WorldPrompt Core.");
        return value;
    }

    record Connection(String serviceUrl, String bearerToken) {}
    private static final class Discovery { String serviceUrl; String tokenFile; }
}
