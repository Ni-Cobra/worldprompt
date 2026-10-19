package dev.worldprompt;

import com.mojang.serialization.MapCodec;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import net.minecraft.core.Holder;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.gametest.framework.GameTestInstance;
import net.minecraft.gametest.framework.TestData;
import net.minecraft.gametest.framework.TestEnvironmentDefinition;
import net.minecraft.network.chat.MutableComponent;
import net.minecraft.network.chat.Component;

/** Adapts an unchanged Fabric GameTest method body to NeoForge's registry-backed test instances. */
public final class RegisteredMethodGameTestInstance extends GameTestInstance {
    private final Class<?> testClass;
    private final String methodName;

    public RegisteredMethodGameTestInstance(TestData<Holder<TestEnvironmentDefinition<?>>> data,
                                            Class<?> testClass, String methodName) {
        super(data);
        this.testClass = testClass;
        this.methodName = methodName;
    }

    @Override
    public void run(GameTestHelper helper) {
        try {
            Object test = testClass.getConstructor().newInstance();
            Method method = testClass.getMethod(methodName, GameTestHelper.class);
            method.invoke(test, helper);
        } catch (InvocationTargetException invocation) {
            Throwable cause = invocation.getCause();
            if (cause instanceof RuntimeException runtime) {
                throw runtime;
            }
            if (cause instanceof Error error) {
                throw error;
            }
            throw new RuntimeException("GameTest " + testClass.getSimpleName() + "." + methodName
                    + " failed: " + (cause == null ? "unknown cause" : cause.getMessage()), cause);
        } catch (ReflectiveOperationException reflection) {
            throw new IllegalStateException("Cannot invoke GameTest " + testClass.getName() + "." + methodName, reflection);
        }
    }

    @Override
    public MapCodec<? extends GameTestInstance> codec() {
        return MapCodec.unit(() -> this);
    }

    @Override
    protected MutableComponent typeDescription() {
        return Component.literal(testClass.getSimpleName() + "." + methodName);
    }
}
