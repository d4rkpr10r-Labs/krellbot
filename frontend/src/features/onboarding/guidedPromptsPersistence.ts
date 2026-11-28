/**
 * Guided prompts preference (M39.5, KRELL-SIGNAL-20261010).
 *
 * "Skip" on the Krell welcome turns off the welcome, tour offers and tips.
 * It is remembered in localStorage under a versioned key; Settings turns it
 * back on. Every storage read and write is guarded, and the default is on.
 * If storage rejects a write, the choice still holds for this page load.
 */
import { useSyncExternalStore } from "react";

export const GUIDED_PROMPTS_STORAGE_KEY = "krellbot.guidedPrompts.v1";

const CHANGE_EVENT = "krellbot:guided-prompts";

/** Set only while storage rejects writes, so Skip still works this session. */
let sessionOverride: boolean | null = null;

function parse(raw: string | null): boolean {
  if (!raw) return true;
  try {
    const value: unknown = JSON.parse(raw);
    if (value && typeof value === "object") {
      const { version, enabled } = value as { version?: unknown; enabled?: unknown };
      if (version === 1 && typeof enabled === "boolean") return enabled;
    }
  } catch {
    // Fall through to the default.
  }
  return true;
}

export function readGuidedPrompts(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const raw = window.localStorage.getItem(GUIDED_PROMPTS_STORAGE_KEY);
    return raw === null && sessionOverride !== null ? sessionOverride : parse(raw);
  } catch {
    return sessionOverride ?? true;
  }
}

export function writeGuidedPrompts(enabled: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(GUIDED_PROMPTS_STORAGE_KEY, JSON.stringify({ version: 1, enabled }));
    sessionOverride = null;
  } catch {
    sessionOverride = enabled;
  }
  try {
    window.dispatchEvent(new Event(CHANGE_EVENT));
  } catch {
    // Best-effort only.
  }
}

export function subscribeGuidedPrompts(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  const onStorage = (event: StorageEvent): void => {
    if (event.key === null || event.key === GUIDED_PROMPTS_STORAGE_KEY) onChange();
  };
  window.addEventListener(CHANGE_EVENT, onChange);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onStorage);
  };
}

/** Whether the welcome, tour offers and tips show. Updates when it changes. */
export function useGuidedPrompts(): boolean {
  return useSyncExternalStore(subscribeGuidedPrompts, readGuidedPrompts, () => true);
}
