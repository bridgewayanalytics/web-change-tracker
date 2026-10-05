# Bubble (Eidarix) API Reference

**Purpose:** Complete technical reference for publishing alerts directly to the Eidarix Bubble app, without going through the workflow (`wf/`) endpoints. Building block for the content-gate redesign.

**Last verified:** 2026-10-05 against test space. All field names confirmed via Swagger + live POST/PATCH tests.

---

## 1. Base URLs and Auth

```
Test:       https://eidarix.bridgewayanalytics.com/version-test/api/1.1/
Live:       https://eidarix.bridgewayanalytics.com/api/1.1/
Auth:       Authorization: Bearer <BUBBLE_API_KEY>   (env: BUBBLE_API_KEY, see bubble/bridgemind.py)
Content-Type: application/json
```

All endpoints follow the pattern:
```
GET    /obj/{type}             — search/list (with constraints)
POST   /obj/{type}             — create new record, returns { status: "success", id: "<_id>" }
GET    /obj/{type}/{id}        — fetch single record, returns { response: { <fields> } }
PATCH  /obj/{type}/{id}        — partial update (only fields in body are changed), returns 204 No Content
PUT    /obj/{type}/{id}        — full replace
DELETE /obj/{type}/{id}        — delete, returns 204 No Content
```

**Important:** The test URL requires `version-test` in the path. The live URL has no version prefix. The `calendaritemtype` object **also requires the versioned URL** even in the live environment — use `https://eidarix.bridgewayanalytics.com/version-live/api/1.1/obj/calendaritemtype` in production.

---

## 2. Space IDs

All data-scoped queries must include a space constraint. Every record belongs to a space.

| Environment | Space ID |
|-------------|----------|
| **Test**    | `1768998437948x865417918648382000` |
| **Live**    | `1770642377799x775210694699370900` |

Standard space constraint (used on every search):
```json
[{"key": "space", "constraint_type": "equals", "value": "<space_id>"}]
```

---

## 3. Search / Constraint Format

The `constraints` query parameter is a URL-encoded JSON array:

```json
[
  {"key": "fieldName", "constraint_type": "equals", "value": "..."},
  {"key": "date",      "constraint_type": "greater than", "value": "2026-06-01T00:00:00.000Z"}
]
```

Common `constraint_type` values: `equals`, `not equal`, `contains`, `greater than`, `less than`, `is empty`, `is not empty`.

Pagination: `?limit=100&cursor=0` — max 100 per page. Response includes `remaining` count; increment cursor by `count` to get the next page.

**Bulk org loading:** 146 orgs in test space (more in live) — requires two pages (`limit=100&cursor=0` then `limit=100&cursor=100`). One page is not enough.

**Constraint key convention:** Both the display name (e.g., `"Name"`) and the underscore convention (e.g., `"name_text"`) are accepted as constraint search keys. Always prefer the display name for clarity.

---

## 4. Write Field Name Convention

**Critical:** Bubble Data API write operations (POST/PATCH body) use the **display field names exactly as they appear in GET responses and the Swagger spec** — including spaces, capitalization, hyphens, and special characters.

```
Write field name = display name as-is
```

**Confirmed by live test:**
- `"BA title"` ✓ (not `ba_title_text`)
- `"NAIC Title"` ✓ (not `naic_title_text`)
- `"BA Ref #"` ✓ (not `ba_ref___text`)
- `"Topics - dt"` ✓ (not `topics___dt_list_custom_newsreel_update`)
- `"space"` ✓ (lowercase, as returned by GET on agendaitem)
- `"Space"` ✓ (capitalized, as returned by GET on calendaritem/libraryitem)

**The underscore naming convention (`type_text`, `field___dt_list_custom_x`) is WRONG for POST/PATCH** — Bubble returns `{"statusCode":400,"body":{"status":"ERROR","message":"Unrecognized field: ba_title_text"}}`.

The old `bubble_sync.py` PATCH code uses this incorrect convention — that code path has never been successfully exercised against the Data API (the library-item UPDATE path is marked "TODO: migrate to workflow" and never triggered in production).

**Constraint search keys (GET)** accept both display names and the underscore convention. For consistency, use display names everywhere.

**Reference:** The Swagger Body definitions (e.g., `calendaritemBody`, `libraryitemBody`) list all writable fields with their exact names. These are the authoritative source for write field names.

---

## 5. Data Types

### 5.1 `calendaritem` — Calendar Event

**Endpoint:** `GET/POST /obj/calendaritem`, `PATCH /obj/calendaritem/{id}`

**Key fields:**

