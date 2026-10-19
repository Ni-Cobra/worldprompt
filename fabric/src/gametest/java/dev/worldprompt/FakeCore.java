package dev.worldprompt;

import dev.worldprompt.kernel.Versions;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;
import java.util.function.Predicate;

/**
 * A deterministic stand-in for WorldPrompt Core's world API on a loopback port, shared by the GameTests.
 * Each test supplies a {@code planner} that maps a submitted request to the {@link Result} Core would
 * return; this class owns the HTTP routes and the job states, so tests only describe what differs.
 * A prompt listed in {@link #planningPrompts} stays in "planning" until the test moves it with
 * {@link #states}; every other job is "ready" at once and follows the mod's application updates.
 */
final class FakeCore implements AutoCloseable {
    static final String TOKEN = "gametest-only-token";

    /** One plan operation and the placement runs (start, length, palette index triples) Core would encode for it. */
    record Op(Map<String, Object> definition, int[] runs, boolean dynamicMatch) {
        Op(Map<String, Object> definition, int... runs) { this(definition, runs, false); }
    }
    record Result(List<Integer> dimensions, List<String> palette, String summary, List<Op> operations) { }

    final HttpServer http;
    private final ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
    private final Function<JsonObject, Result> planner;
    final Map<String, JsonObject> requests = new ConcurrentHashMap<>();
    final Map<String, String> states = new ConcurrentHashMap<>();
    final Map<String, Integer> resultRequests = new ConcurrentHashMap<>();
    final AtomicInteger modelPosts = new AtomicInteger(), rebuildPosts = new AtomicInteger();
    /** The newest submitted request. */
    volatile JsonObject lastRequest;
    /** Unfinished jobs one player may own at once; 0 for no limit. */
    volatile int limit;
    volatile String model = "gpt-6-astra", reasoningEffort = "high";
    volatile boolean fastMode = true;
    /** Report token usage on every job, as a real Core does once a model has run. */
    volatile boolean tokenUsage;
    volatile Set<String> planningPrompts = Set.of("hold", "blocked");
    /** Requests whose cancellation Core answers with a 503, to look like an outage. */
    volatile Predicate<JsonObject> cancelOutage = request -> false;
    private volatile boolean closed;

    FakeCore(Function<JsonObject, Result> planner) throws IOException {
        this.planner = planner;
        http = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        http.createContext("/api/world/info", exchange -> {
            if (!authorized(exchange)) return;
            respond(exchange, 200, Map.of("version", "test", "provider", "codex", "model", model,
                    "reasoning_effort", reasoningEffort, "fast_mode", fastMode, "active_jobs", 0, "queued_jobs", 0));
        });
        http.createContext("/api/world/jobs", this::handle);
        http.setExecutor(executor);
        http.start();
    }

    /** Point the mod at this server. */
    void connect(AdapterConfig config) {
        config.serviceUrl = "http://127.0.0.1:" + http.getAddress().getPort();
        config.token = TOKEN;
    }

    private static boolean finished(String state) { return state.equals("failed") || state.equals("completed"); }
    private static String prompt(JsonObject request) { return request.get("prompt").getAsString(); }

    private JsonObject publicJob(String id) {
        JsonObject request = requests.get(id);
        JsonObject body = request.deepCopy();
        body.remove("block_catalog");
        body.addProperty("id", id);
        body.addProperty("status", states.get(id));
        body.addProperty("model", model);
        body.addProperty("reasoning_effort", reasoningEffort);
        body.addProperty("fast_mode", fastMode);
        body.addProperty("operation_count", planner.apply(request).operations().size());
        body.addProperty("operation", 0);
        if (tokenUsage) {
            int tokens = request.get("effort").getAsString().equals("high") ? 300 : 150;
            body.add("token_usage", new Gson().toJsonTree(Map.of("input_tokens", tokens * 2 / 3,
                    "output_tokens", tokens / 3, "total_tokens", tokens, "complete", true,
                    "model_calls", tokens / 150)));
        }
        return body;
    }

    private boolean authorized(HttpExchange exchange) throws IOException {
        if (("Bearer " + TOKEN).equals(exchange.getRequestHeaders().getFirst("Authorization"))) return true;
        respond(exchange, 403, Map.of("error", "Missing test token"));
        return false;
    }

