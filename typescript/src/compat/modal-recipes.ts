import type { App, Image, ModalClient, Secret } from "modal";

/** A sandbox grouping request, without a fabricated native app ID. */
export class ModalAppRecipe {
  constructor(readonly name: string, readonly options: { createIfMissing?: boolean; environment?: string } = {}) {}
  native(client: ModalClient): Promise<App> { return client.apps.fromName(this.name, this.options); }
  group(): Record<string, string> {
    if (this.options.createIfMissing !== true) throw new Error("Arker sandbox app grouping requires createIfMissing: true; existing Modal ownership cannot be resolved by Arker");
    if (typeof this.name !== "string" || !this.name) throw new TypeError("App name must be a nonempty string");
    const unknown = Object.keys(this.options).filter(key => key !== "createIfMissing" && key !== "environment");
    if (unknown.length) throw new Error(`Unsupported app recipe options: ${unknown.join(", ")}`);
    return { name: this.name, ...(this.options.environment !== undefined ? { environment: this.options.environment } : {}) };
  }
}

const images = new WeakMap<Image, { tag: string; secret?: Secret; app?: ModalAppRecipe }>();
const secrets = new WeakMap<Secret, Readonly<Record<string, string>>>();

export type RegistryImage = Image & { build(app: App | ModalAppRecipe): Promise<Image> };

export function registryImage(client: ModalClient, tag: string, secret?: Secret): RegistryImage {
  const image = client.images.fromRegistry(tag, secret);
  const recipe = { tag, secret, app: undefined as ModalAppRecipe | undefined };
  images.set(image, recipe);
  const build = image.build.bind(image);
  image.build = async (app: App | ModalAppRecipe) => {
    if (!(app instanceof ModalAppRecipe)) return build(app);
    if (recipe.app && recipe.app !== app) throw new Error("Image build app must match the sandbox app");
    recipe.app = app;
    return image;
  };
  return image as RegistryImage;
}

export function registry(image: unknown, app: unknown): string {
  const recipe = image && typeof image === "object" ? images.get(image as Image) : undefined;
  if (!recipe) throw new Error("Arker requires a local registry recipe; opaque native image handles are unsupported");
  if (recipe.app && recipe.app !== app) throw new Error("Image build app must match the sandbox app");
  registryCredentials(image);
  if (typeof recipe.tag !== "string" || !recipe.tag.trim()) throw new TypeError("Image registry reference must be nonempty");
  return recipe.tag;
}

export function registryCredentials(image: unknown): { username: string; password: string } | undefined {
  const recipe = image && typeof image === "object" ? images.get(image as Image) : undefined;
  if (recipe?.secret === undefined) return undefined;
  const values = secrets.get(recipe.secret);
  if (!values) throw new Error("Registry authentication requires a local secrets.fromObject recipe");
  if (!values.REGISTRY_USERNAME || !values.REGISTRY_PASSWORD) throw new Error("Registry secret requires REGISTRY_USERNAME and REGISTRY_PASSWORD");
  return { username: values.REGISTRY_USERNAME, password: values.REGISTRY_PASSWORD };
}

export async function buildNativeImage(image: Image, app: App): Promise<Image> {
  return images.has(image) ? image.build(app) : image;
}

export async function localSecret(client: ModalClient, entries: Record<string, string>, options?: Parameters<ModalClient["secrets"]["fromObject"]>[1]): Promise<Secret> {
  const secret = await client.secrets.fromObject(entries, options);
  secrets.set(secret, Object.freeze({ ...entries }));
  return secret;
}

export function secretEnvironment(values: unknown): Record<string, string> {
  if (values === undefined) return Object.create(null);
  if (!Array.isArray(values)) throw new TypeError("secrets must be an array");
  const result: Record<string, string> = Object.create(null);
  for (const value of values) {
    const environment = value && typeof value === "object" ? secrets.get(value) : undefined;
    if (!environment) throw new Error("Arker requires local secrets.fromObject recipes");
    Object.assign(result, environment);
  }
  return result;
}

export function creationDefaults(options: Record<string, unknown>): { env: Record<string, string>; workdir: string } {
  const env = secretEnvironment(options.secrets);
  if (options.env !== undefined) {
    if (!options.env || typeof options.env !== "object" || Array.isArray(options.env)) throw new TypeError("env must be a string record");
    Object.assign(env, options.env);
  }
  for (const [key, value] of Object.entries(env)) {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key) || typeof value !== "string") throw new TypeError("Invalid Modal environment");
  }
  const workdir = options.workdir ?? "/";
  if (typeof workdir !== "string" || !workdir.startsWith("/")) throw new TypeError("workdir must be an absolute path");
  return { env, workdir };
}

class RecipeService {}

/** Keep unrelated service methods bound to their native receiver. */
export function recipeService<T extends object, R extends object>(native: () => T, recipes: R): Omit<T, keyof R> & R {
  return new Proxy(Object.assign(new RecipeService(), recipes), { get(target, key, receiver) {
    if (Object.hasOwn(target, key)) return Reflect.get(target, key, receiver);
    const service = native();
    const value = Reflect.get(service, key);
    return typeof value === "function" ? value.bind(service) : value;
  } }) as Omit<T, keyof R> & R;
}

/** Named volumes remain native resources; Arker creation rejects these recipes. */
export class ModalVolumeRecipe {
  constructor(
    private readonly name: string,
    private readonly options: Parameters<ModalClient["volumes"]["fromName"]>[1],
  ) {}
  native(client: ModalClient) { return client.volumes.fromName(this.name, this.options); }
}

export async function nativeVolumes<T extends { volumes?: Record<string, unknown> }>(client: ModalClient, options: T): Promise<T> {
  if (!Object.values(options.volumes ?? {}).some(value => value instanceof ModalVolumeRecipe)) return options;
  const volumes = Object.fromEntries(await Promise.all(Object.entries(options.volumes!).map(async ([path, value]) =>
    [path, value instanceof ModalVolumeRecipe ? await value.native(client) : value])));
  return { ...options, volumes };
}
