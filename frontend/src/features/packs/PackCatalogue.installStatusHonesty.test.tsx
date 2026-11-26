/**
 * A catalogue install says "Installed" only when a pack file is on this
 * computer. A pending marker is not an install, and nothing downloads it
 * later, so the row says that plainly instead of implying a download.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PackCatalogue, installFeedback } from "./PackCatalogue";
import { PackLibrary, type PackLibraryClient } from "./PackLibrary";
import type { CatalogueDocument, PackInstallResult, PacksClient } from "./client";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const CATALOGUE: CatalogueDocument = {
  schema_version: "1",
  catalogue_version: "1",
  updated: "2026-11-25T00:00:00Z",
  packs: [
    {
      id: "free-dual-ma",
      tier: "free",
      label: "Free Dual MA",
      description: "Educational free pack",
      version: "1.0.0",
      permissions: [],
    },
  ],
};

function catalogueWith(result: PackInstallResult): PacksClient {
  return {
    listCatalogue: vi.fn(async () => CATALOGUE),
    install: vi.fn(async () => result),
  } as unknown as PacksClient;
}

async function installStatus(result: PackInstallResult): Promise<string> {
  render(<PackCatalogue client={catalogueWith(result)} />);
  await screen.findByTestId("pack-catalogue-list");
  fireEvent.click(screen.getByTestId("pack-catalogue-install-free-dual-ma"));
  let text = "";
  await waitFor(() => {
    text = screen.getByTestId("pack-catalogue-install-status-free-dual-ma").textContent ?? "";
    expect(text).not.toBe("");
  });
  return text;
}

describe("catalogue install status", () => {
  it("does not call a pending marker an install", async () => {
    const text = await installStatus({
      ok: true,
      pack: { bucket: "download_pending", pack_id: "free-dual-ma", version: "1.0.0", permissions: [], rollback_ref: null },
    });

    expect(text).not.toMatch(/Installed/);
    expect(text).toMatch(/Not installed · 1\.0\.0/);
    expect(text).toMatch(/can't download catalogue packs/);
    expect(text).not.toMatch(/pending|downloading/i);
  });

  it("does not claim an install when the response carries no pack row", async () => {
    const text = await installStatus({ ok: true });

    expect(text).toMatch(/^Not installed · 1\.0\.0/);
  });

  it.each(["installed", "configured", "deployed"])(
    "reports a pack that is on this computer (%s) as installed",
    (bucket) => {
      expect(installFeedback(bucket, "1.0.0")).toBe(`Installed · ${bucket} · 1.0.0`);
    },
  );
});

describe("pack library bucket label", () => {
  it("labels a pending row as not downloaded, not as a download in progress", async () => {
    const client = {
      listInstalled: vi.fn(async () => [
        { bucket: "download_pending", pack_id: "free-dual-ma", version: "1.0.0", permissions: [], rollback_ref: null },
      ]),
    } as unknown as PackLibraryClient;

    render(<PackLibrary client={client} />);

    const row = await screen.findByTestId("pack-library-row-free-dual-ma");
    expect(row.getAttribute("aria-label")).toMatch(/bucket Not downloaded/);
    expect(row.getAttribute("aria-label")).not.toMatch(/Download pending/);
  });
});