    private synchronized void handle(HttpExchange exchange) throws IOException {
        try {
            if (!authorized(exchange)) return;
            String suffix = exchange.getRequestURI().getPath().substring("/api/world/jobs".length());
            if (suffix.isEmpty()) {
                if (!exchange.getRequestMethod().equals("POST")) {
                    respond(exchange, 200, Map.of("jobs", requests.keySet().stream()
                            .filter(id -> !finished(states.get(id))).map(this::publicJob).toList()));
                    return;
                }
                JsonObject request = body(exchange);
                String owner = request.get("owner_id").getAsString();
                long unfinished = requests.entrySet().stream().filter(entry -> entry.getValue().get("owner_id").getAsString().equals(owner)
                        && !finished(states.get(entry.getKey()))).count();
                if (limit > 0 && unfinished >= limit) {
                    respond(exchange, 409, Map.of("error", "Core allows at most " + limit + " active WorldPrompt job per player "
                            + "(world_max_jobs_per_player), and you already have " + limit + ".", "code", "player_job_limit", "limit", limit));
                    return;
                }
                String id = request.get("request_id").getAsString();
                modelPosts.incrementAndGet();
                requests.put(id, request);
                lastRequest = request;
                states.put(id, planningPrompts.contains(prompt(request)) ? "planning" : "ready");
                respond(exchange, 202, publicJob(id));
                return;
            }
            String[] parts = suffix.substring(1).split("/");
            String id = parts[0];
            if (!requests.containsKey(id)) { respond(exchange, 404, Map.of("error", "No job")); return; }
            if (parts.length == 1) { respond(exchange, 200, publicJob(id)); return; }
            switch (parts[1]) {
                case "rebuild" -> {
                    rebuildPosts.incrementAndGet();
                    respond(exchange, 404, Map.of("error", "Rebuild belongs to the mod"));
                }
                case "cancel" -> {
                    if (cancelOutage.test(requests.get(id))) { respond(exchange, 503, Map.of("error", "Deliberate cancellation outage")); return; }
                    states.put(id, "failed"); respond(exchange, 200, publicJob(id));
                }
                case "application" -> {
                    states.put(id, body(exchange).get("status").getAsString()); respond(exchange, 200, publicJob(id));
                }
                case "result" -> {
                    resultRequests.merge(id, 1, Integer::sum);
                    JsonObject request = requests.get(id);
                    boolean doublePass = request.has("effort") && request.get("effort").getAsString().equals("high");
                    respond(exchange, 200, resultJson(planner.apply(request), doublePass));
                }
                default -> respond(exchange, 404, Map.of("error", "No operation paging route"));
            }
        } catch (Throwable error) { respond(exchange, 500, Map.of("error", error.toString())); }
    }

    private static JsonObject body(HttpExchange exchange) throws IOException {
        return JsonParser.parseString(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8)).getAsJsonObject();
    }

    /** The version 2 result payload: the plan, plus each operation's placement runs as little-endian base64. */
    private JsonObject resultJson(Result result, boolean doublePass) {
        JsonArray definitions = new JsonArray(), operations = new JsonArray();
        for (int i = 0; i < result.operations().size(); i++) {
            Op op = result.operations().get(i);
            ByteBuffer bytes = ByteBuffer.allocate(op.runs().length * 4).order(ByteOrder.LITTLE_ENDIAN);
            long writes = 0;
            for (int n = 0; n < op.runs().length; n++) { bytes.putInt(op.runs()[n]); if (n % 3 == 1) writes += op.runs()[n]; }
            JsonObject packed = new JsonObject();
            packed.addProperty("index", i);
            packed.addProperty("op", op.definition().get("op").toString());
            packed.addProperty("run_count", op.runs().length / 3);
            packed.addProperty("writes", writes);
            packed.addProperty("dynamic_match", op.dynamicMatch());
            packed.addProperty("runs_base64", Base64.getEncoder().encodeToString(bytes.array()));
            operations.add(packed);
            definitions.add(new Gson().toJsonTree(op.definition()));
        }
        JsonObject plan = new JsonObject();
        plan.addProperty("version", 1);
        plan.addProperty("summary", result.summary());
        plan.add("operations", definitions);
        JsonObject json = new JsonObject();
        json.addProperty("result_version", 2);
        json.addProperty("run_encoding", "base64-u32le");
        json.add("dimensions", new Gson().toJsonTree(result.dimensions()));
        json.add("palette", new Gson().toJsonTree(result.palette()));
        json.addProperty("operation_count", result.operations().size());
        json.add("plan", plan);
        json.add("operations", operations);
        // Core adds the job's saved model settings to every result, and Double pass jobs' draft summary.
        json.addProperty("model", model);
        json.addProperty("reasoning_effort", reasoningEffort);
        if (doublePass) json.addProperty("draft_plan_summary", "Draft: " + result.summary());
        return json;
    }

    private static void respond(HttpExchange exchange, int status, Object body) throws IOException {
        byte[] data = new Gson().toJson(body).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.getResponseHeaders().set(Versions.CORE_HEADER, Versions.MOD);
        exchange.sendResponseHeaders(status, data.length);
        exchange.getResponseBody().write(data);
        exchange.close();
    }

    @Override public void close() {
        if (!closed) { closed = true; http.stop(0); executor.shutdownNow(); }
    }
}
