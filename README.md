# Agriculture AI

Crop advice, pest detection from a phone photo, weather and price alerts, and
farmer–buyer matching for smallholder farmers. The platform is built to be
**distributed through telcos, co-operatives and NGOs**: each partner gets its
own branded, isolated workspace, and farmers reach the service on any phone
through SMS, USSD, WhatsApp or a light web app.

See [`docs/PRODUCT.md`](docs/PRODUCT.md) for the product study: users,
go-to-market, business model, design rationale and roadmap.

## What's inside

| Capability | How it works |
|---|---|
| **Crop advice** | Questions by SMS, USSD, WhatsApp or app, answered by Claude using the farmer's crop, growth stage, forecast, nearby prices and the curated knowledge base. A rule-based fallback works offline. Proactive tips are sent when a plot enters a new growth stage. |
| **Pest and disease detection** | Photo → quality gate (blur, dark, small) → Claude vision with structured output restricted to knowledge-base ids → confidence routing. Uncertain or notifiable cases go to an extension officer's review queue. A symptom matcher covers feature phones. |
| **Outbreak early warning** | Confirmed reports are clustered per pest. Farmers nearby who grow an affected crop are warned, across all partners. |
| **Weather alerts** | Open-Meteo forecasts, cached per grid cell, turned into crop- and stage-aware alerts: heavy rain, dry spell, heat, frost, wind, spray window and planting rains. |
| **Price alerts** | Partner-uploaded market prices and completed-deal prices. `PRICE <crop>`, threshold alerts, and automatic alerts on big weekly moves. |
| **Farmer–buyer matching** | Buyers post demand. The matcher ranks eligible lots by distance, reliability, lot size, price and grade, and **pools several small farms** to fill an order. Farmers accept with `YES CODE`, and contact details are shared only after acceptance. |
| **Partner console** | Farmer onboarding (CSV import), officer review queue, segmented broadcasts with cost estimates, prices, outbreaks, impact analytics (reach by gender and crop, engagement, deals), message log, API keys. |

Languages: English, Kiswahili and French for every farmer-facing template and
menu. The knowledge base covers 10 crops and 33 pests and diseases.

## Quick start

```bash
pip install -e ".[dev]"
python -m agri_ai.seed            # demo co-op, telco, buyer, farmers, prices; prints API keys
uvicorn agri_ai.main:app --reload
```

- Farmer app: <http://localhost:8000/app>. Log in with phone `0711000000`;
  in development mode the code is filled in for you.
- Partner console: <http://localhost:8000/dashboard>. Paste a key printed by
  the seed script.
- API docs: <http://localhost:8000/docs>

To turn on AI diagnosis and conversational advice, set `ANTHROPIC_API_KEY`.
Without it, everything still works using the rule-based knowledge-base
fallbacks.

```bash
pytest -q        # 81 tests, fully offline (fake model, fake weather, in-memory SMS outbox)
```

Docker (API + Postgres + alert scheduler): `cp .env.example .env && docker compose up`.

## Try the channels locally

```bash
H='X-Webhook-Secret: dev-webhook-secret'
# SMS (Africa's Talking field names; Twilio's From/To/Body also work)
curl -s localhost:8000/webhooks/sms -H "$H" -d from=+254711000000 -d to=22384 -d text="PRICE maize"
curl -s localhost:8000/webhooks/sms -H "$H" -d from=+254711000000 -d to=22384 -d text="SELL maize 5 bags 40"
curl -s localhost:8000/webhooks/sms -H "$H" -d from=+254711000000 -d to=22384 -d text="my maize leaves have holes and sawdust"
# USSD: the gateway sends the full input path each step
curl -s localhost:8000/webhooks/ussd -H "$H" -d sessionId=1 -d serviceCode='*384*1#' -d phoneNumber=+254711000000 -d text=""
curl -s localhost:8000/webhooks/ussd -H "$H" -d sessionId=1 -d serviceCode='*384*1#' -d phoneNumber=+254711000000 -d text="3*1*200*0"
```

With the default `console` gateway, outbound SMS appear in the server log and
under **Messages** in the console.

### SMS keywords

| English | Kiswahili | French | Action |
|---|---|---|---|
| `HELP` | `MSAADA` | `AIDE` | List commands |
| `WEATHER` | `HALI` | `METEO` | 3-day forecast for the farm |
| `PRICE maize` | `BEI mahindi` | `PRIX maïs` | Nearest market prices with trend |
| `SELL maize 500kg 40` / `5 bags` | `UZA …` | `VENDRE …` | List produce (price per kg optional) and match buyers |
| `YES ABC12` / `NO ABC12` | `NDIYO` / `HAPANA` | `OUI` / `NON` | Answer a buyer offer |
| `ALERT maize above 45` | `ARIFU …` | `ALERTE …` | Price threshold alert |
| `PEST maize <symptoms>` | `WADUDU …` | `RAVAGEUR …` | Symptom diagnosis without a photo |
| `ASK <question>` or any sentence | `ULIZA …` | `QUESTION …` | Crop advice |
| `JOIN <name> <crops>` | `JIUNGE …` | `INSCRIRE …` | Self-register |
| `LANG SW` | `LUGHA SW` | `LANGUE FR` | Change language |
| `STOP` / `START` | `ACHA` / `ANZA` | `STOP` | Opt out or back in |

