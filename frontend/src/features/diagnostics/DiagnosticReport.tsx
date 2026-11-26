import { useState, type JSX } from "react";
import "./DiagnosticReport.css";

type JsonObject = Record<string, unknown>;

const REPORT_PATH = "/api/v1/diagnostics/report";
const OPERATIONS_PATH = "/api/v1/diagnostics/operations";
const UNAVAILABLE = "unavailable";

async function readSection(path: string): Promise<JsonObject> {
  const response = await fetch(path, { credentials: "same-origin", cache: "no-store" });
  if (!response.ok) throw new Error(UNAVAILABLE);
  const body: unknown = await response.json();
  if (body === null || typeof body !== "object" || Array.isArray(body)) throw new Error(UNAVAILABLE);
  return body as JsonObject;
}

function saveJson(contents: unknown, filename: string): void {
  const text = JSON.stringify(contents, null, 2) + "\n";
  const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

/** A list part's row count, or 'unavailable' when the engine could not read it. */
function listCount(value: unknown): string {
  return Array.isArray(value) ? String(value.length) : UNAVAILABLE;
}

function unresolvedCount(value: unknown): string {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    const count = (value as JsonObject).count;
    if (typeof count === "number" && Number.isInteger(count) && count >= 0) return String(count);
  }
  return UNAVAILABLE;
}

/** Preview a local-only report before the user saves or shares it. */
export function DiagnosticReport(): JSX.Element {
  const [report, setReport] = useState<JsonObject | null>(null);
  const [operations, setOperations] = useState<JsonObject | typeof UNAVAILABLE | null>(null);
  const [steps, setSteps] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function prepare(): Promise<void> {
    setBusy(true);
    setError(null);
    const [reportResult, operationsResult] = await Promise.allSettled([
      readSection(REPORT_PATH),
      readSection(OPERATIONS_PATH),
    ]);
    if (reportResult.status === "fulfilled") {
      setReport(reportResult.value);
    } else {
      setReport(null);
      setError("Could not prepare the report. Check your local session and try again.");
    }
    setOperations(operationsResult.status === "fulfilled" ? operationsResult.value : UNAVAILABLE);
    setBusy(false);
  }

  function download(): void {
    if (report === null) return;
    saveJson({ ...report, reproduction_steps: steps }, "krellbot-diagnostic-report.json");
  }

  function downloadOperations(): void {
    if (operations === null || operations === UNAVAILABLE) return;
    saveJson(operations, "krellbot-diagnostic-operations.json");
  }

  return (
    <section className="kbot-diagnostic-report" aria-label="Diagnostic report" data-testid="diagnostic-report">
      <h3>Diagnostic report</h3>
      <p>Preview your build, operating system, scheduler status and recent paper checks recorded by this build before saving a report.</p>
      <button type="button" onClick={() => void prepare()} disabled={busy}>
        {busy ? "Preparing…" : "Prepare report"}
      </button>
      {error !== null && <p role="alert">{error}</p>}
      {report !== null && <>
        <label htmlFor="diagnostic-steps">What happened? Add steps to reproduce it</label>
        <textarea id="diagnostic-steps" value={steps} onChange={(event) => setSteps(event.target.value)} />
        <p>Review your notes and the report before sharing it.</p>
        <pre data-testid="diagnostic-preview">{JSON.stringify({ ...report, reproduction_steps: steps }, null, 2)}</pre>
        <button type="button" onClick={download}>Download report</button>
      </>}
      {operations !== null && (
        <section
          className="kbot-diagnostic-report__operations"
          aria-label="Operations section"
          data-testid="diagnostic-operations"
        >
          <h4>Operations section (optional)</h4>
          <p>
            Current alert kinds and codes, how many orders await reconcile and for how long, and the paper
            packs armed per venue and pair. It holds no keys, balances, quantities, file paths, notes or
            live events. It saves as a separate file and is not added to the report.
          </p>
          {operations === UNAVAILABLE ? (
            <p data-testid="diagnostic-operations-unavailable">Operations section: unavailable.</p>
          ) : (
            <>
              <dl className="kbot-diagnostic-report__summary" data-testid="diagnostic-operations-summary">
                <div>
                  <dt>Alerts</dt>
                  <dd data-testid="diagnostic-operations-alerts">{listCount(operations.alerts)}</dd>
                </div>
                <div>
                  <dt>Unresolved orders</dt>
                  <dd data-testid="diagnostic-operations-unresolved">{unresolvedCount(operations.unresolved_orders)}</dd>
                </div>
                <div>
                  <dt>Paper runs</dt>
                  <dd data-testid="diagnostic-operations-paper-runs">{listCount(operations.paper_runs)}</dd>
                </div>
              </dl>
              <pre data-testid="diagnostic-operations-preview">{JSON.stringify(operations, null, 2)}</pre>
              <button type="button" onClick={downloadOperations}>Download Operations section</button>
            </>
          )}
        </section>
      )}
    </section>
  );
}
