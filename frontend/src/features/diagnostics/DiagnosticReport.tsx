import { useState, type JSX } from "react";
import "./DiagnosticReport.css";

/** Preview a local-only report before the user saves or shares it. */
export function DiagnosticReport(): JSX.Element {
  const [report, setReport] = useState<Record<string, unknown> | null>(null);
  const [steps, setSteps] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function prepare(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/v1/diagnostics/report", {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) throw new Error("unavailable");
      setReport(await response.json() as Record<string, unknown>);
    } catch {
      setReport(null);
      setError("Could not prepare the report. Check your local session and try again.");
    } finally {
      setBusy(false);
    }
  }

  function download(): void {
    if (report === null) return;
    const contents = JSON.stringify({ ...report, reproduction_steps: steps }, null, 2) + "\n";
    const url = URL.createObjectURL(new Blob([contents], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "krellbot-diagnostic-report.json";
    link.click();
    URL.revokeObjectURL(url);
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
    </section>
  );
}
