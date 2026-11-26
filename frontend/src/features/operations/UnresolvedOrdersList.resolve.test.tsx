import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { UnresolvedOrdersList } from "./UnresolvedOrdersList";
import type { OperationsClient, ResolveOrderResult, UnresolvedOrderRow } from "./client";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const COID = "coid-stuck-CANARY";

function order(over: Partial<UnresolvedOrderRow> = {}): UnresolvedOrderRow {
  return {
    client_order_id: COID,
    age_seconds: 90,
    last_lookup_result: "none",
    ...over,
  };
}

function resolved(over: Partial<ResolveOrderResult> = {}): ResolveOrderResult {
  return {
    schema_version: "1",
    code: "order_resolved",
    ok: true,
    effect: "changed",
    message: "Order resolved. Nothing was sent to the exchange.",
    client_order_id: COID,
    outcome: "filled",
    filled_qty: "0.25",
    evidence: "operator_asserted",
    attributed: true,
    ...over,
  };
}

function makeClient(over: Partial<OperationsClient> = {}): OperationsClient {
  return {
    getOperations: vi.fn(),
    promote: vi.fn(),
    pauseEntries: vi.fn(),
    resumeEntries: vi.fn(),
    engageKill: vi.fn(),
    releaseKill: vi.fn(),
    ackAlert: vi.fn(),
    armLive: vi.fn(),
    ...over,
  };
}

function expectNothingElseSent(client: OperationsClient): void {
  for (const name of [
    "promote",
    "pauseEntries",
    "resumeEntries",
    "engageKill",
    "releaseKill",
    "ackAlert",
    "armLive",
  ] as const) {
    expect(client[name]).not.toHaveBeenCalled();
  }
}

async function openResolve(): Promise<HTMLElement> {
  fireEvent.click(await screen.findByTestId(`ops-unresolved-resolve-${COID}`));
  return screen.findByTestId("ops-unresolved-resolve-form");
}