USSD menu: 1 Weather · 2 Prices · 3 Sell · 4 Pest help · 5 Ask an expert
(answered by SMS) · 6 My offers · 7 Language.

## Architecture

```
agri_ai/
  main.py              FastAPI app, static UIs, public knowledge endpoints
  config.py            environment settings (+ production safety checks)
  models.py            SQLAlchemy models (multi-tenant: Organization → Farmer → Plot …)
  knowledge/           curated crops.json and pests.json + loader, crop resolver, symptom matcher
  services/
    llm.py             Claude client: cached KB system prompt, structured diagnosis, refusal fallback
    advisory.py        grounded advice, farmer context, rules fallback, stage tips, daily AI caps
    diagnosis.py       photo/text diagnosis, confidence routing, officer review
    image_quality.py   blur/dark/size checks, resize, EXIF stripping
    weather.py         Open-Meteo provider, grid-cell cache, crop-stage alert rules
    prices.py          quotes, trends, threshold and move alerts
    marketplace.py     eligibility, scoring, aggregation, offers, accept/decline, expiry
    outbreaks.py       clustering of trusted reports, cross-partner warnings
    notifications.py   SMS gateways (console, Africa's Talking, Twilio), segment counting, alert de-dup
  channels/            SMS/WhatsApp command interpreter, stateless USSD menu
  api/                 admin, partner, buyer, farmer (app) and webhook routers
  jobs.py              scheduled jobs: weather, prices, stages, outbreaks, marketplace expiry
  static/              farmer PWA, partner console, landing page
```

**AI design.** Requests go to `claude-opus-5-5` (configurable via
`AGRI_LLM_MODEL`) through the Anthropic SDK, with:
- one byte-stable system prompt that holds the whole knowledge base, marked
  for prompt caching;
- a JSON-schema structured output for diagnoses;
- explicit `effort` (default `medium`, set by `AGRI_LLM_EFFORT`);
- server-side refusal fallback (`fallbacks: "default"`).

Every call has a deterministic fallback, and farmers are capped at
`AGRI_AI_DAILY_LIMIT` model calls per day.

**Jobs.** Run `python -m agri_ai.jobs` from cron every few hours. Alerts are
de-duplicated, so re-runs and overlapping runs are safe. Partners can also
trigger a run for their own farmers from the console.

## API overview

| Audience | Auth | Prefix |
|---|---|---|
| Platform operator | `X-Admin-Token` | `/api/v1/admin` (create orgs, which returns the first API key; run jobs) |
| Partners (telco, co-op, NGO) | `X-API-Key` (roles: admin, officer, viewer) | `/api/v1/partner` (org, keys, farmers, CSV import, broadcast, diagnoses and review, prices, outbreaks, analytics, messages, jobs) |
| Buyers | `X-API-Key` | `/api/v1/buyer` (supply, requests, candidates, matches, complete, cancel) |
| Farmers (app) | `Authorization: Bearer` (SMS code login) | `/api/v1/farmer` (home, advice, diagnose, weather, prices, price alerts, listings, offers, alerts) |
| Gateways | `X-Webhook-Secret` or `?secret=` | `/webhooks/sms`, `/webhooks/ussd`, `/webhooks/whatsapp` |
| Public | none | `/api/v1/crops`, `/api/v1/pests`, `/health` |

Onboard a partner:

```bash
curl -s localhost:8000/api/v1/admin/orgs -H 'X-Admin-Token: dev-admin-token' -H 'Content-Type: application/json' \
  -d '{"name":"Kilimo Co-op","kind":"cooperative","country":"KE","currency":"KES","sms_shortcode":"22384","ussd_code":"*384*1#"}'
```

## Going to production

- Set `AGRI_ENV=production` plus strong `AGRI_ADMIN_TOKEN`,
  `AGRI_SECRET_KEY` and `AGRI_WEBHOOK_SECRET`. The app refuses to start
  otherwise, and login codes are never exposed in production.
- Use Postgres (`AGRI_DATABASE_URL=postgresql+psycopg://…`, install
  `.[postgres]`). Tables are created at startup; adopt Alembic before the
  first schema change.
- SMS: `AGRI_SMS_GATEWAY=africastalking` (`AT_USERNAME`, `AT_API_KEY`,
  `AT_SENDER_ID`) or `twilio`. Point the gateway's inbound SMS, USSD and
  WhatsApp callbacks at `/webhooks/...` with the secret.
- Store uploads on a persistent volume (`AGRI_UPLOAD_DIR`).
- Schedule `python -m agri_ai.jobs`.
- Serve over HTTPS behind a reverse proxy.

## Known limitations

- Knowledge-base text (stage tasks, pest actions) is English. Templates and
  menus are translated, and AI answers come in the farmer's language, but
  rule-based tips in Kiswahili or French still embed English knowledge-base
  text. Agronomist-reviewed translations are the top roadmap item.
- The agronomy content is a general East and West Africa starting point.
  Validate it with local extension services before launch in each country.
- Model calls run inside the request (photo diagnosis in the app) or after
  the response (SMS, USSD, WhatsApp). At scale, move them to a task queue.
- Matching and outbreak clustering scan in Python. Use PostGIS when volumes
  grow.
