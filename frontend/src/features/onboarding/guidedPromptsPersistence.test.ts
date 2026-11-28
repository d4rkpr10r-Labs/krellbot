/**
 * Guided prompts preference (M39.5): on by default, remembered once turned
 * off, and safe when storage is missing or throws.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  GUIDED_PROMPTS_STORAGE_KEY,
  readGuidedPrompts,
  subscribeGuidedPrompts,
  writeGuidedPrompts,
} from "./guidedPromptsPersistence";

describe("guided prompts preference", () => {
  beforeEach(() => {
    window.localStorage.clear();
    writeGuidedPrompts(true);
    window.localStorage.clear();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("is on when nothing is stored", () => {
    expect(readGuidedPrompts()).toBe(true);
  });

  it("remembers off and on", () => {
    writeGuidedPrompts(false);
    expect(readGuidedPrompts()).toBe(false);
    expect(window.localStorage.getItem(GUIDED_PROMPTS_STORAGE_KEY)).toContain('"version":1');
    writeGuidedPrompts(true);
    expect(readGuidedPrompts()).toBe(true);
  });

  it("falls back to on for unreadable or unknown stored values", () => {
    for (const raw of ["not json", "{}", '{"version":2,"enabled":false}', '{"version":1,"enabled":"no"}', "null"]) {
      window.localStorage.setItem(GUIDED_PROMPTS_STORAGE_KEY, raw);
      expect(readGuidedPrompts()).toBe(true);
    }
  });

  it("stays on when reading storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readGuidedPrompts()).toBe(true);
  });

  it("still honours Skip for this session when writing storage throws", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(() => writeGuidedPrompts(false)).not.toThrow();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readGuidedPrompts()).toBe(false);
  });

  it("tells subscribers when the preference changes", () => {
    const seen = vi.fn();
    const stop = subscribeGuidedPrompts(seen);
    writeGuidedPrompts(false);
    expect(seen).toHaveBeenCalledTimes(1);
    stop();
    writeGuidedPrompts(true);
    expect(seen).toHaveBeenCalledTimes(1);
  });
});
