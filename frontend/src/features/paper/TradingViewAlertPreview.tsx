import { useId, useState, type FormEvent, type JSX } from "react";
import { getCsrf } from "../../session";

type Result =
  | { kind: "success"; ticker: string; signal: "entry" | "exit"; occurredAt: string }
  | { kind: "error"; message: string };

export function TradingViewAlertPreview(): JSX.Element {
  const inputId = useId();
  const [alertJson, setAlertJson] = useState("");
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<Result | null>(null);

  async function preview(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (pending || !alertJson.trim()) return;
    setPending(true);
    setResult(null);
    try {
      const response = await fetch("/api/v1/paper/tradingview/preview", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", "X-Krellbot-CSRF": getCsrf() },
        body: alertJson,
      });
      const body = await response.json();
      const alert = body?.alert;
      if (
        response.ok && body?.ok === true && alert?.mode === "paper"
        && typeof alert.ticker === "string" && typeof alert.occurred_at === "string"
        && (alert.signal === "entry" || alert.signal === "exit")
      ) {
        setResult({ kind: "success", ticker: alert.ticker, signal: alert.signal, occurredAt: alert.occurred_at });
      } else {
        const message = body?.code === "alert_too_large"
          ? "The alert must be 4 KiB or smaller."
          : body?.code === "invalid_alert" && typeof body.message === "string"
            ? body.message
            : "Alert preview was refused.";
        setResult({ kind: "error", message });
      }
    } catch {
      setResult({ kind: "error", message: "Could not preview the alert in this workstation session." });
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={preview} className="kbot-paper-status__field">
      <label htmlFor={inputId} className="kbot-paper-status__field-label">TradingView alert JSON</label>
      <textarea
        id={inputId}
        className="kbot-paper-status__input"
        value={alertJson}
        rows={5}
        disabled={pending}
        onChange={(event) => {
          setAlertJson(event.target.value);
          setResult(null);
        }}
      />
      <button
        type="submit"
        className="kbot-paper-status__action"
        disabled={pending || !alertJson.trim()}
      >
        {pending ? "Previewing…" : "Preview alert"}
      </button>
      {result?.kind === "success" && (
        <p role="status">Paper preview · {result.ticker} · {result.signal} · {result.occurredAt}. No order sent.</p>
      )}
      {result?.kind === "error" && <p role="alert">{result.message}</p>}
    </form>
  );
}
