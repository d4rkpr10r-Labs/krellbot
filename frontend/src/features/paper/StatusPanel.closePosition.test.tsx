/**
 * Close position in the armed paper branch: the operator types the pair back,
 * then the panel POSTs `positions.close` once and shows the backend's own
 * words. The control stays disabled until the typed text is exactly the pair;
 * the backend checks it again. A refusal keeps the confirmed state, a
 * transport failure is an unknown outcome and is never retried, and a live
 * status shows no close control here.
 *
 * Every client and `fetch` here is an in-memory vi.fn() fake.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StatusPanel } from "./StatusPanel";
import { CLOSE_POSITION_DISCLOSURE, STOP_RUN_DISCLOSURE } from "./stopRunDisclosure";
import type { PaperClient, PaperCommandResult, PaperStatus } from "./client";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function armedStatus(extra: Partial<PaperStatus> = {}): PaperStatus {
  return {
    schema_version: "1",
    armed: true,
    venue: "kraken",
    pair: "SUIUSD",
    entries_paused: false,
    mode: "paper",
    pack_id: "trend-follow",
    ...extra,
  };
}

function makeClient(overrides: Partial<PaperClient> = {}): PaperClient {
  return {
    getStatus: vi.fn().mockResolvedValue(armedStatus()),
    pauseEntries: vi.fn(),
    resumeEntries: vi.fn(),
    disarm: vi.fn(),
    ...overrides,
  } as PaperClient;
}

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

const CLOSED: PaperCommandResult = {
  schema_version: "1",
  code: "position_closed",
  ok: true,
  effect: "changed",
  message:
    "Position closed: only this strategy's quantity was sold, after its stop was cancelled. New buys stay paused until you resume them, or stop the run and arm it again.",
};

function describedText(control: HTMLElement): string {
  return (control.getAttribute("aria-describedby") ?? "")
    .split(/\s+/)
    .filter(Boolean)
    .map((id) => document.getElementById(id)?.textContent ?? "")
    .join(" ");
}

describe("close-position copy", () => {
  it("says the close cancels the stop, sells only the strategy's quantity and pauses new buys", () => {
    expect(CLOSE_POSITION_DISCLOSURE).toBe(
      "Close position cancels the resting stop, then sells only the quantity this strategy owns on this pair, never other holdings of the same coin. New buys stay paused until you resume them.",
    );
    expect(STOP_RUN_DISCLOSURE).toContain("use Close position");
  });
});

describe("StatusPanel Close position", () => {
  it("enables the close only when the typed text is exactly the pair", async () => {
    render(<StatusPanel client={makeClient()} />);
    const input = await screen.findByTestId("paper-close-position-confirm");
    const button = screen.getByRole("button", { name: /^close position$/i }) as HTMLButtonElement;
    expect((input as HTMLInputElement).value).toBe("");
    expect(button.disabled).toBe(true);
    expect(screen.getByLabelText(/type SUIUSD to close the position/i)).toBe(input);
    expect(describedText(button)).toContain(CLOSE_POSITION_DISCLOSURE);

    for (const typed of ["suiusd", "SUIUSD ", "SUI"]) {
      fireEvent.change(input, { target: { value: typed } });
      expect(button.disabled).toBe(true);
    }
    fireEvent.change(input, { target: { value: "SUIUSD" } });
    expect(button.disabled).toBe(false);
  });

  it("POSTs positions.close once with the typed pair, shows the backend's words and re-reads status", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(CLOSED));
    vi.stubGlobal("fetch", fetchMock);
    const getStatus = vi
      .fn()
      .mockResolvedValueOnce(armedStatus())
      .mockResolvedValue(armedStatus({ entries_paused: true }));
    render(<StatusPanel client={makeClient({ getStatus })} />);

    fireEvent.change(await screen.findByTestId("paper-close-position-confirm"), {
      target: { value: "SUIUSD" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^close position$/i }));

    expect((await screen.findByTestId("paper-close-position-ok")).textContent).toBe(CLOSED.message);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/commands");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      schema_version: "1",
      command: "positions.close",
      payload: { venue: "kraken", pair: "SUIUSD", confirmation: "SUIUSD" },
    });
    await waitFor(() => {
      expect(getStatus).toHaveBeenCalledTimes(2);
    });
    expect(screen.getByTestId("paper-status-entries").textContent).toBe("Entries paused");
    expect((screen.getByTestId("paper-close-position-confirm") as HTMLInputElement).value).toBe("");
  });

  it("keeps the confirmed state and shows the backend's refusal", async () => {
    const refusal: PaperCommandResult = {
      schema_version: "1",
      code: "obligation_open",
      ok: false,
      effect: "refused",
      message: "An order on this venue is not settled yet. Nothing was sent.",
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(refusal));
    vi.stubGlobal("fetch", fetchMock);
    const getStatus = vi.fn().mockResolvedValue(armedStatus());
    render(<StatusPanel client={makeClient({ getStatus })} />);

    fireEvent.change(await screen.findByTestId("paper-close-position-confirm"), {
      target: { value: "SUIUSD" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^close position$/i }));

    expect((await screen.findByTestId("paper-status-error")).textContent).toBe(refusal.message);
    expect(screen.queryByTestId("paper-close-position-ok")).toBeNull();
    expect(screen.getByTestId("paper-status-entries").textContent).toBe("Entries active");
    expect(getStatus).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("re-reads status after a close that changed state without selling", async () => {
    const rejected: PaperCommandResult = {
      schema_version: "1",
      code: "outcome_unknown",
      ok: false,
      effect: "changed",
      message: "The close's outcome is not known yet. It is never sent again.",
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(rejected)));
    const getStatus = vi.fn().mockResolvedValue(armedStatus());
    render(<StatusPanel client={makeClient({ getStatus })} />);

    fireEvent.change(await screen.findByTestId("paper-close-position-confirm"), {
      target: { value: "SUIUSD" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^close position$/i }));

    expect((await screen.findByTestId("paper-status-error")).textContent).toBe(rejected.message);
    await waitFor(() => {
      expect(getStatus).toHaveBeenCalledTimes(2);
    });
  });

  it("treats a transport failure as an unknown outcome and never retries", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({}, 502));
    vi.stubGlobal("fetch", fetchMock);
    render(<StatusPanel client={makeClient()} />);

    fireEvent.change(await screen.findByTestId("paper-close-position-confirm"), {
      target: { value: "SUIUSD" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^close position$/i }));

    expect((await screen.findByTestId("paper-status-unknown")).textContent).toMatch(
      /^Close position outcome unknown: positions\.close failed: 502\./,
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows no close control for a live status", async () => {
    render(<StatusPanel client={makeClient({ getStatus: vi.fn().mockResolvedValue(armedStatus({ mode: "live" })) })} />);
    expect(await screen.findByText(/Controls unavailable/)).toBeDefined();
    expect(screen.queryByTestId("paper-close-position")).toBeNull();
    expect(screen.queryByRole("button", { name: /close position/i })).toBeNull();
  });
});
