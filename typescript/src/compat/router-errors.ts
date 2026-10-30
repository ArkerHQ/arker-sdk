const timeoutBrand = Symbol.for("@arker-ai/sdk/RouterLookupTimeoutError");

export class RouterLookupTimeoutError extends Error {
  readonly [timeoutBrand] = true;

  static [Symbol.hasInstance](value: unknown): boolean {
    if (this !== RouterLookupTimeoutError) return Function.prototype[Symbol.hasInstance].call(this, value);
    return value instanceof Error && Reflect.get(value, timeoutBrand) === true;
  }

  constructor(id: string) {
    super(`Ownership lookup timed out for ${id}`);
    this.name = "RouterLookupTimeoutError";
  }
}
