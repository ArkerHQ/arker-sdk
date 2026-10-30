// These values cross native brand checks; their containing API property is
// observed, but their data and built-in methods are not vendor API surfaces.
function opaque(value: object): boolean {
  if (Array.isArray(value) || ArrayBuffer.isView(value) || value instanceof ArrayBuffer || value instanceof Promise
    || (typeof SharedArrayBuffer !== "undefined" && value instanceof SharedArrayBuffer)
    || value instanceof Error || value instanceof Date || value instanceof RegExp
    || value instanceof Map || value instanceof Set || value instanceof WeakMap || value instanceof WeakSet
    || (typeof ReadableStream !== "undefined" && value instanceof ReadableStream)
    || (typeof WritableStream !== "undefined" && value instanceof WritableStream)) return true;
  // A fixed own method cannot be rebound without violating Proxy invariants.
  // Leave such objects untouched rather than break private-field receivers.
  return Object.values(Object.getOwnPropertyDescriptors(value)).some(descriptor => (
    !descriptor.configurable && !descriptor.writable && typeof descriptor.value === "function"
  ));
}

export function traceVendor<T extends object>(target: T, prefix: string, calls: Set<string>): T {
  const objects = new WeakMap<object, Map<string, object>>();
  function observe<T>(target: T, prefix: string): T {
    if (target === null || (typeof target !== "object" && typeof target !== "function") || opaque(target)) return target;
    let paths = objects.get(target);
    if (!paths) { paths = new Map(); objects.set(target, paths); }
    const existing = paths.get(prefix);
    if (existing) return existing as T;
    const methods = new Map<PropertyKey, { value: Function; wrapper: Function }>();
    const proxy = new Proxy(target, {
      get(object, property) {
        const value = Reflect.get(object, property, object);
        const publicName = typeof property === "string" && !property.startsWith("_") && !property.startsWith("#")
          && !["then", "constructor", "prototype"].includes(property) && Reflect.has(object, property);
        const api = `${prefix}.${String(property)}`;
        if (publicName && typeof value !== "function") calls.add(api);
        const own = Reflect.getOwnPropertyDescriptor(object, property);
        // Proxy invariants prohibit replacing a frozen own property's value.
        if (own && !own.configurable && "value" in own && !own.writable) return value;
        if (property === "constructor" || property === "prototype") return value;
        if (typeof value === "function") {
          const cached = methods.get(property);
          if (cached?.value === value) return cached.wrapper;
          const wrapper = (...args: unknown[]) => {
            if (publicName) calls.add(api);
            const result = Reflect.apply(value, object, args);
            const children = ["Sandbox.fork", "Sandbox.static.fork"].includes(api);
            const instance = prefix.endsWith(".static") && ["create", "connect", "get", "createIfNotExists"].includes(String(property));
            const handle = ["Sandbox.commands.run", "Sandbox.commands.connect", "Sandbox.pty.create", "Sandbox.pty.connect"].includes(api);
            if (!children && !instance && !handle) return result === object ? proxy : result;
            const returned = (result: unknown) => {
              if (children && Array.isArray(result)) {
                return result.map(child => child instanceof Error ? child : observe(child, "Sandbox"));
              }
              if (instance) {
                return observe(result, prefix.slice(0, -".static".length));
              }
              if (handle && result && typeof (result as { wait?: unknown }).wait === "function") {
                return observe(result, "CommandHandle");
              }
              return result;
            };
            return result instanceof Promise ? result.then(returned) : returned(result);
          };
          methods.set(property, { value, wrapper });
          return wrapper;
        }
        return publicName ? observe(value, api) : value;
      },
    });
    paths.set(prefix, proxy);
    return proxy;
  }
  return observe(target, prefix);
}
