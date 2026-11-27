# Telemetry

Telemetry is **opt-in** (default off) until the operator opts in. Paid
catalogue surfaces point here from [paid-disclosures.md](paid-disclosures.md).
`krellbot telemetry enable` prints one example payload and waits for `y`.
Anything other than `y` leaves it off.

The workstation Packs checkout UI also exposes an explicit opt-in control
that writes the same `$KRELLBOT_HOME/telemetry.json` flag via
`PUT /api/v1/telemetry`. Until that flag is true, nothing leaves the
machine.

```
krellbot telemetry enable
krellbot telemetry disable
```

`enable` and `disable` write the flag and print the new state. Neither
command sends a payload by itself. The Packs consent toggle behaves the
same way: consent is recorded; no event is posted by the toggle alone.

## What the fill payload carries

A fill telemetry record carries only these keys:

| Key | Meaning |
| --- | --- |
| `install_id` | A random per-install UUID. Not a key, not a license. |
| `pack_id` | The slug of the armed pack. |
| `pack_version` | The version recorded at arm time. |
| `venue` | The exchange the pack is armed on. |
| `pair` | The market the pack trades. |
| `side` | `buy` or `sell`. |
| `bar_ts` | The candle close time the engine acted on. |
| `modeled_px` | The price the pack's signal produced. |
| `fill_px` | The fill price the simulator or venue reported. |
| `qty_bucket` | The floor of log2 of the USD notional. Not the raw quantity. |
| `fee_bps` | The venue's fee in basis points. |

## Checkout funnel events (opt-in)

When telemetry is enabled, the workstation may also post closed funnel
events for the paid catalogue path:

| `event` | When |
| --- | --- |
| `view` | Signed catalogue successfully loaded |
| `checkout_start` | Checkout started for a catalogue SKU |
| `checkout_success` | Checkout paid / entitlement granted |
| `checkout_fail` | Checkout refused, failed, or unreachable |
| `install` | Catalogue pack installed into the local library |
| `arm` | Pack armed (paper or live) |

A funnel record carries only:

| Key | Meaning |
| --- | --- |
| `install_id` | Same per-install UUID as fill telemetry |
| `kind` | Always `funnel` |
| `event` | One of the events above |
| `sku` | Catalogue SKU / pack id stem (never a path or source blob) |

## What payloads do NOT carry

Neither fill nor funnel records send:

- a balance,
- an API key or secret,
- a license value,
- a session id or claim-cache token,
- an IP address,
- the raw quantity,
- strategy source or pack bytes,
- anything from a Krell conversation, or an AI provider key.

The payload travels over HTTPS to `https://krellbot.dev/api/telemetry`.
The operator can inspect the recorded shape any time with
`krellbot telemetry show`.

## AI providers

AI traffic is not telemetry and does not depend on the telemetry flag. It
starts only when you connect a model in Settings, and it goes from your
computer straight to the provider you chose.

What is sent to the provider:

- your conversation with Krell: your messages, her replies, and her
  instructions;
- the Krellbot data Krell reads through her tools while answering you (for
  example a strategy draft, a backtest result or your paper status);
- when you connect or re-check a model, a request for the provider's model
  list and one short test message that offers a `ping` tool.

Exchange keys, your license and session tokens are never sent. Nothing about
your AI use is sent to **krellbot.dev**: no conversation, no key, no model
name, and no count.

Each connection carries this sentence, which Settings shows and Krell repeats
at the start of a conversation:

> Your conversation with Krell and the Krellbot data she reads are sent to
> {provider} ({host}). Nothing is sent to krellbot.dev.

Keys live in the OS keychain under service `krellbot-ai:<connection id>`,
username `credential`, apart from exchange keys.
`$KRELLBOT_HOME/ai/connections.json` (mode 0600) records the provider, label,
endpoint, model, sign-in kind, probe result and the active connection, never
a key. The loopback API never returns a key, and removing a connection deletes
its keychain entry.

Endpoints must use `https://`. Plain `http://` is accepted only for a model on
this computer (`127.0.0.1`, `::1` or `localhost`, for example Ollama or LM
Studio), and requests to this computer never go through a proxy. An endpoint
with a user name, password, query, parameters or fragment is refused, and a
provider redirect is never followed, so the key is not re-sent to another
host.