describe("UnresolvedOrdersList resolve", () => {
  it("offers no resolve action when the client cannot resolve orders", async () => {
    const client = makeClient({ listUnresolvedOrders: vi.fn().mockResolvedValue([order()]) });
    render(<UnresolvedOrdersList client={client} />);
    expect(await screen.findByTestId(`ops-unresolved-row-${COID}`)).toBeTruthy();
    expect(screen.queryByTestId(`ops-unresolved-resolve-${COID}`)).toBeNull();
  });

  it("resolves from the recorded venue lookup without asking for the client order ID", async () => {
    const listUnresolvedOrders = vi
      .fn()
      .mockResolvedValueOnce([order({ last_lookup_result: "filled" })])
      .mockResolvedValueOnce([]);
    const resolveOrder = vi
      .fn()
      .mockResolvedValue(resolved({ evidence: "operator_accepted_venue_observation" }));
    const client = makeClient({ listUnresolvedOrders, resolveOrder });
    render(<UnresolvedOrdersList client={client} />);

    const form = await openResolve();
    expect(form.textContent).toContain("filled");
    expect(within(form).queryByTestId("ops-unresolved-confirm")).toBeNull();
    fireEvent.click(within(form).getByTestId("ops-unresolved-accept-evidence"));

    await waitFor(() => expect(resolveOrder).toHaveBeenCalledTimes(1));
    expect(resolveOrder).toHaveBeenCalledWith({ client_order_id: COID });
    expect((await screen.findByTestId("ops-unresolved-feedback")).textContent).toContain(
      "Nothing was sent to the exchange.",
    );
    await waitFor(() => expect(listUnresolvedOrders).toHaveBeenCalledTimes(2));
    expect(await screen.findByTestId("ops-unresolved-empty")).toBeTruthy();
    expectNothingElseSent(client);
  });

  it("asserts an executed quantity only after the client order ID is typed back exactly", async () => {
    const resolveOrder = vi.fn().mockResolvedValue(resolved());
    const client = makeClient({
      listUnresolvedOrders: vi.fn().mockResolvedValue([order()]),
      resolveOrder,
    });
    render(<UnresolvedOrdersList client={client} />);

    const form = await openResolve();
    expect(within(form).queryByTestId("ops-unresolved-accept-evidence")).toBeNull();
    const submit = within(form).getByTestId("ops-unresolved-submit") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    fireEvent.click(within(form).getByLabelText("It executed"));
    fireEvent.change(within(form).getByTestId("ops-unresolved-executed-qty"), {
      target: { value: "0.25" },
    });
    const confirm = within(form).getByTestId("ops-unresolved-confirm");
    for (const wrong of ["", "coid-stuck", ` ${COID}`, COID.toLowerCase()]) {
      fireEvent.change(confirm, { target: { value: wrong } });
      expect(submit.disabled).toBe(true);
    }
    fireEvent.change(confirm, { target: { value: COID } });
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() => expect(resolveOrder).toHaveBeenCalledTimes(1));
    expect(resolveOrder).toHaveBeenCalledWith({
      client_order_id: COID,
      outcome: "executed",
      executed_qty: "0.25",
      confirmation: COID,
    });
    expectNothingElseSent(client);
  });

  it("asserts that the order did not execute without an executed quantity", async () => {
    const resolveOrder = vi.fn().mockResolvedValue(resolved({ outcome: "absent", filled_qty: "0" }));
    const client = makeClient({
      listUnresolvedOrders: vi.fn().mockResolvedValue([order()]),
      resolveOrder,
    });
    render(<UnresolvedOrdersList client={client} />);

    const form = await openResolve();
    fireEvent.click(within(form).getByLabelText("It did not execute"));
    expect(
      (within(form).getByTestId("ops-unresolved-executed-qty") as HTMLInputElement).disabled,
    ).toBe(true);
    fireEvent.change(within(form).getByTestId("ops-unresolved-confirm"), {
      target: { value: COID },
    });
    fireEvent.click(within(form).getByTestId("ops-unresolved-submit"));

    await waitFor(() =>
      expect(resolveOrder).toHaveBeenCalledWith({
        client_order_id: COID,
        outcome: "not_executed",
        confirmation: COID,
      }),
    );
  });

  it("asks for a typed assertion when the last lookup shows the order still open", async () => {
    const resolveOrder = vi.fn().mockResolvedValue(resolved({ outcome: "absent", filled_qty: "0" }));
    const client = makeClient({
      listUnresolvedOrders: vi.fn().mockResolvedValue([order({ last_lookup_result: "open" })]),
      resolveOrder,
    });
    render(<UnresolvedOrdersList client={client} />);

    const form = await openResolve();
    expect(within(form).queryByTestId("ops-unresolved-accept-evidence")).toBeNull();
    expect(form.textContent).toContain("may still be working");
    fireEvent.click(within(form).getByLabelText("It did not execute"));
    const submit = within(form).getByTestId("ops-unresolved-submit") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.change(within(form).getByTestId("ops-unresolved-confirm"), {
      target: { value: COID },
    });
    fireEvent.click(submit);

    await waitFor(() =>
      expect(resolveOrder).toHaveBeenCalledWith({
        client_order_id: COID,
        outcome: "not_executed",
        confirmation: COID,
      }),
    );
    expectNothingElseSent(client);
  });

  it("switches to a typed assertion when the server says the order is still working", async () => {
    const listUnresolvedOrders = vi.fn().mockResolvedValue([order({ last_lookup_result: "filled" })]);
    const resolveOrder = vi
      .fn()
      .mockResolvedValueOnce({
        schema_version: "1",
        code: "order_still_working",
        ok: false,
        effect: "refused",
        message:
          "The last venue lookup shows this order still working. Cancel it at the exchange and wait for the next lookup, or say what happened and type its client order ID.",
      })
      .mockResolvedValueOnce(resolved({ filled_qty: "0.4" }));
    const client = makeClient({ listUnresolvedOrders, resolveOrder });
    render(<UnresolvedOrdersList client={client} />);

    const form = await openResolve();
    fireEvent.click(within(form).getByTestId("ops-unresolved-accept-evidence"));

    const refusal = await screen.findByTestId("ops-unresolved-resolve-error");
    expect(refusal.textContent).toContain("still working");
    const assertForm = screen.getByTestId("ops-unresolved-resolve-form");
    expect(within(assertForm).queryByTestId("ops-unresolved-accept-evidence")).toBeNull();
    expect(listUnresolvedOrders).toHaveBeenCalledTimes(1);

    fireEvent.click(within(assertForm).getByLabelText("It executed"));
    fireEvent.change(within(assertForm).getByTestId("ops-unresolved-executed-qty"), {
      target: { value: "0.4" },
    });
    fireEvent.change(within(assertForm).getByTestId("ops-unresolved-confirm"), {
      target: { value: COID },
    });
    fireEvent.click(within(assertForm).getByTestId("ops-unresolved-submit"));

    await waitFor(() => expect(resolveOrder).toHaveBeenCalledTimes(2));
    expect(resolveOrder).toHaveBeenNthCalledWith(1, { client_order_id: COID });
    expect(resolveOrder).toHaveBeenNthCalledWith(2, {
      client_order_id: COID,
      outcome: "executed",
      executed_qty: "0.4",
      confirmation: COID,
    });
    expectNothingElseSent(client);
  });

  it("shows a refusal and keeps the order listed and the form open", async () => {
    const listUnresolvedOrders = vi.fn().mockResolvedValue([order()]);
    const resolveOrder = vi.fn().mockResolvedValue({
      schema_version: "1",
      code: "tick_running",
      ok: false,
      effect: "refused",
      message: "A tick for this venue is running. Try again in a moment.",
    });
    const client = makeClient({ listUnresolvedOrders, resolveOrder });
    render(<UnresolvedOrdersList client={client} />);

    const form = await openResolve();
    fireEvent.click(within(form).getByLabelText("It did not execute"));
    fireEvent.change(within(form).getByTestId("ops-unresolved-confirm"), {
      target: { value: COID },
    });
    fireEvent.click(within(form).getByTestId("ops-unresolved-submit"));

    const refusal = await screen.findByTestId("ops-unresolved-resolve-error");
    expect(refusal.getAttribute("role")).toBe("alert");
    expect(refusal.textContent).toContain("A tick for this venue is running.");
    expect(screen.getByTestId(`ops-unresolved-row-${COID}`)).toBeTruthy();
    expect(screen.getByTestId("ops-unresolved-resolve-form")).toBeTruthy();
    expect(listUnresolvedOrders).toHaveBeenCalledTimes(1);
  });

  it("reports a failed request without claiming the order was resolved", async () => {
    const resolveOrder = vi
      .fn()
      .mockRejectedValue(new Error("operations.resolve_order failed: 403"));
    const client = makeClient({
      listUnresolvedOrders: vi.fn().mockResolvedValue([order({ last_lookup_result: "absent" })]),
      resolveOrder,
    });
    render(<UnresolvedOrdersList client={client} />);

    const form = await openResolve();
    fireEvent.click(within(form).getByTestId("ops-unresolved-accept-evidence"));

    const refusal = await screen.findByTestId("ops-unresolved-resolve-error");
    expect(refusal.textContent).toContain("403");
    expect(screen.queryByTestId("ops-unresolved-feedback")).toBeNull();
  });
});