| Field (read/write — same name) | Type | Notes |
|---|---|---|
| `_id` | string | Bubble unique ID (read-only) |
| `title` | string | Event title |
| `date` | datetime | Start datetime, ISO 8601 UTC (e.g. `2026-06-11T18:00:00.000Z`) |
| `End time` | datetime | End datetime, ISO 8601 UTC |
| `full day` | boolean | Full-day event |
| `type` | string | → `calendaritemtype._id` |
| `Orgs ` *(trailing space)* | array[string] | → `organization._id` list |
| `Topics - dt` | array[string] | → `chronicletopic._id` list |
| `attached agenda items` | array[string] | → `agendaitem._id` list |
| `Agenda` | array[string] | → `libraryitem._id` list |
| `Timezone Code` | string | Always `"America/New_York"` for us |
| `phone_number_and_access_code` | string | Dial-in + access code |
| `location` | string | Meeting URL |
| `event description` | string | Description text |
| `Space` | string | → `space._id` (set on create) |
| `Eiderix Ref` | string | Our pipeline's `agent_call_id` — for dedup/tracing |

**Auto-managed by Bubble (do NOT send in write payload):**
- `body` — HTML block auto-generated by Bubble workflow
- `AMPM from/until`, `Hour from/until`, `Minutes from/until` — derived from datetime
- `name for search` — auto-lowercased from `title`
- `has topic` — auto-set when `Topics - dt` is populated
- `length` — option set derived from datetimes
- `datewasset` — internal flag
- `Outlook Event ID / UID / last sync` — Outlook sync managed separately

**Lookup for UPDATE (finding an existing record):**
- Primary: match by `space` + `date` range (one-day window: `date > 2026-06-11T00:00:00.000Z` AND `date < 2026-06-12T00:00:00.000Z`)
- Secondary: optionally add org constraint (`Orgs ` contains org `_id`) to disambiguate when multiple events share a date
- Once found: PATCH by `_id`

---

### 5.2 `libraryitem` — Library Item (documents, agendas, materials)

**Endpoint:** `GET/POST /obj/libraryitem`, `PATCH /obj/libraryitem/{id}`

**Key fields:**

| Field (read/write — same name) | Type | Notes |
|---|---|---|
| `_id` | string | Bubble unique ID (read-only) |
| `Name` | string | Document title |
| `URL` | string | Source URL |
| `file name` | string | Filename (e.g., `agenda-06-11.pdf`) |
| `Type - DT` | string | → `libraryitemtype._id` |
| `Organizations` | array[string] | → `organization._id` list |
| `Topics - dt` | array[string] | → `chronicletopic._id` list |
| `Agenda items` | array[string] | → `agendaitem._id` list |
| `date` | datetime | Document date (send as `YYYY-MM-DDT00:00:00.000Z`) |
| `Date display` | option set | `"Full date"`, `"Month, Year"`, `"Year"` |
| `summary` | string | AI-generated summary |
| `Status` | option set | Always `"Active"` for new records |
| `Space` | string | → `space._id` |
| `Eiderix Ref` | string | Our pipeline's `agent_call_id` — for dedup/tracing |

**Auto-managed by Bubble (do NOT send in write payload):**
- `name-for-search` — auto-lowercased from `Name`
- `full date display?` — computed from `Date display`
- `News in print` — set when linked to a newsreel issue
- `Available To Vector Store` — set by vector store workflow
- `file` — binary upload; we link by URL only
- `origin` — legacy field

**Lookup for UPDATE (finding an existing record):**
- Primary: match by `URL` equals `<url>`
- Fallback: match by `Name` equals `<title>`
- Once found: PATCH by `_id`

---

### 5.3 `agendaitem` — Agenda Item

**Endpoint:** `GET/POST /obj/agendaitem`, `PATCH /obj/agendaitem/{id}`

**Key fields:**

| Field (read/write — same name) | Type | Notes |
|---|---|---|
| `_id` | string | Bubble unique ID (read-only) |
| `BA title` | string | Bridgeway Analytics title (our label) |
| `NAIC Title` | string | Official NAIC agenda title |
| `BA Ref #` | string | Reference ID (e.g. `LATF#APF-2025-14`) |
| `topics - dt` | array[string] | → `chronicletopic._id` list |
| `space` | string | → `space._id` *(lowercase — as returned by GET)* |
| `Eiderix Ref` | string | Our pipeline's `agent_call_id` — for dedup/tracing |

**Auto-managed by Bubble:**
- `text field for search` — auto-lowercased from `BA title`
- `origin` — legacy

**Important notes:**
- Agenda items are **always CREATE** — they are never updated in place
- `BA title` is the field used for lookup by title (not `NAIC Title`)
- For "Existing" / "Updated" status items from the agent: search by `BA title` equals `<title>` to find the existing `_id`, then just link that ID — do not re-create
- `space` is lowercase on `agendaitem` (unlike `Space` on calendaritem/libraryitem)

