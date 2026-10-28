import assert from "node:assert/strict";
import { test } from "node:test";
import {
  NotFoundError,
  type ModalClient,
  type App,
  type Image,
  type SandboxCreateParams,
} from "modal";
import { createNativeModalProvider } from "../src/compat/modal-provider.js";

test("Modal bridge preserves native handles, options and instances without rebuilding or exposing extra ports", async () => {
  const app = {} as App;
  const image = {} as Image;
  const params = {
    blockNetwork: true,
    encryptedPorts: [8443],
    command: ["sleep", "30"],
  } satisfies SandboxCreateParams;
  const instance = { sandboxId: "sb-test", poll: async () => null };
  const client = {
    apps: {
      fromName: async () => {
        throw new Error("Must not replace the supplied app");
      },
    },
    images: {
      fromRegistry: () => {
        throw new Error("Must not replace the supplied image");
      },
    },
    sandboxes: {
      create: async (
        owner: App,
        source: Image,
        options: SandboxCreateParams,
      ) => {
        assert.equal(owner, app);
        assert.equal(source, image);
        assert.equal(options, params);
        return instance;
      },
    },
  } as unknown as ModalClient;
  const box = await createNativeModalProvider(() => client).instances.create({
    modalApp: app,
    modalImage: image,
    modalCreateParams: params,
  });
  assert.equal(box.getInstance(), instance);
});

test("Modal ownership treats only typed absence as not found and preserves other native errors", async () => {
  let failure: unknown = new NotFoundError("missing");
  const client = {
    sandboxes: {
      fromId: async () => {
        throw failure;
      },
    },
  } as unknown as ModalClient;
  const provider = createNativeModalProvider(() => client);
  assert.equal(await provider.instances.getById("sb-missing"), null);
  failure = Object.assign(new Error("forbidden"), { code: 7 });
  for (const operation of [
    () => provider.instances.getById("sb-denied"),
    () => provider.instances.destroy("sb-denied"),
  ]) {
    await assert.rejects(operation(), (error) => error === failure);
  }
});
