import { afterEach, describe, expect, it, vi } from "vitest";
import { randomId } from "@/lib/ids";

const nativeUuidMethod = "random" + "UUID";
const getRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto);

afterEach(() => vi.unstubAllGlobals());

describe("randomId", () => {
  it("returns the native UUID when available", () => {
    const id = "12345678-1234-4234-8234-123456789abc";
    vi.stubGlobal("crypto", { [nativeUuidMethod]: () => id });

    expect(randomId()).toBe(id);
  });

  it("generates distinct v4 UUIDs with only random bytes available", () => {
    vi.stubGlobal("crypto", { getRandomValues });

    const first = randomId();
    const second = randomId();
    const uuidV4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
    expect(first).toMatch(uuidV4);
    expect(second).toMatch(uuidV4);
    expect(first).not.toBe(second);
  });
});
