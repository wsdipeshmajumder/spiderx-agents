# SpiderX AI · Eva

A voice-first builder for phone-AI agents.

The entire UI is one iridescent blob. You tap it, tell it about the agent you
want (sector, language, persona, guardrails, what callers usually need), and it
saves the agent and immediately hands the live call over to the new persona —
so you're testing exactly what you just designed, in the same breath.

There is no other UI. No forms, no dropdowns, no buttons. Eva handles every
configuration decision conversationally and chooses sensible defaults
silently. Connectors fire as real Gemini function tools during a test call.
Real inbound phone calls work through the Twilio Media Streams bridge.

```
          ┌─────────────────┐    PCM16 / 16 kHz mic    ┌──────────────┐
 Browser ←┤   single blob   ├─────WebSocket binary────►│   FastAPI    │
 (React)  │  voice surface  │←────PCM16 / 24 kHz spk ──┤   relay      │
          └─────────────────┘                          └──────┬───────┘
                                                              │ google-genai
                                                              ▼
                              ┌─────────────────────────────────────────┐
                              │  Gemini Live · native-audio dialog      │
                              │  · save_agent / select_agent (builder)  │
                              │  · 10 connector tools (test mode)       │
                              └─────────────────────────────────────────┘
                                                              ▲
                  ┌──────────────────────┐  µ-law 8k JSON     │
   Twilio Voice ──┤  /ws/twilio/{id}     ├────────────────────┘
   (real PSTN)    │  µ-law ↔ PCM resample │
                  └──────────────────────┘
```

## Interaction model

Everything is the blob:

| Gesture                       | Effect                                      |
|-------------------------------|---------------------------------------------|
| Tap (on landing)              | Start the live session (Eva greets you)   |
| Tap (during a call)           | Toggle microphone mute                      |
| Press-and-hold ≥ 0.55 s        | End the call                                |

Everything else is conversational:

- **Build a new agent** — just say what you want. Eva interviews you in 4-6 short turns and calls `save_agent` when it has enough.
- **Test a saved agent** — say "call Maya" / "let me try the dental one" — Eva calls `select_agent` with the right id and hands you over mid-conversation. The blob stays on screen; the voice changes.
- **Use connectors** — once you're talking to a saved agent, the configured connectors (calendar, CRM, KB, SMS, payments, etc.) are wired as live Gemini tools and fire mid-call.

The handoff between Eva and the new persona happens inside one WebSocket;
the browser only sees the blob momentarily shimmer (`transferring` state) and
then it's a different voice in your ear.

## Run

```bash
cd /Users/dipeshmajumder/phone_ai
cp .env.example .env
# paste your Gemini key (ipi-gemini-api-key) into .env
./run.sh
# open http://127.0.0.1:8765 in Chrome / Safari, allow microphone
```

No Node build step. The single-page React app loads via `esm.sh` import maps;
the AudioWorklet runs natively.

## Inbound phone calls (Twilio)

1. Set up an ngrok tunnel:

   ```bash
   ngrok http 8765
   ```

   Copy the `*.ngrok-free.app` host.

2. Add to `.env`:

   ```
   PUBLIC_HOST=<your-host>.ngrok-free.app
   ```

3. Restart the server, then point a Twilio Voice number's webhook (set
   *A call comes in* → Webhook, **HTTP POST**) at:

   ```
   https://<your-host>.ngrok-free.app/api/sip/twilio/twiml/<agent_id>
   ```

   where `<agent_id>` is one of your saved agents.

4. Call the Twilio number. Twilio's Media Streams will WebSocket into
   `/ws/twilio/<agent_id>`, where µ-law 8 kHz is transcoded to/from
   Gemini's PCM 16 kHz / 24 kHz in real time. Connectors fire the same
   way they do in the browser test.

Want a different provider (Telnyx, Plivo, Exotel, etc.)? They all support
WebSocket media streams in a near-identical shape — the bridge in
`backend/twilio_bridge.py` is small (≈ 130 lines) and is easy to fork.

## Connectors

Each connector is a Gemini `FunctionDeclaration` plus a Python handler. The
handlers in `backend/connectors.py` return plausible stubs so the end-to-end
flow works out of the box; replace the function bodies to point at your real
calendar / CRM / KB / SMS / payments.

| id                    | What it does                                    |
|-----------------------|-------------------------------------------------|
| `calendar_check`      | List available slots on a date                  |
| `calendar_book`       | Confirm a slot                                  |
| `crm_lookup`          | Find a customer by phone / email                |
| `crm_create_lead`     | Create a new lead                               |
| `order_status`        | Look up an order                                |
| `knowledge_base_search` | RAG-style FAQ search                          |
| `sms_send`            | Send an SMS                                      |
| `email_send`          | Send an email                                    |
| `payment_link`        | Hand the caller a hosted payment link            |
| `http_webhook`        | POST anything to a configured URL                |

Which connectors a given agent gets is decided by Eva during the build
conversation — it picks 1-3 obviously-useful ones for the sector.

## Files

```
backend/
  app.py             FastAPI routes (REST + ws/session + twilio + static)
  gemini_bridge.py   Unified session loop with builder→test handoff
  twilio_bridge.py   µ-law ↔ PCM bridge for Twilio Media Streams
  connectors.py      10 Gemini function declarations + stub handlers
  db.py              SQLite (one table: agents)
  presets.py         sectors / locales / voices / guardrails / sip / connectors
frontend/
  index.html
  app.js             React shell (one component, ~140 lines)
  voice-blob.js      Iridescent blob: SVG layers + sparkle canvas + audio reactivity
  audio-engine.js    Mic capture (16 kHz) + speaker playback (24 kHz) + meters
  recorder-worklet.js
  styles.css
data/eva.db        auto-created
.env.example
run.sh
```

## Configuration knobs

| Env var               | Default                                        |
|-----------------------|------------------------------------------------|
| `GEMINI_API_KEY`      | required                                       |
| `GEMINI_LIVE_MODEL`   | `gemini-2.5-flash-native-audio-latest`         |
| `PUBLIC_HOST`         | empty (Twilio routing disabled until set)      |
| `WEBHOOK_URL`         | empty (used by the `http_webhook` connector)   |
| `LOG_LEVEL`           | `INFO`                                         |

The bridge auto-falls back through known Live model names if the configured
one is unavailable to your key. To pin a specific model, set
`GEMINI_LIVE_MODEL` in `.env`.

## Browser notes

- Chrome / Edge / Safari 17+ on macOS work.
- The page must run on `localhost` or HTTPS for `getUserMedia` to grant the microphone.
- For a fresh start (clear all saved agents), delete `data/eva.db`.
