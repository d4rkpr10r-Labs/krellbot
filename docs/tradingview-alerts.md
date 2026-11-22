# TradingView alerts for paper trading

Krellbot can validate and preview an alert supplied through an authenticated
local workstation session. In Run, open the TradingView alert panel and paste
the alert JSON. This preview leaves runs, orders, and local
trading state untouched. It is the first slice of the TradingView integration.

## Alert format

Set a different rule ID for each alert rule. This example uses a TradingView
chart's exchange, ticker, and trigger time.

```json
{
  "schema_version": "1",
  "rule_id": "btc-entry",
  "ticker": "{{exchange}}:{{ticker}}",
  "signal": "entry",
  "occurred_at": "{{timenow}}"
}
```

Use `exit` for the corresponding exit rule. The local preview endpoint is
`POST /api/v1/paper/tradingview/preview`. It accepts up to 4 KiB and uses
the workstation's session, CSRF, Origin, and Host gates. It returns the
normalized paper preview and a deterministic fingerprint. Ticker spelling
is preserved for explicit market mapping. Do not put credentials, order
quantities, or addresses in alert messages.

TradingView sends webhooks to an external HTTP endpoint. A hosted TradingView
alert cannot reach the loopback preview endpoint directly. The workstation's
local network boundary stays in place.

## Remaining implementation

1. Add an explicit mapping from each alert
   rule and TradingView ticker to a validated, paper-armed revision and
   canonical market.
2. Add a separate authenticated HTTPS relay that lets the workstation pull
   deliveries. Preserve the loopback boundary. Limit delivery age, data
   retention, payload size, and retries. Document how to rotate the relay
   credential and verify source authentication.
3. Carry accepted signals into the deterministic paper tick and journal path.
   Reuse paper risk controls, kill/restore fences, and stale-metadata refusal.
   Prove duplicate and out-of-order delivery handling across restarts with
   fixtures. Keep live orders outside this integration.

See [TradingView's webhook alert documentation](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/).
