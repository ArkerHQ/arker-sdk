export function traceVendor<T extends object>(target: T, prefix: string, calls: Set<string>): T {
  return new Proxy(target, {
    get(object, property) {
      const value = Reflect.get(object, property, object);
      if (typeof property !== "string") return value;
      const api = `${prefix}.${property}`;
      if (typeof value === "function") {
        return (...args: unknown[]) => {
          const result = Reflect.apply(value, object, args);
          const observed = (result: unknown) => {
            calls.add(api);
            if (prefix.endsWith(".static") && ["create", "get", "createIfNotExists"].includes(property)) {
              return traceVendor(result as object, prefix.slice(0, -".static".length), calls);
            }
            return result;
          };
          return result instanceof Promise ? result.then(observed) : observed(result);
        };
      }
      return value && typeof value === "object" ? traceVendor(value, api, calls) : value;
    },
  });
}
