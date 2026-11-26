import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { summarizeAlertKind } from "../operations/alertKindLabel";
import { FleetArmedList } from "./FleetArmedList";
import type {
  FleetArmedClient,
  FleetArmedDeployment,
  FleetArmedView,
  FleetFeedFreshness,
  FleetFreshnessClient,
  FleetFreshnessView,
} from "./client";

function deployment(
  overrides: Partial<FleetArmedDeployment> = {},
): FleetArmedDeployment {
  return {
    venue: "kraken",
    pair: "SUIUSD",
    mode: "paper",
    pack_id: "trend-follow",
    entries_paused: false,
    last_tick_ts_ms: 1_790_003_660_000,
    last_tick_result: "1 tick, no refusal",
    last_refusal_code: null,
    detail_href: "/?view=workstation&venue=kraken&pair=SUIUSD",
    ...overrides,
  };
}

const COINBASE = deployment({
  venue: "coinbase",
  pair: "BTC-USD",
  pack_id: "mean-revert",
  detail_href: "/?view=workstation&venue=coinbase&pair=BTC-USD",
});

function feed(overrides: Partial<FleetFeedFreshness> = {}): FleetFeedFreshness {
  return {
    venue: "kraken",
    pack_id: "trend-follow",
    pair: "SUIUSD",
    last_bar_ts_ms: 1_790_000_000_000,
    candle_age_s: 3660,
    candle_stale: false,
    ...overrides,
  };
}

function fleetView(deployments: FleetArmedDeployment[]): FleetArmedView {
  return {
    schema_version: "1",
    armed: deployments.length > 0,
    armed_count: deployments.length,
    deployments,
  };
}

function freshnessView(feeds: FleetFeedFreshness[]): FleetFreshnessView {
  return { schema_version: "1", feeds };
}

function makeClient(
  deployments: FleetArmedDeployment[],
  listFleetFreshness: FleetFreshnessClient["listFleetFreshness"],
): FleetArmedClient & FleetFreshnessClient {
  return {
    listFleet: vi.fn().mockResolvedValue(fleetView(deployments)),
    listFleetFreshness,
  };
}

function rowFor(venue: string, pair: string): HTMLElement {
  const row = screen
    .getAllByTestId("fleet-armed-row")
    .find(
      (candidate) =>
        candidate.getAttribute("data-venue") === venue &&
        candidate.getAttribute("data-pair") === pair,
    );
  if (row === undefined) {
    throw new Error(`no fleet row for ${venue} ${pair}`);
  }
  return row;
}

async function candleAgeText(venue: string, pair: string, expected: string): Promise<HTMLElement> {
  await waitFor(() => {
    expect(
      within(rowFor(venue, pair)).getByTestId("fleet-armed-candle-age").textContent,
    ).toBe(expected);
  });
  return rowFor(venue, pair);
}

describe("FleetArmedList candle freshness", () => {
  it("joins each armed row with its own feed by venue, pack and pair", async () => {
    const client = makeClient(
      [deployment(), COINBASE],
      vi.fn().mockResolvedValue(
        freshnessView([
          feed({
            venue: "coinbase",
            pack_id: "mean-revert",
            pair: "BTC-USD",
            candle_age_s: 10_800,
            candle_stale: true,
          }),
          // Same market, another pack: never joined onto trend-follow's row.
          feed({ pack_id: "old-pack", candle_age_s: 99_999, candle_stale: true }),
          feed(),
        ]),
      ),
    );
    render(<FleetArmedList client={client} />);

    const kraken = await candleAgeText("kraken", "SUIUSD", "1h 1m old at last check");
    expect(within(kraken).getByTestId("fleet-armed-candle-state").textContent).toBe(
      "Fresh",
    );

    const coinbase = await candleAgeText("coinbase", "BTC-USD", "3h old at last check");
    expect(within(coinbase).getByTestId("fleet-armed-candle-state").textContent).toBe(
      "Stale",
    );
    expect(screen.getByRole("columnheader", { name: "Candle age" })).toBeTruthy();
  });

  it("shows unavailable when the feed has no candle evidence", async () => {
    const client = makeClient(
      [deployment(), COINBASE],
      vi.fn().mockResolvedValue(
        freshnessView([
          feed({ last_bar_ts_ms: null, candle_age_s: null, candle_stale: null }),
          // coinbase has no freshness row at all.
        ]),
      ),
    );
    render(<FleetArmedList client={client} />);

    for (const [venue, pair] of [
      ["kraken", "SUIUSD"],
      ["coinbase", "BTC-USD"],
    ]) {
      const row = await candleAgeText(venue, pair, "unavailable");
      expect(within(row).queryByTestId("fleet-armed-candle-state")).toBeNull();
    }
  });

  it("shows the age but no verdict when freshness cannot be judged", async () => {
    const client = makeClient(
      [deployment()],
      vi.fn().mockResolvedValue(
        freshnessView([feed({ candle_age_s: 18_000, candle_stale: null })]),
      ),
    );
    render(<FleetArmedList client={client} />);

    const row = await candleAgeText("kraken", "SUIUSD", "5h old at last check");
    expect(within(row).getByTestId("fleet-armed-candle-state").textContent).toBe(
      "Freshness unavailable",
    );
  });

  it("keeps the fleet and says unavailable when freshness cannot be read", async () => {
    const client = makeClient(
      [deployment()],
      vi.fn().mockRejectedValue(new Error("paper fleet freshness failed: 500")),
    );
    render(<FleetArmedList client={client} />);

    const row = await candleAgeText("kraken", "SUIUSD", "unavailable");
    expect(within(row).getByTestId("fleet-armed-last-result").textContent).toBe(
      "1 tick, no refusal",
    );
    expect(within(row).queryByTestId("fleet-armed-candle-state")).toBeNull();
  });

  it("says unavailable when the client cannot read freshness", async () => {
    const client: FleetArmedClient = {
      listFleet: vi.fn().mockResolvedValue(fleetView([deployment()])),
    };
    render(<FleetArmedList client={client} />);

    await candleAgeText("kraken", "SUIUSD", "unavailable");
  });

  it("re-reads freshness on Refresh", async () => {
    const listFleetFreshness = vi
      .fn()
      .mockResolvedValueOnce(freshnessView([feed()]))
      .mockResolvedValueOnce(
        freshnessView([feed({ candle_age_s: 2 * 86_400 + 3_600, candle_stale: true })]),
      );
    const client = makeClient([deployment()], listFleetFreshness);
    render(<FleetArmedList client={client} />);
    await candleAgeText("kraken", "SUIUSD", "1h 1m old at last check");

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    const row = await candleAgeText("kraken", "SUIUSD", "2d 1h old at last check");
    expect(within(row).getByTestId("fleet-armed-candle-state").textContent).toBe(
      "Stale",
    );
    expect(listFleetFreshness).toHaveBeenCalledTimes(2);
  });
});

describe("feed_stale alert label", () => {
  it("names the stale-feed alert", () => {
    expect(summarizeAlertKind("feed_stale")).toEqual({
      label: "Feed stale",
      kind: "feed_stale",
      isKnown: true,
      isUnknown: false,
    });
  });
});
