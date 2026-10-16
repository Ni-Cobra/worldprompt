package dev.worldprompt;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.Executors;

/** Network and JSON serialization never run on the Minecraft server thread. */
final class CoreClient implements AutoCloseable {
    private final AdapterConfig config;
    private final java.util.concurrent.ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
    private final HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    CoreClient(AdapterConfig config) { this.config = config; }

    CompletableFuture<JsonObject> request(String method, String path, Object body) {
        return requestLazy(method, path, () -> body);
    }
    CompletableFuture<JsonObject> requestLazy(String method, String path, java.util.function.Supplier<Object> bodySupplier) {
        return CompletableFuture.supplyAsync(() -> {
            try {
                Object body = bodySupplier.get();
                AdapterConfig.Connection connection;
                try { connection = config.connection(); }
                catch (java.io.IOException e) { throw new TokenException(e.getMessage()); }
                HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(connection.serviceUrl() + path))
                        .timeout(Duration.ofSeconds(20)).header("Authorization", "Bearer " + connection.bearerToken())
                        .header(Versions.MOD_HEADER, Versions.MOD);
                // A POST or PUT without a payload is still that method, with an empty body, never a GET.
                if (body != null) builder.header("Content-Type", "application/json").method(method,
                        HttpRequest.BodyPublishers.ofString(new Gson().toJson(body)));
                else if (method.equals("GET")) builder.GET();
                else builder.method(method, HttpRequest.BodyPublishers.noBody());
                HttpResponse<String> response = client.send(builder.build(), boundedBody(path.endsWith("/result") ? 128 * 1024 * 1024 : 8 * 1024 * 1024));
                // Nothing from a Core of another API version is trusted, its error replies included.
                String mismatch = Versions.coreMismatch(response.headers().firstValue(Versions.CORE_HEADER).orElse(null));
                if (mismatch != null) throw new ApiException(409, mismatch, Versions.MISMATCH);
                JsonObject parsed;
                try { parsed = JsonParser.parseString(response.body()).getAsJsonObject(); }
                catch (RuntimeException e) { throw new ApiException(response.statusCode(), "Core returned invalid JSON."); }
                if (response.statusCode() / 100 != 2) {
                    String message = parsed.has("error") && parsed.get("error").isJsonPrimitive()
                            ? parsed.get("error").getAsString() : "Core HTTP " + response.statusCode();
                    String code = parsed.has("code") && parsed.get("code").isJsonPrimitive() ? parsed.get("code").getAsString() : "";
                    throw new ApiException(response.statusCode(), message, code);
                }
                return parsed;
            } catch (Exception e) { throw new CompletionException(e); }
        }, executor);
    }
    CompletableFuture<ReplayPayload> result(String path) {
        return request("GET", path, null).thenApplyAsync(ReplayPayload::parse, executor);
    }
    static HttpResponse.BodyHandler<String> boundedBody(int limit) {
        return info -> new HttpResponse.BodySubscriber<>() {
            final HttpResponse.BodySubscriber<String> delegate = HttpResponse.BodySubscribers.ofString(java.nio.charset.StandardCharsets.UTF_8);
            java.util.concurrent.Flow.Subscription subscription;
            long received;
            boolean rejected;
            public java.util.concurrent.CompletionStage<String> getBody() { return delegate.getBody(); }
            public void onSubscribe(java.util.concurrent.Flow.Subscription value) { subscription = value; delegate.onSubscribe(value); }
            public void onNext(java.util.List<java.nio.ByteBuffer> chunks) {
                if (rejected) return;
                for (var chunk : chunks) received += chunk.remaining();
                if (received > limit) {
                    rejected = true; subscription.cancel();
                    delegate.onError(new java.io.IOException("Core response exceeds the allowed size."));
                } else delegate.onNext(chunks);
            }
            public void onError(Throwable error) { if (!rejected) delegate.onError(error); }
            public void onComplete() { if (!rejected) delegate.onComplete(); }
        };
    }
    static Throwable cause(Throwable e) {
        while ((e instanceof CompletionException || e instanceof java.util.concurrent.ExecutionException) && e.getCause() != null)
            e = e.getCause();
        return e;
    }
    /** Lets requests already sent finish (callers bound their own wait); nothing new is accepted. */
    @Override public void close() { executor.shutdown(); }
    static final class TokenException extends RuntimeException {
        TokenException(String message) { super(message); }
    }
    static final class ApiException extends RuntimeException {
        final int status;
        /** Stable machine-readable reason, such as {@code player_job_limit}; empty when Core sends none. */
        final String code;
        ApiException(int status, String message) { this(status, message, ""); }
        ApiException(int status, String message, String code) { super(message); this.status = status; this.code = code; }
    }
}
