/**
 * Outline the element Krell is talking about (M38.17).
 *
 * The engine's `highlight` event names a validated test id. The element
 * whose `data-testid` is exactly that id gets `data-krell-highlight="true"`
 * for 4 seconds; `krell.css` draws it as an outline only, with no motion
 * and nothing laid over the controls. An id no element has does nothing.
 *
 * The id is model-chosen text, so it never becomes part of a selector: the
 * element is found by comparing each `data-testid` value as a string.
 */
import { useEffect } from "react";
import "./krell.css";

const HIGHLIGHT_MS = 4000;
const ATTRIBUTE = "data-krell-highlight";

function elementWithTestId(testId: string): Element | null {
  for (const element of document.querySelectorAll("[data-testid]")) {
    if (element.getAttribute("data-testid") === testId) return element;
  }
  return null;
}

/**
 * Highlight `[data-testid=testId]` for 4 seconds. `nonce` changes with each
 * highlight event, so Krell naming the same element again outlines it again.
 */
export function useKrellHighlight(testId: string | null, nonce = 0): void {
  useEffect(() => {
    if (!testId) return;
    const element = elementWithTestId(testId);
    if (!element) return;
    element.setAttribute(ATTRIBUTE, "true");
    const timer = window.setTimeout(() => element.removeAttribute(ATTRIBUTE), HIGHLIGHT_MS);
    return () => {
      window.clearTimeout(timer);
      element.removeAttribute(ATTRIBUTE);
    };
  }, [testId, nonce]);
}