**Lookup for "Existing"/"Updated" items:**
```
GET /obj/agendaitem?constraints=[space_constraint, {"key": "BA title", "constraint_type": "equals", "value": "<title>"}]
```

---

### 5.4 `chronicletopic` — Chronicle Topic

**Endpoint:** `GET /obj/chronicletopic` (read-only from pipeline perspective)

**Key fields:**

| Field | Notes |
|-------|-------|
| `_id` | Bubble unique ID |
| `Title` | Full topic title — use for exact-match lookup |
| `Empty topic` | boolean — if true, this is the fallback topic for events with no matching topics |
| `Space` | → `space._id` |

**Lookup pattern:**
```
GET /obj/chronicletopic?constraints=[space_constraint, {"key": "Title", "constraint_type": "equals", "value": "<topic_name>"}]
```

Or load all at once (64 in test space) and resolve in-memory:
```
GET /obj/chronicletopic?constraints=[space_constraint]&limit=100
```

---

### 5.5 `organization` — Organization

**Endpoint:** `GET /obj/organization` (read-only from pipeline perspective)

**Key fields:**

| Field | Notes |
|-------|-------|
| `_id` | Bubble unique ID |
| `Name` | Full org name — use for exact-match lookup |
| `Short Name` | Abbreviated name |
| `Level` | Hierarchy level (1=root, 2=top-level, 3-6=nested) |
| `Parent` | → parent `organization._id` |
| `Space` | → `space._id` |

**Lookup pattern:**
```
GET /obj/organization?constraints=[space_constraint, {"key": "Name", "constraint_type": "equals", "value": "<org_name>"}]
```

**Bulk load (146 orgs in test space — requires two pages):**
```
Page 1: GET /obj/organization?constraints=[space_constraint]&limit=100&cursor=0
Page 2: GET /obj/organization?constraints=[space_constraint]&limit=100&cursor=100
```
Combine both result sets to get all orgs. Live space has more orgs — always check `remaining` and paginate until it is 0.

---

### 5.6 `calendaritemtype` — Calendar Item Type

**Endpoint:** `GET /obj/calendaritemtype` — **requires versioned URL** even in production
- Test: `https://eidarix.bridgewayanalytics.com/version-test/api/1.1/obj/calendaritemtype`
- Live: `https://eidarix.bridgewayanalytics.com/version-live/api/1.1/obj/calendaritemtype`

**All types in test space (3 total):**

| `_id` | `display` | `Full day` | `meeting` | Notes |
|--------|-----------|------------|-----------|-------|
| `1774450888749x503072671341055170` | `"Meeting"` | false | true | Standard NAIC meetings |
| `1774450888749x696169840832005600` | `"Effective date"` | true | false | Adopted guidelines |
| `1774450888749x842219978507623700` | `"Last comment date"` | true | false | RFC comment deadlines |

**Write field on calendaritem:** `type` → value is the `_id` above

---

### 5.7 `libraryitemtype` — Library Item Type

**Endpoint:** `GET /obj/libraryitemtype` (standard non-versioned URL works)

**All types in test space (9 total):**

| `_id` | `Title` | Notes |
|--------|---------|-------|
| `1771254376180x176048524209204700` | `"Proposed Guidance & Support Materials"` | RFCs, exposure drafts |
| `1771254376181x155862874436283100` | `"Existing Requirements & Guidance"` | Adopted guidelines |
| `1771254376181x174725638731171740` | `"In the Weeds"` | Deep-dive analysis |
| `1771254376181x275810514918123360` | `"Agenda & Materials"` | Meeting agendas + materials |
| `1771254376181x934872927540484400` | `"Publication"` | Research papers, publications |
| `1771254376182x113762779592273550` | `"Web Repository"` | Web links, external references |
| `1771254376182x331725207824129540` | `"Newsreel"` | Newsreel-linked content |
| `1771254376182x426904129835546940` | `"Podcasts & Webinars"` | Audio/video content |
| `1771254376182x947670759074166400` | `"Other"` | Catch-all |

**Write field on libraryitem:** `Type - DT` → value is the `_id` above

**Agent alert_type → libraryitemtype mapping:**
| Alert Type | Library Item Type Title |
|------------|------------------------|
| New/Updated Agenda | `"Agenda & Materials"` |
| New/Updated Materials | `"Agenda & Materials"` |
| New/Updated Agenda & Materials | `"Agenda & Materials"` |
| New/Updated Request for Comment | `"Proposed Guidance & Support Materials"` |
| New/Updated Effective Date | `"Existing Requirements & Guidance"` |
| New or Updated Report or Other Resource | `"Publication"` |
| New Meeting Transcript Available | `"Publication"` *(use until "Meeting Transcript" type confirmed in live)* |
| Other | `"Other"` |

