# Product study: Agriculture AI for smallholder farmers

This is the reasoning behind the platform's design: who it serves, who pays, and
why each feature works the way it does. It is meant for founders, partners and
contributors. Figures that need local validation are marked as assumptions.

## 1. The problem

Smallholder farmers (roughly 0.5–2 ha, mostly rain-fed) lose a large share of
their potential yield and income to four things:

| Pain | What it looks like on the farm | Feature |
|---|---|---|
| Advice gap | Extension officers are few, each covering many farmers; advice comes late or not at all. | Crop advice (SMS/USSD/app), stage-based tips |
| Pests and diseases | Fall armyworm, late blight, cassava viruses and others are spotted late and misidentified, so farmers spray the wrong product or nothing. | Photo diagnosis, symptom matcher, outbreak alerts |
| Weather risk | Planting, fertilising, spraying and harvesting decisions are made without a usable forecast. | Crop-stage-aware weather alerts |
| Weak market position | Farmers sell small lots at the farm gate to whoever shows up, with little price information. | Price alerts, buyer matching with aggregation |

## 2. Users and constraints

**Farmers.** Many use basic feature phones (SMS and USSD only), some use
low-end Android phones with expensive, patchy data. Literacy and language vary,
and English is often a second or third language. Trust goes to people and
institutions they already know: their co-op, their mobile operator, an NGO
field officer.

Design consequences:

- **Every feature works without a smartphone.** SMS keywords, a USSD menu,
  and free-text questions ("my maize leaves have holes") all work. Photos come
  through WhatsApp or the web app.
- **Short, actionable, local-language messages** (English, Kiswahili, French
  out of the box). Each 160-character SMS segment costs the partner money, so
  the platform counts segments.
- **Data-light app:** photos are shrunk on the phone before upload, the home
  screen loads in one request, and the app shell works offline.
- **Honesty about uncertainty.** A confident wrong diagnosis costs a harvest.
  Low-confidence and notifiable cases go to a human extension officer.

**Extension officers** (employed by co-ops, NGOs or government) get a review
queue in place of a phone full of blurry WhatsApp photos. Their confirmations
also feed outbreak detection.

**Partners (the paying customers)**: see below.

**Buyers** (millers, processors, exporters, traders, school-feeding and
relief programmes) want reliable volume at a known quality, which smallholders
individually cannot offer.

## 3. Go-to-market: B2B2C through partners

Selling directly to farmers is slow and costly: customer acquisition, trust
and payment collection are all hard. The tools reach farmers through
organisations that already have the relationship and a reason to pay:

| Partner | Why they pay | What they use most |
|---|---|---|
| **Mobile operators** | Value-added service revenue, lower churn and more airtime/data usage among rural subscribers; a reason to promote mobile money. | White-label brand, USSD code and shortcode, high-volume alerts, usage analytics. |
| **Co-operatives and farmer organisations** | Higher member yields, more produce aggregated through the co-op (which earns a margin), better input credit repayment. | Member import, officer review queue, broadcasts, buyer matching (co-op as aggregator). |
| **NGOs and development programmes** | Programme outcomes and donor reporting: reach, engagement and gender-disaggregated impact at low cost per farmer. | Analytics (reach by gender and crop, engagement, deals), broadcasts, outbreak monitoring. |
| **Agribusiness and input companies** | Traceable sourcing, contract-farming support, demand signals for inputs. | Buyer API, supply overview, completed-deal data. |
| **Buyers** | Volume from many small farms without many middlemen. | Buy requests, matching, deal confirmation. |

Platform features that make the B2B2C model work:

- **Multi-tenancy:** each partner is an isolated organisation with its own
  farmers, API keys (admin, officer and viewer roles), brand name, SMS
  shortcode and USSD code.
- **White-labelling:** messages carry the partner's brand ("RiftFarm:
  …"), not ours.
- **Per-message audit and billing data:** every SMS is logged with its segment
  count. AI calls are capped per farmer per day so partner costs stay
  predictable.
- **Impact analytics:** active farmers, gender split, advice helpfulness,
  diagnoses, alerts and marketplace value. These are the numbers partners
  report to boards and funders.
- **Integration API:** partners plug in their CRMs, member registers, market
  information systems and payment rails.

### Business model options (to validate with partners)

1. **Per active farmer per month** (platform licence), tiered by volume. This
   is simplest for telcos and NGOs.
2. **Usage pass-through plus margin** on SMS and AI calls. This suits co-ops
   with irregular usage.
3. **Marketplace take rate** on completed deals, paid by the buyer. Deals
   are recorded with price and volume, so this is measurable.
4. **Programme contracts** with NGOs and governments for a region or season,
   including officer tooling and reporting.

> Assumption to test: the cost of AI per active farmer per month is small next
> to SMS costs, given caching, daily caps and a rule-based first line for SMS
> and USSD.

## 4. Feature design notes

### Crop advice
- Grounded on a curated knowledge base (crops, growth stages, fertiliser,
  spacing, pests). That knowledge base is sent to the model as a cached system
  prompt, so answers stay consistent and cheap.
