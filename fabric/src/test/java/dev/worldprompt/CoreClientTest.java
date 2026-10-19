package dev.worldprompt;

import dev.worldprompt.kernel.Versions;

import static org.junit.jupiter.api.Assertions.*;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CompletionException;
import java.util.concurrent.Flow;
import org.junit.jupiter.api.Test;

class CoreClientTest {
    @Test void responseLimitRejectsAccumulatedChunksBeforeParsing() {
        var subscriber = CoreClient.boundedBody(5).apply(null);
        boolean[] cancelled = {false};
        subscriber.onSubscribe(new Flow.Subscription() {
            public void request(long count) { }
            public void cancel() { cancelled[0] = true; }
        });
        subscriber.onNext(List.of(ByteBuffer.wrap(new byte[]{1,2,3})));
        subscriber.onNext(List.of(ByteBuffer.wrap(new byte[]{4,5,6})));
        subscriber.onComplete();
        assertTrue(cancelled[0]);
        assertThrows(CompletionException.class, () -> subscriber.getBody().toCompletableFuture().join());
    }
    @Test void responseAtLimitCompletesNormally() {
        var subscriber = CoreClient.boundedBody(2).apply(null);
        subscriber.onSubscribe(new Flow.Subscription() {
            public void request(long count) { }
            public void cancel() { fail("Unexpected cancellation"); }
        });
        subscriber.onNext(List.of(ByteBuffer.wrap("{}".getBytes(StandardCharsets.UTF_8))));
        subscriber.onComplete();
        assertEquals("{}", subscriber.getBody().toCompletableFuture().join());
    }
    private static com.sun.net.httpserver.HttpServer server(java.util.function.Consumer<com.sun.net.httpserver.HttpExchange> handler)
            throws java.io.IOException {
        return server(Versions.MOD, handler);
    }
    /** A stand-in Core reporting {@code coreVersion}, or no version at all when it is null. */
    private static com.sun.net.httpserver.HttpServer server(String coreVersion,
            java.util.function.Consumer<com.sun.net.httpserver.HttpExchange> handler) throws java.io.IOException {
        var http = com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1", 0), 0);
        http.createContext("/", exchange -> {
            handler.accept(exchange);
            byte[] reply = "{\"ok\":true}".getBytes(StandardCharsets.UTF_8);
            if (coreVersion != null) exchange.getResponseHeaders().set(Versions.CORE_HEADER, coreVersion);
            try { exchange.sendResponseHeaders(200, reply.length); exchange.getResponseBody().write(reply); }
            catch (java.io.IOException ignored) { }
            exchange.close();
        });
        http.start();
        return http;
    }
    private static AdapterConfig configFor(com.sun.net.httpserver.HttpServer http) {
        AdapterConfig config = new AdapterConfig();
        config.serviceUrl = "http://127.0.0.1:" + http.getAddress().getPort();
        config.token = "test-token";
        return config;
    }
    @Test void postWithoutABodyStaysAPostWithAnEmptyBody() throws Exception {
        String[] seen = new String[2];
        var http = server(exchange -> {
            seen[0] = exchange.getRequestMethod();
            try { seen[1] = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8); }
            catch (java.io.IOException e) { seen[1] = "error"; }
        });
        try (CoreClient client = new CoreClient(configFor(http))) {
            assertTrue(client.request("POST", "/api/world/jobs/x/cancel", null).get(5, java.util.concurrent.TimeUnit.SECONDS).get("ok").getAsBoolean());
            assertEquals("POST", seen[0]);
            assertEquals("", seen[1]);
            client.request("GET", "/api/world/info", null).get(5, java.util.concurrent.TimeUnit.SECONDS);
            assertEquals("GET", seen[0]);
        } finally { http.stop(0); }
    }
    /** {@link Versions#MOD} with MAJOR, MINOR or PATCH moved by one: only a PATCH change stays compatible. */
    private static String modVersionWith(int part) {
        String[] parts = Versions.MOD.split("[-+]", 2)[0].split("\\.");
        parts[part] = String.valueOf(Integer.parseInt(parts[part]) + 1);
        return String.join(".", parts);
    }
    @Test void everyRequestCarriesTheModVersionAndAPatchDifferenceIsCompatible() throws Exception {
        String[] seen = new String[1];
        var http = server(modVersionWith(2), exchange -> seen[0] = exchange.getRequestHeaders().getFirst(Versions.MOD_HEADER));
        try (CoreClient client = new CoreClient(configFor(http))) {
            assertTrue(client.request("GET", "/api/world/jobs", null).get(5, java.util.concurrent.TimeUnit.SECONDS).get("ok").getAsBoolean());
            assertEquals(Versions.MOD, seen[0]);
        } finally { http.stop(0); }
    }
    @Test void aCoreOfAnotherApiVersionOrWithoutOneIsRefused() throws Exception {
        for (String core : new String[]{modVersionWith(1), modVersionWith(0), "banana", null}) {
            var http = server(core, exchange -> { });
            try (CoreClient client = new CoreClient(configFor(http))) {
                var error = assertThrows(java.util.concurrent.ExecutionException.class,
                        () -> client.request("POST", "/api/world/jobs", java.util.Map.of()).get(5, java.util.concurrent.TimeUnit.SECONDS));
                var api = assertInstanceOf(CoreClient.ApiException.class, CoreClient.cause(error));
                assertEquals(Versions.MISMATCH, api.code);
                assertTrue(api.getMessage().contains(core == null ? "does not report its version"
                        : core.equals("banana") ? "unreadable version" : "must match"), api.getMessage());
            } finally { http.stop(0); }
        }
    }
    @Test void versionsCompareMajorAndMinorOnly() {
        assertEquals("0.10", Versions.api("0.10.7"));
        assertEquals("1.2", Versions.api("01.2.3-rc.1"));
        assertNull(Versions.api("0.10"));
        assertNull(Versions.api(null));
        assertNull(Versions.coreMismatch(modVersionWith(2)));
    }
    @Test void closeDoesNotInterruptARequestThatIsAlreadyInFlight() throws Exception {
        var started = new java.util.concurrent.CountDownLatch(1);
        var http = server(exchange -> {
            started.countDown();
            try { Thread.sleep(300); } catch (InterruptedException ignored) { }
        });
        try {
            CoreClient client = new CoreClient(configFor(http));
            var reply = client.request("POST", "/api/world/jobs/x/cancel", java.util.Map.of("message", "stopped"));
            assertTrue(started.await(5, java.util.concurrent.TimeUnit.SECONDS));
            client.close();
            assertTrue(reply.get(5, java.util.concurrent.TimeUnit.SECONDS).get("ok").getAsBoolean(),
                    "shutdown() lets the cancellation finish; shutdownNow() would have interrupted it");
        } finally { http.stop(0); }
    }
}