---

## 6. Chronicle Topics Reference (Test Space — 64 total)

These exist in the test space. Live space has the same set plus additional NAIC-specific ones.

```
1772364051422x161004932788948450  Collateralized Loan Obligations (CLOs) and Asset-Backed Securities (ABS)
1772364051453x251147923920128420  Fitch
1772364051467x741797369183902800  NAIC Climate Initiatives
1772364051480x569186010447587000  Funds Under Schedule BA
1772364051493x251447186261330100  New York State Bond Exchange Traded Fund (ETF) Treatment
1772364051509x980555326872963200  Funding Agreements
1772364051510x898865719134087200  Standard & Poor (S&P)
1772364051524x151156015684665400  RBC C-3 (Interest Rate and Market Risk)
1772364051538x583111062552146600  SSAP Rejection of Current Expected Credit Loss (CECL) Model
1772364051543x655451832919931900  Tax Credit Structures
1772364051560x412712507156954430  The Federal Reserve Board (FRB)
1772364051561x159916793649937950  International Standard on Sustainability Assurance (ISSA-5000)
1772364051562x953889931271602000  U.K. Bank of England (BoE)
1772364051563x149951718097507400  Bermuda Monetary Authority (BMA) Climate Disclosures
1772364051565x137475041772936020  Texas Bond Exchange Traded Fund (ETF) Treatment
1772364051565x424340262726740300  Iowa Refinement to 511.8 Investment of Funds
1772364051576x585152954849205600  Commercial Real Estate Equity
1772364051576x623480019666068900  Financial Stability Board (FSB)
1772364053949x539517646942305700  Credit for Reinsurance
1772364053973x826872816027479400  NAIC Designations and Use of Agency Ratings
1772364053974x739672587147741800  Private Equity Owned Insurers
1772364053976x730810587764283300  The NAIC Investment Oversight Framework
1772364053977x695400422339360600  Exchange Traded Funds (ETFs)
1772364053986x292171563190917000  Digital Assets
1772364053986x622836366346927600  Non-Performing Commercial and Farm Mortgages with Designations CM-6 and CM-7
1772364053999x566715266977397400  Actuarial Guideline (AG) LIII
1772364054003x434941976530390340  Principles-Based Bond Definition and Reporting
1772364054016x523863224851957440  U.K. Solvency
1772364054726x735890669445522000  Life RBC Covariance & Asset Concentration Risk
1772364054741x966014907731739500  Generator of Economic Scenarios (GOES)
1772364054752x237831236541406140  AM Best
1772364054766x153190903834617300  Residential Mortgages
1772364054767x145757227501820640  Negative Interest Maintenance Reserves (IMR)
1772364054776x971989806922977800  The U.S. Department of the Treasury, Federal Insurance Office (FIO)
1772364054790x650735389214077700  C-1, R-1 & H-1 Bond Factors
1772364054804x341052981647096640  The U.S. Department of the Treasury, Federal Insurance Office (FIO) and Federal Advisory Committee on Insurance (FACI)
1772364054805x713668206228373600  CMBS & RMBS
1772364054806x524015355783178800  Liquidity Stress Tests (LSTs)
1772364054823x658846903788593300  Joint Committee (JC) of the European Supervisory Authorities (ESAs)
1772364054836x913377176710762800  Calendar Events with no Topic  ← empty-topic fallback (Empty topic = true)
1772364054863x203599405208318750  Affiliate and Related Party Investments
1772364054884x149586687421134900  ART Heatmaps: State Investment Limits as a Percentage of Admitted Assets
1772364054885x939812784158560100  Group Capital Calculations
1772364054886x131815667908103500  The Bermuda Monetary Authority (BMA)
1772364054886x706461953908513300  Iowa Limits on Foreign Investments
1772364054887x581677079922170900  Mutual Funds
1772364054888x429778373901388100  Moody's Ratings
1772364054895x503803176067330240  Collateral Loans
1772364054895x567484369542222300  European Insurance and Occupational Pensions Authority (EIOPA) Climate Initiatives
1772364054896x328002245297093000  The Financial Stability Board (FSB)
1772364054897x162713167565373700  Principle-Based Reserving (PBR) & Fixed Income Annuities (VM-22)
1772364054898x612192738293640600  Repurchase Agreements & Securities Lending
1772364054899x524029755604167740  International Association of Insurance Supervisors (IAIS)
1772364054905x156984803317659070  New York Ownership and Related Party Investments
1772364054906x109817332705371310  Principal Protected Securities (PPS)
1772364054907x312513398086822660  Massachusetts Bond Exchange Traded Fund (ETF) Treatment
1772364054908x835495424455508600  European Insurance and Occupational Pensions Authority (EIOPA)
1772364054919x779471137855229300  U.S. Department of the Treasury, Financial Stability Oversight Council (FSOC)
1772364054920x146504506336973660  International Accounting Standards Board (IASB)
1772364054955x784337261922289300  New Jersey Limits on Foreign Investments
1772364054956x748366612540125300  The International Association of Insurance Supervisors (IAIS)
1772364054957x384095436874424700  ALM Derivatives & Derivative Investments
1772364054957x813312966107070800  Iowa Limits on High Yield Credit
1772364054958x513261579450202240  Short-Term Investments
```