- Personal context: the crop, its growth stage computed from the planting
  date, the local forecast, nearby prices, recent diagnoses and outbreaks
  nearby.
- A **rule-based fallback** answers from the knowledge base when there is no
  network to the model, no API key, a refusal, or a farmer over the daily cap.
- **Proactive stage tips:** each plot gets one message when it enters a new
  growth stage (for example "top-dress now", "most water-sensitive stage").

### Pest and disease detection
1. **Quality gate on the server** (blur, darkness, size). Asking for a retake
   is cheaper and safer than a guess.
2. **Model diagnosis restricted to knowledge-base ids**, returned as structured
   output (`pest_id`, confidence, severity, evidence, actions). Unknown ids
   are coerced to `other`.
3. **Routing:** confident results go straight to the farmer. Low confidence,
   `other`, notifiable diseases (MLN, cassava brown streak, BXW, Panama
   disease) and model-flagged cases go to the officer queue. The farmer still
   gets safe interim advice and a reference number.
4. **IPM first:** cultural, then biological, then chemical controls. Chemicals
   are named by active ingredient with label, protection and pre-harvest
   interval reminders.
5. **Feature-phone path:** `PEST maize holes in leaves sawdust` and USSD
   option 4 use the offline symptom matcher.

### Outbreak early warning (the network effect)
Trusted reports (officer-confirmed, or confident photo diagnoses) are
clustered per pest. Three or more distinct farms within 25 km in 14 days
(all configurable) is an outbreak. Farmers who grow an affected crop nearby
are warned, **across all partners**. Only the pest, an approximate centre and
a count cross organisation boundaries. Every photo makes the service better
for the neighbours.

### Weather alerts
Forecasts come from Open-Meteo (free, no key), cached per ~11 km grid cell so
thousands of farmers share one call. Rules are crop- and stage-aware: heavy
rain (with harvest-now advice at maturity), dry spells during water-critical
stages, heat stress against each crop's own threshold, frost, strong wind,
spray windows (only when the farmer actually has something to spray) and
planting rains. Each farmer gets at most two weather alerts per run, and all
alerts are de-duplicated.

### Prices
Partners upload observations (enumerators, market information systems).
Completed marketplace deals become price points too. Farmers can query
`PRICE maize`, set threshold alerts (`ALERT maize above 45`), and get
automatic alerts when a nearby market moves 10% or more in a week.

### Farmer–buyer matching
- Buyers post crop, quantity, price, location, radius, minimum grade and
  minimum lot size.
- Eligibility: consent, partner marketplace switch, currency, grade, price
  floor, availability dates, distance and free quantity.
- Score: distance 35%, reliability 20% (completed vs. cancelled deals, with a
  neutral prior), lot size 20%, price headroom 15%, grade 10%.
- **Aggregation:** the order is filled from several farms, the way a co-op
  bulks produce. Declines and expired offers (48 h) trigger re-matching.
- **Privacy:** buyers see aggregated supply and anonymised candidates. A
  farmer's name and phone are revealed only after that farmer accepts.

## 5. Trust, safety and data protection

- Explicit marketplace consent. STOP and START opt-out for all non-transactional
  SMS.
- Right to erasure: deleting a farmer removes their profile, plots, photos,
  diagnoses, listings, alerts and login codes. Billing logs are kept but
  anonymised.
- Photos are re-encoded on upload, which strips EXIF metadata including GPS.
- Phone-number login with hashed one-time codes, rate limits and attempt
  limits. The login endpoint does not reveal whether a number is registered.
- Partner API keys are hashed at rest, role-scoped and revocable.
- Webhooks require a shared secret. Media downloads are restricted to
  provider hosts (an SSRF guard).
- Production mode refuses to start with default secrets.

## 6. Success metrics

- **Reach:** registered and monthly active farmers, by gender and crop.
- **Engagement:** questions asked, photos sent, alert opt-out rate (the main
  sign of alert fatigue), and the share of advice rated helpful.
- **Diagnosis quality:** officer agreement rate with AI results, and the share
  routed to review.
- **Outcomes:** deals completed, volume and value, and the price farmers got
  against the local market price. Later, yield surveys run by partners.
- **Unit economics:** SMS segments and AI calls per active farmer.

## 7. Roadmap

1. **Content:** translate knowledge-base stage tasks and pest actions into
   Kiswahili and French (today the templates are translated, but
   knowledge-base text is English unless the AI answers), then add Hausa,
   Amharic, Luganda and others, plus more crops (cowpea, sweet potato,
   cocoa, cotton). All content needs agronomist review per country.
2. **Voice/IVR** for farmers who cannot read SMS comfortably.
3. **Payments:** mobile-money escrow for deals and input credit.
4. **Officer mobile app** with an offline review queue and farm visits.
5. **Evaluation loop:** use officer confirmations as a labelled dataset to
   measure and improve diagnosis accuracy per crop and region.
6. **Satellite and soil data** (NDVI, soil moisture) for field-level advice and
   for insurance and credit partners.
7. **Scale:** background workers for model calls, a Postgres/PostGIS spatial
   index for matching and outbreaks, and Alembic migrations.
