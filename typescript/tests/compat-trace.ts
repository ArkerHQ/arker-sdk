const observedProperties = new Set([
  "Sandbox.sandboxId", "Sandbox.sandboxDomain", "Sandbox.trafficAccessToken",
  "Sandbox.commands", "Sandbox.files", "Sandbox.pty", "Sandbox.git", "Sandbox.commands.supportsStdinClose",
  "CommandHandle.pid", "CommandHandle.stdout", "CommandHandle.stderr", "CommandHandle.exitCode", "CommandHandle.error",
]);

export function traceVendor<T extends object>(target: T, prefix: string, calls: Set<string>): T {
  return new Proxy(target, {
    get(object, property) {
      const value = Reflect.get(object, property, object);
      if (typeof property !== "string") return value;
      const api = `${prefix}.${property}`;
      if (typeof value === "function") {
        return (...args: unknown[]) => {
          calls.add(api);
          const result = Reflect.apply(value, object, args);
          const observed = (result: unknown) => {
            if (prefix.endsWith(".static") && ["create", "connect", "get", "createIfNotExists"].includes(property)) {
              return traceVendor(result as object, prefix.slice(0, -".static".length), calls);
            }
            if (["Sandbox.commands.run", "Sandbox.commands.connect", "Sandbox.pty.create", "Sandbox.pty.connect"].includes(api)
              && result && typeof (result as { wait?: unknown }).wait === "function") {
              return traceVendor(result as object, "CommandHandle", calls);
            }
            return result;
          };
          return result instanceof Promise ? result.then(observed) : observed(result);
        };
      }
      if (observedProperties.has(api)) calls.add(api);
      return value && typeof value === "object" ? traceVendor(value, api, calls) : value;
    },
  });
}