**Empty-topic fallback:** `1772364054836x913377176710762800` ("Calendar Events with no Topic") — use when an event has no matching chronicle topics, to avoid sending an empty array.

---

## 7. Organization Reference (Test Space — 146 total, selected NAIC orgs)

Key NAIC orgs present in the test space. Always resolve by exact `Name` match at runtime — never hardcode IDs; live space IDs differ.

```
NAIC hierarchy (L2):
  1774256229360x184654588542338530  NAIC

Key committees (L3):
  1774256229768x368945586059747260  Financial Condition (E) Committee
  1774256229842x744368417831742000  Life Insurance & Annuities (A) Committee
  1774256229864x223258359100990900  Executive (EX) Committee

Key task forces / working groups (L4-L6):
  1774256233839x359108015188111170  Natural Catastrophe Risk and Resilience (EX) Task Force
  1774256233858x890399996683947800  Life Actuarial (A) Task Force
  1774256233919x628604004901991400  Capital Adequacy (E) Task Force
  1774256233949x932698034318264500  Accounting Practices & Procedures (E) Task Force
  1774256233981x633952341378619600  Valuation of Securities (E) Task Force
  1774256234026x292322615927574900  Group Solvency Issues (E) Working Group
  1774256234092x166173525953933630  Financial Stability (E) Task Force
  1774256234095x430453355663251700  Group Capital Calculation (E) Working Group
  1774256234137x137384071132558690  Reinsurance (E) Task Force
  1774256234154x644756831659611000  Structured Securities Group
  1774256234188x892315418749160800  Securities Valuation Office
  1774256234303x950438113896638500  Capital Markets Bureau
  1774256234502x796994882887763700  Federal Insurance Office
  1774256235532x564288593433260700  Risk-Based Capital (RBC) Model Governance (EX) Task Force
  1774256235627x812463858770065400  Invested Assets (E) Task Force
  1774256235779x226510970691874000  Receivership & Insolvency (E) Task Force
  1774256235849x781703658324464100  Blanks (E) Working Group
  1774256235897x392541319805933100  Valuation Manual (VM)-22 (A) Subgroup
  1774256235905x641284515280206100  Statutory Accounting Principles (E) Working Group
  1774256235937x220904293572367070  Life Risk Based Capital (E) Working Group
  1774256236079x496547428298829600  Macroprudential (E) Working Group
  1774256236159x375898650656409900  CLO Modelling Ad-hoc Group
  1774256236352x665109628732278900  Property & Casualty Risk-Based Capital (E) Working Group
  1774256236419x371825439038201860  Investment Analysis (E) Working Group
  1774256236423x494787041995732100  Health Risk-Based Capital (E) Working Group
  1774256236568x644112473397786500  Risk Based Capital Investment Risk & Evaluation (E) Working Group
  1774256236609x470477402463802100  Credit Rating Provider (E) Working Group
  1774256236649x794601242509351800  Experience Reporting (A) Subgroup
  1774256236859x410892894855306560  Investment Designation Analysis (E) Working Group
```

---

## 8. Alert Type → Bubble Action Mapping

Source: `bubble/bubble_sync_classifier.py` `_TYPE_MAP`

| Alert Type | Event Action | Library Item Action | Create Agenda Items |
|------------|-------------|--------------------|--------------------|
| New Meeting | create | — | — |
| Updated Meeting | update | — | — |
| New Agenda | update | create | yes |
| New Materials | update | create | no |
| New Agenda & Materials | update | create | yes |
| Updated Agenda | update | update | yes |
| Updated Materials | update | update | no |
| Updated Agenda & Materials | update | update | yes |
| New Request for Comment | create | create | no |
| Updated Request for Comment | update | update | no |
| New Effective Date | create | create | no |
| Updated Effective Date | update | update | no |
| New or Updated Report or Other Resource | update | create | no |
| New Meeting Transcript Available | update | create | no |
| Other | update | create | no |
| No Meaningful Change | — | — | — |
| Alert not relevant - … | — | — | — |

---

## 9. Processing Order and Dependencies

Objects must be created in this strict order because later objects need IDs from earlier ones:

```
1. Resolve all lookups (in parallel, no order dependency):
   - org names → org _id list        (GET /obj/organization, space-scoped, paginate: 2 pages for 146 orgs)
   - chronicle topic names → topic _id list  (GET /obj/chronicletopic, space-scoped, 1 page)
   - libraryitemtype name → type _id  (GET /obj/libraryitemtype, 1 page)
   - calendaritemtype name → type _id (GET versioned /obj/calendaritemtype, 1 page)

2. Agenda items (if any):
   - For "New" status: POST /obj/agendaitem → get _id
   - For "Existing"/"Updated" status: GET /obj/agendaitem?constraints=[BA title = ...] → get _id
   - Collect: agenda_item_ids[]

3. Library item (if any):
   - If "create": POST /obj/libraryitem (include agenda_item_ids from step 2)
   - If "update": GET by URL or Name → PATCH /obj/libraryitem/{id}
   - Collect: library_item_id

4. Calendar item (event):
   - If "create": POST /obj/calendaritem (include agenda_item_ids and library_item_id)
   - If "update": GET by date + org → PATCH /obj/calendaritem/{id} (linking fields only)
```

For UPDATE on calendar item, only send **linking fields** — not datetime/type/orgs which are already set:
```json
{
  "Topics - dt": ["<topic_id>", ...],
  "attached agenda items": ["<ai_id>", ...],
  "Agenda": ["<lib_id>"]
}
```

---

## 10. CREATE Payloads (Data API — Direct)

### 10.1 Create Agenda Item

```
POST /obj/agendaitem
```
```json
{
  "BA title": "VA Scope Clarification",
  "NAIC Title": "VM Variable Annuity Scope Clarification",
  "BA Ref #": "LATF#APF-2025-14",
  "topics - dt": ["<topic_id>", ...],
  "space": "<space_id>",
  "Eiderix Ref": "<agent_call_id>"
}
```
Returns: `{ "status": "success", "id": "<new_agendaitem_id>" }`

**Omit `NAIC Title` and `BA Ref #` if N/A or empty.**

---

### 10.2 Create Library Item

```
POST /obj/libraryitem
```
```json
{
  "Name": "Life Actuarial (A) Task Force Agenda - June 11, 2026",
  "URL": "https://content.naic.org/...",
  "file name": "latf-agenda-061126.pdf",
  "Type - DT": "<libraryitemtype_id>",
  "Organizations": ["<org_id>", ...],
  "Topics - dt": ["<topic_id>", ...],
  "Agenda items": ["<agendaitem_id>", ...],
  "date": "2026-06-11T00:00:00.000Z",
  "Date display": "Full date",
  "summary": "Agenda for the June 11, 2026 LATF meeting...",
  "Status": "Active",
  "Space": "<space_id>",
  "Eiderix Ref": "<agent_call_id>"
}
```
Returns: `{ "status": "success", "id": "<new_libraryitem_id>" }`

**Omit `URL`, `file name`, `summary` if N/A or empty.**
**`date` format:** always send as ISO 8601 UTC midnight: `"YYYY-MM-DDT00:00:00.000Z"`.

---

### 10.3 Create Calendar Item (Event)

```
POST /obj/calendaritem
```
```json
{
  "title": "Life Actuarial (A) Task Force",
  "type": "<calendaritemtype_id>",
  "date": "2026-06-11T18:00:00.000Z",
  "End time": "2026-06-11T19:00:00.000Z",
  "full day": false,
  "Orgs ": ["<org_id>", ...],
  "Topics - dt": ["<topic_id>", ...],
  "attached agenda items": ["<agendaitem_id>", ...],
  "Agenda": ["<libraryitem_id>"],
  "Timezone Code": "America/New_York",
  "location": "https://naic.webex.com/...",
  "phone_number_and_access_code": "+1-415-655-0003,,23366395014##",
  "event description": "Description of the change...",
  "Space": "<space_id>",
  "Eiderix Ref": "<agent_call_id>"
}
```

**Datetime handling:** Send times in UTC. The agent outputs Eastern time ISO 8601 (e.g., `2026-06-11T14:00:00-04:00`). Convert to UTC before sending: `2026-06-11T18:00:00.000Z`.

**Omit** `location`, `phone_number_and_access_code`, `event description`, `attached agenda items`, `Agenda` if N/A or empty.

---

## 11. UPDATE Payloads (Data API — Direct)

### 11.1 Update Library Item

Find first (by `URL` or `Name`), then:
```
PATCH /obj/libraryitem/{id}
```
```json
{
  "URL": "https://...",
  "file name": "revised-agenda.pdf",
  "Topics - dt": ["<topic_id>", ...]
}
```
Only include fields that are actually changing.

---

### 11.2 Update Calendar Item (Event)

Find first (by date + org), then:
```
PATCH /obj/calendaritem/{id}
```

**For "Updated Meeting"** (datetime/location changed):
```json
{
  "date": "2026-06-11T18:00:00.000Z",
  "End time": "2026-06-11T19:00:00.000Z",
  "location": "https://...",
  "phone_number_and_access_code": "...",
  "Topics - dt": ["<topic_id>", ...]
}
```

**For all other update types** (linking a new library item / agenda items to an existing event):
```json
{
  "Topics - dt": ["<topic_id>", ...],
  "attached agenda items": ["<agendaitem_id>", ...],
  "Agenda": ["<libraryitem_id>"]
}
```
Do **not** resend `date`, `type`, `Orgs `, `title` — these are already set on the existing record and resending them can trigger Eidarix validation errors.

---

## 12. Agent Output → Bubble Field Mapping

This maps fields from `alerts_table.jsonl` rows and `bubble_action` previews to what gets sent to Bubble.

| Agent / bubble_action field | Bubble write field | Object |
|---|---|---|
| `event_title` | `title` | calendaritem |
| `event_start_date_time` (convert ET→UTC) | `date` | calendaritem |
| `event_end_date_time` (convert ET→UTC) | `End time` | calendaritem |
| `event_is_full_day` (`"Full Day"` → `true`) | `full day` | calendaritem |
| `event_url` | `location` | calendaritem |
| `event_call_in_number_access_code` | `phone_number_and_access_code` | calendaritem |
| resolved org IDs | `Orgs ` | calendaritem |
| resolved topic IDs | `Topics - dt` | calendaritem |
| resolved agendaitem IDs | `attached agenda items` | calendaritem |
| resolved libraryitem ID | `Agenda` | calendaritem |
| calendaritemtype ID | `type` | calendaritem |
| space ID | `Space` | calendaritem |
| `agent_call_id` | `Eiderix Ref` | calendaritem |
| `library_item_preliminary_title.title` | `Name` | libraryitem |
| `library_item_url` | `URL` | libraryitem |
| `library_items_file_name` | `file name` | libraryitem |
| libraryitemtype ID | `Type - DT` | libraryitem |
| resolved org IDs | `Organizations` | libraryitem |
| resolved topic IDs (union of agenda + doc extraction) | `Topics - dt` | libraryitem |
| resolved agendaitem IDs | `Agenda items` | libraryitem |
| event date (ISO UTC midnight) | `date` | libraryitem |
| `"Full date"` | `Date display` | libraryitem |
| doc extraction summary | `summary` | libraryitem |
| `"Active"` | `Status` | libraryitem |
| space ID | `Space` | libraryitem |
| `agent_call_id` | `Eiderix Ref` | libraryitem |
| `agenda_item_title_chronicle_topics[i].agenda_item_title` | `BA title` | agendaitem |
| `agenda_item_title_official[i].official_title` | `NAIC Title` | agendaitem |
| `agenda_item_standardized_id[i].standardized_id` | `BA Ref #` | agendaitem |
| resolved topic IDs for that agenda item | `topics - dt` | agendaitem |
| space ID | `space` | agendaitem |
| `agent_call_id` | `Eiderix Ref` | agendaitem |

**Chronicle topics for library item** = union of:
1. Topics from all `agenda_item_title_chronicle_topics[i].chronicle_topics[]`
2. Topics from `document_extractions_table.jsonl` row (enriched via `enrich_with_doc_extraction()`)

**Chronicle topics for calendaritem** = same union as library item. Fallback to "Calendar Events with no Topic" (`1772364054836x913377176710762800`) if the union is empty.

**Transcript alert (`"New Meeting Transcript Available"`) topic edge case:** this alert type has no agenda items (`agenda_items: false` in `_TYPE_MAP`), so the chronicle topics come only from the doc extraction for the transcript. If doc extraction produced no topics, apply the empty-topic fallback.

**UPDATE lookup fields (read from `bubble_action.*.match_search` on the row):**
| `match_search` key | Used to find |
|---|---|
| `event_preview.match_search.date` | calendaritem by date range |
| `event_preview.match_search.org` | calendaritem secondary filter by org |
| `library_item_preview.match_search.url` | libraryitem by URL |
| `library_item_preview.match_search.title` | libraryitem fallback by Name |

---

## 13. API Response Shapes

### Success responses

| Operation | HTTP | Body |
|-----------|------|------|
| POST (create) | 201 | `{ "status": "success", "id": "<new_id>" }` |
| PATCH (update) | 204 | *(empty body)* |
| DELETE | 204 | *(empty body)* |
| GET (single) | 200 | `{ "response": { "_id": "...", <fields> } }` |
| GET (list) | 200 | `{ "response": { "cursor": 0, "results": [...], "count": N, "remaining": M } }` |

### Error responses

All errors return JSON with a consistent shape:

```json
{ "statusCode": 400, "body": { "status": "ERROR", "message": "Unrecognized field: ba_title_text" } }
{ "statusCode": 404, "body": { "status": "MISSING_DATA", "message": "Missing object of type libraryitem: object with id <id> does not exist" } }
```

| HTTP | `status` value | Common cause |
|------|---------------|--------------|
| 400 | `"ERROR"` | Unrecognized field, wrong field type, validation failure |
| 401 | `"ERROR"` | Invalid or missing API key |
| 404 | `"MISSING_DATA"` | Object ID not found |

The `message` field is human-readable and specific — log it directly for debugging. The most common error during development is `"Unrecognized field: <name>"` which means a write field name is wrong.

---

## 14. Known Quirks and Gotchas

1. **Write fields use display names** — POST/PATCH body must use exact display names from the Swagger / GET response (e.g., `"Topics - dt"`, `"BA Ref #"`, `"Orgs "`). The underscore convention (`topics___dt_list_custom_newsreel_update`) is rejected with `"Unrecognized field"`.

2. **Constraint search accepts both conventions** — GET constraint `key` accepts `"URL"` or `"url_text"`, `"Name"` or `"name_text"`. Use display names for consistency.

3. **`Orgs ` has a trailing space** — the calendaritem write field name is `"Orgs "` (with trailing space), matching exactly what the GET response returns. Do not trim it.

4. **`agendaitem.Organiozations` is a typo** (extra 'o'). This field exists in the Swagger but is unreliable — org linkage for agenda items flows through calendaritem and libraryitem instead.

5. **`calendaritemtype` requires versioned URL** — `/api/1.1/obj/calendaritemtype` returns 404 even in live. Must use `/version-live/api/1.1/obj/calendaritemtype`.

6. **`libraryitem` has two org list fields**: `Organizations` and `Organization` (singular). Use `Organizations` (plural) — this is the one the app reads.

7. **`date` timezone**: Always send as UTC midnight (`T00:00:00.000Z`) for date-only values (library item `date`). For calendaritem datetimes, convert the agent's Eastern time to UTC before sending.

8. **`Date display` option set values**: confirmed from live data: `"Full date"`, `"Month, Year"`. Also likely `"Year"`. Always send `"Full date"` when we have an exact date.

9. **List fields on PATCH replace the entire list** — Bubble does not append. To add to an existing list (e.g., `attached agenda items`), first fetch the current value, merge, then PATCH the merged result.

10. **Org name exact match** — Chronicle topic names and org names must match Bubble exactly (case-sensitive). "Life RBC Covariance & Asset Concentration Risk" not "RBC Covariance & Asset Concentration Risk". Verify against section 6.

11. **`Status` and `Date display` option sets** — send the string value directly (e.g., `"Active"`, `"Full date"`), not an ID.

12. **`agendaitem.space` is lowercase** — `"space"` (not `"Space"`). Calendaritem and libraryitem use `"Space"` (capital S). This is consistent with their respective GET responses.

13. **Org pagination** — Live space has more than 100 orgs. Always paginate until `remaining == 0`. Two pages sufficient for the current test space (146 orgs), but check `remaining` defensively.

---

## 15. Confirmed Write Field IDs

All of the following have been empirically confirmed via live POST/PATCH tests against the test space Data API:

**agendaitem** (POST confirmed 2026-10-05):
```
"BA title", "NAIC Title", "BA Ref #", "topics - dt", "space", "Eiderix Ref"
```

**libraryitem** (PATCH confirmed 2026-10-05 — POST field names inferred from Swagger but follow the same pattern):
```
"Name", "URL", "file name", "Type - DT", "Organizations", "Topics - dt",
"Agenda items", "date", "Date display", "summary", "Status", "Space", "Eiderix Ref"
```

**calendaritem** (PATCH confirmed 2026-10-05 via display-name test — POST field names inferred from Swagger):
```
"title", "date", "End time", "full day", "type", "Orgs ", "Topics - dt",
"attached agenda items", "Agenda", "Timezone Code", "phone_number_and_access_code",
"location", "event description", "Space", "Eiderix Ref"
```

**First thing to validate when building the new direct-API layer:** POST a test calendaritem and libraryitem using the above field names and verify the records are created correctly. The Bubble API returns a clear `"Unrecognized field: <name>"` error if any field name is wrong. Test POST before building the full route.
