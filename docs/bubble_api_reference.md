# Bubble (Eidarix) API Reference

**Purpose:** Complete technical reference for publishing alerts directly to the Eidarix Bubble app, without going through the workflow (`wf/`) endpoints. Building block for the content-gate redesign.

**Last verified:** 2026-10-05 against test space. All IDs, field names, and enum values confirmed from live API calls.

---

## 1. Base URLs and Auth

```
Test:       https://eidarix.bridgewayanalytics.com/version-test/api/1.1/
Live:       https://eidarix.bridgewayanalytics.com/api/1.1/
Auth:       Authorization: Bearer 0a951ec86c08a59e274411913ce6aec3
Content-Type: application/json
```

All endpoints follow the pattern:
```
GET    /obj/{type}             — search/list (with constraints)
POST   /obj/{type}             — create new record, returns { id: "<_id>", status: "success" }
GET    /obj/{type}/{id}        — fetch single record
PATCH  /obj/{type}/{id}        — partial update (only fields in body are changed)
PUT    /obj/{type}/{id}        — full replace
DELETE /obj/{type}/{id}        — delete
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

---

## 4. Write Field ID Convention

Bubble Data API reads return display field names (e.g., `"Topics - dt"`). Writes (POST/PATCH) use **internal Bubble field IDs** which follow this convention:

```
display_name_lowercased_spaces_to_underscores + _ + type_suffix
```

Type suffixes:
| Suffix | Field type |
|--------|------------|
| `_text` | text / long text |
| `_date` | date or datetime |
| `_boolean` | yes/no |
| `_number` | numeric |
| `_option_<setname>` | option set |
| `_custom_<typename>` | single reference to another type |
| `_list_custom_<typename>` | list of references to another type |

Special encoding: spaces and hyphens in display names become underscores; repeated special chars (like ` - `) produce double/triple underscores.

Examples:
| Display name (read) | Write field ID |
|---------------------|---------------|
| `title` | `title_text` |
| `date` | `date_date` |
| `End time` | `length_end_time_date` |
| `full day` | `full_day_boolean` |
| `Orgs ` *(trailing space)* | `orgs__list_custom_organization` |
| `Organizations` | `organizations_list_custom_organization` |
| `Topics - dt` | `topics___dt_list_custom_newsreel_update` *(triple underscore for ` - `)* |
| `Type - DT` | `type___dt_custom_libraryitemtype` |
| `BA title` | `ba_title_text` |
| `BA Ref #` | `ba_ref___text` |
| `NAIC Title` | `naic_title_text` |
| `topics - dt` *(agendaitem)* | `topics___dt_list_custom_chronicletopic` |
| `status` | `status_option_status` |
| `Name` | `name_text` |
| `URL` | `url_text` |
| `file name` | `file_name_text` |
| `summary` | `summary_text` |
| `Timezone Code` | `timezone_code_text` |
| `phone_number_and_access_code` | `phone_number_and_access_code_text` |
| `event description` | `event_description_text` |
| `attached agenda items` | `attached_agenda_items_list_custom_agendaitem` |
| `Agenda` *(calendaritem)* | `agenda_list_custom_libraryitem` |
| `Agenda items` *(libraryitem)* | `agenda_items_list_custom_agendaitem` |
| `Date display` | `date_display_option_date_display` |

---

## 5. Data Types

### 5.1 `calendaritem` — Calendar Event

**Endpoint:** `GET/POST /obj/calendaritem`, `PATCH /obj/calendaritem/{id}`

**Key fields (as returned by GET):**

| Field (read) | Write field ID | Type | Notes |
|---|---|---|---|
| `_id` | — | string | Bubble unique ID |
| `title` | `title_text` | string | Event title |
| `date` | `date_date` | datetime | Start datetime, ISO 8601 (e.g. `2026-06-11T14:00:00.000Z`) |
| `End time` | `length_end_time_date` | datetime | End datetime |
| `full day` | `full_day_boolean` | boolean | Full-day event |
| `type` | `type_custom_calendaritemtype` | string | → `calendaritemtype._id` |
| `Orgs ` *(trailing space)* | `orgs__list_custom_organization` | array[string] | → `organization._id` list |
| `Topics - dt` | `topics___dt_list_custom_newsreel_update` | array[string] | → `chronicletopic._id` list |
| `attached agenda items` | `attached_agenda_items_list_custom_agendaitem` | array[string] | → `agendaitem._id` list |
| `Agenda` | `agenda_list_custom_libraryitem` | array[string] | → `libraryitem._id` list |
| `Timezone Code` | `timezone_code_text` | string | Always `"America/New_York"` for us |
| `phone_number_and_access_code` | `phone_number_and_access_code_text` | string | Dial-in + access code |
| `location` | `location_text` | string | Meeting URL |
| `event description` | `event_description_text` | string | Description text |
| `Space` | `space_custom_space` | string | → `space._id` (set on create) |

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

**Key fields (as returned by GET):**

| Field (read) | Write field ID | Type | Notes |
|---|---|---|---|
| `_id` | — | string | Bubble unique ID |
| `Name` | `name_text` | string | Document title |
| `URL` | `url_text` | string | Source URL |
| `file name` | `file_name_text` | string | Filename (e.g., `agenda-06-11.pdf`) |
| `Type - DT` | `type___dt_custom_libraryitemtype` | string | → `libraryitemtype._id` |
| `Organizations` | `organizations_list_custom_organization` | array[string] | → `organization._id` list |
| `Topics - dt` | `topics___dt_list_custom_newsreel_update` | array[string] | → `chronicletopic._id` list |
| `Agenda items` | `agenda_items_list_custom_agendaitem` | array[string] | → `agendaitem._id` list |
| `date` | `date_date` | datetime | Document date (send as `YYYY-MM-DDT00:00:00.000Z`) |
| `Date display` | `date_display_option_date_display` | option set | `"Full date"`, `"Month, Year"`, `"Year"` |
| `summary` | `summary_text` | string | AI-generated summary |
| `status` | `status_option_status` | option set | Always `"Active"` for new records |
| `Space` | `space_custom_space` | string | → `space._id` |

**Auto-managed by Bubble (do NOT send in write payload):**
- `name-for-search` — auto-lowercased from `Name`
- `full date display?` — computed from `Date display`
- `News in print` — set when linked to a newsreel issue
- `Available To Vector Store` — set by vector store workflow
- `file` — binary upload; we link by URL only
- `origin` — legacy field

**Lookup for UPDATE (finding an existing record):**
- Primary: match by `url_text` equals `<url>`
- Fallback: match by `name_text` equals `<title>`
- Once found: PATCH by `_id`

---

### 5.3 `agendaitem` — Agenda Item

**Endpoint:** `GET/POST /obj/agendaitem`, `PATCH /obj/agendaitem/{id}`

**Key fields (as returned by GET):**

| Field (read) | Write field ID | Type | Notes |
|---|---|---|---|
| `_id` | — | string | Bubble unique ID |
| `BA title` | `ba_title_text` | string | Bridgeway Analytics title (our label) |
| `NAIC Title` | `naic_title_text` | string | Official NAIC agenda title |
| `BA Ref #` | `ba_ref___text` | string | Reference ID (e.g. `LATF#APF-2025-14`) |
| `topics - dt` | `topics___dt_list_custom_chronicletopic` | array[string] | → `chronicletopic._id` list |
| `space` | `space_custom_space` | string | → `space._id` |

**Auto-managed by Bubble:**
- `text field for search` — auto-lowercased from `BA title`
- `origin` — legacy

**Important notes:**
- Agenda items are **always CREATE** — they are never updated in place
- `BA title` is the field used for lookup by title (not `NAIC Title`)
- For "Existing" / "Updated" status items from the agent: search by `BA title` equals `<title>` to find the existing `_id`, then just link that ID — do not re-create
- The `Organiozations` field (typo in field name) exists in the schema but is not reliably used; org linkage flows through the calendaritem and libraryitem instead

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

Or load all (146 in test space) and resolve in-memory.

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

**Write field ID on calendaritem:** `type_custom_calendaritemtype` → value is the `_id` above

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

**Write field ID on libraryitem:** `type___dt_custom_libraryitemtype` → value is the `_id` above

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

Key NAIC orgs present in the test space (Level 4+ are the working groups/task forces the agent outputs):

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

**Note:** The test space has all the correct NAIC org names and IDs. Live space has additional orgs. Always resolve by exact `Name` match at runtime — never hardcode IDs.

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
   - org names → org _id list
   - chronicle topic names → topic _id list
   - libraryitemtype name → type _id
   - calendaritemtype name → type _id

2. Agenda items (if any):
   - For "New" status: POST /obj/agendaitem → get _id
   - For "Existing"/"Updated" status: GET /obj/agendaitem?constraints=[BA title = ...] → get _id
   - Collect: agenda_item_ids[]

3. Library item (if any):
   - If "create": POST /obj/libraryitem (include agenda_item_ids from step 2 in Agenda items field)
   - If "update": GET by URL or title → PATCH /obj/libraryitem/{id}
   - Collect: library_item_id

4. Calendar item (event):
   - If "create": POST /obj/calendaritem (include agenda_item_ids and library_item_id)
   - If "update": GET by date + org → PATCH /obj/calendaritem/{id} (linking fields only)
```

For UPDATE on calendar item, only send **linking fields** — not datetime/type/orgs which are already set:
```json
{
  "topics___dt_list_custom_newsreel_update": ["<topic_id>", ...],
  "attached_agenda_items_list_custom_agendaitem": ["<ai_id>", ...],
  "agenda_list_custom_libraryitem": ["<lib_id>"]
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
  "ba_title_text": "VA Scope Clarification",
  "naic_title_text": "VM Variable Annuity Scope Clarification",
  "ba_ref___text": "LATF#APF-2025-14",
  "topics___dt_list_custom_chronicletopic": ["<topic_id>", ...],
  "space_custom_space": "<space_id>"
}
```
Returns: `{ "id": "<new_agendaitem_id>", "status": "success" }`

**Omit `naic_title_text` and `ba_ref___text` if N/A or empty.**

---

### 10.2 Create Library Item

```
POST /obj/libraryitem
```
```json
{
  "name_text": "Life Actuarial (A) Task Force Agenda - June 11, 2026",
  "url_text": "https://content.naic.org/...",
  "file_name_text": "latf-agenda-061126.pdf",
  "type___dt_custom_libraryitemtype": "<libraryitemtype_id>",
  "organizations_list_custom_organization": ["<org_id>", ...],
  "topics___dt_list_custom_newsreel_update": ["<topic_id>", ...],
  "agenda_items_list_custom_agendaitem": ["<agendaitem_id>", ...],
  "date_date": "2026-06-11T00:00:00.000Z",
  "date_display_option_date_display": "Full date",
  "summary_text": "Agenda for the June 11, 2026 LATF meeting...",
  "status_option_status": "Active",
  "space_custom_space": "<space_id>"
}
```
Returns: `{ "id": "<new_libraryitem_id>", "status": "success" }`

**Omit `url_text`, `file_name_text`, `summary_text` if N/A or empty.**
**`date_date` format:** always send as ISO 8601 UTC midnight: `"YYYY-MM-DDT00:00:00.000Z"`.

---

### 10.3 Create Calendar Item (Event)

```
POST /obj/calendaritem
```
```json
{
  "title_text": "Life Actuarial (A) Task Force",
  "type_custom_calendaritemtype": "<calendaritemtype_id>",
  "date_date": "2026-06-11T18:00:00.000Z",
  "length_end_time_date": "2026-06-11T19:00:00.000Z",
  "full_day_boolean": false,
  "orgs__list_custom_organization": ["<org_id>", ...],
  "topics___dt_list_custom_newsreel_update": ["<topic_id>", ...],
  "attached_agenda_items_list_custom_agendaitem": ["<agendaitem_id>", ...],
  "agenda_list_custom_libraryitem": ["<libraryitem_id>"],
  "timezone_code_text": "America/New_York",
  "location_text": "https://naic.webex.com/...",
  "phone_number_and_access_code_text": "+1-415-655-0003,,23366395014##",
  "event_description_text": "Description of the change...",
  "space_custom_space": "<space_id>"
}
```

**Datetime handling:** Send times in UTC. The agent outputs Eastern time ISO 8601 (e.g., `2026-06-11T14:00:00-04:00`). Convert to UTC before sending: `2026-06-11T18:00:00.000Z`.

**Omit** `location_text`, `phone_number_and_access_code_text`, `event_description_text`, `attached_agenda_items_list_custom_agendaitem`, `agenda_list_custom_libraryitem` if N/A or empty.

---

## 11. UPDATE Payloads (Data API — Direct)

### 11.1 Update Library Item

Find first (by URL or title), then:
```
PATCH /obj/libraryitem/{id}
```
```json
{
  "url_text": "https://...",
  "file_name_text": "revised-agenda.pdf",
  "topics___dt_list_custom_newsreel_update": ["<topic_id>", ...]
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
  "date_date": "2026-06-11T18:00:00.000Z",
  "length_end_time_date": "2026-06-11T19:00:00.000Z",
  "location_text": "https://...",
  "phone_number_and_access_code_text": "...",
  "topics___dt_list_custom_newsreel_update": ["<topic_id>", ...]
}
```

**For all other update types** (linking a new library item / agenda items to an existing event):
```json
{
  "topics___dt_list_custom_newsreel_update": ["<topic_id>", ...],
  "attached_agenda_items_list_custom_agendaitem": ["<agendaitem_id>", ...],
  "agenda_list_custom_libraryitem": ["<libraryitem_id>"]
}
```
Do **not** resend `date_date`, `type`, `orgs`, `title` — these are already set on the existing record and resending them can trigger Eidarix validation errors.

---

## 12. Agent Output → Bubble Field Mapping

This maps fields from `alerts_table.jsonl` rows (agent output) to what gets sent to Bubble.

| Agent field | Used for |
|-------------|----------|
| `agent_call_id` | Tracing only — stored in our JSONL; not sent to Bubble |
| `alert_type` | Determines action map (section 8) |
| `organization[]` | Resolved to org `_id` list for events and library items |
| `event_title` | → `title_text` on calendaritem |
| `event_start_date_time` | → `date_date` on calendaritem (convert ET → UTC) |
| `event_end_date_time` | → `length_end_time_date` on calendaritem (convert ET → UTC) |
| `event_is_full_day` | → `full_day_boolean` (`"Full Day"` → `true`) |
| `event_url` | → `location_text` on calendaritem |
| `event_call_in_number_access_code` | → `phone_number_and_access_code_text` |
| `library_item_preliminary_title.title` | → `name_text` on libraryitem |
| `library_item_url` | → `url_text` on libraryitem |
| `library_items_file_name` | → `file_name_text` on libraryitem |
| `agenda_item_title_chronicle_topics[i].agenda_item_title` | → `ba_title_text` on agendaitem |
| `agenda_item_title_chronicle_topics[i].chronicle_topics[]` | → Resolved topic IDs → `topics___dt_list_custom_chronicletopic` on agendaitem |
| `agenda_item_title_official[i].official_title` | → `naic_title_text` on agendaitem |
| `agenda_item_standardized_id[i].standardized_id` | → `ba_ref___text` on agendaitem |
| `bubble_action.event_preview.match_search.date` | Used to find existing calendaritem |
| `bubble_action.event_preview.match_search.org` | Used to narrow calendaritem search |
| `bubble_action.library_item_preview.match_search.url` | Used to find existing libraryitem |
| `bubble_action.library_item_preview.match_search.title` | Fallback for libraryitem lookup |

**Chronicle topics for library item** = union of:
1. Topics from all `agenda_item_title_chronicle_topics[i].chronicle_topics[]`
2. Topics from `document_extractions_table.jsonl` row (enriched via `enrich_with_doc_extraction()`)

**Chronicle topics for calendaritem** = same union as library item + fallback to "Calendar Events with no Topic" (`1772364054836x913377176710762800`) if empty.

---

## 13. Known Quirks and Gotchas

1. **`Orgs ` has a trailing space** in the Data API response. The write field ID is `orgs__list_custom_organization` (double underscore encodes the trailing space).

2. **`agendaitem.Organiozations` is a typo** (extra 'o'). This field is present in the swagger but unreliable — org linkage for agenda items flows through calendaritem and libraryitem.

3. **`calendaritemtype` requires versioned URL** — `/api/1.1/obj/calendaritemtype` returns 404 even in live. Must use `/version-live/api/1.1/obj/calendaritemtype`.

4. **`libraryitem` has two org list fields**: `Organizations` (write: `organizations_list_custom_organization`) and `Organization` (write: `organization_list_custom_organization`). Use `Organizations` (plural, no trailing space) — this is the one the app reads.

5. **`date_date` timezone**: Always send as UTC midnight (`T00:00:00.000Z`) for date-only values. For datetimes, convert the agent's Eastern time to UTC before sending.

6. **`date_display` option set values**: confirmed values from live data: `"Full date"`, `"Month, Year"`. Also likely `"Year"`. Always send `"Full date"` when we have an exact date.

7. **List fields on PATCH**: Bubble replaces the entire list, not appends. To add to an existing list (e.g., `attached_agenda_items`), first fetch the current value, merge, then PATCH the merged result.

8. **Org name exact match**: Chronicle topic names and org names must match Bubble exactly. The test space has all real NAIC org names. "Life RBC Covariance & Asset Concentration Risk" (not "RBC Covariance & Asset Concentration Risk") — verify topic names against section 6 above.

9. **`ba_ref___text` triple underscore**: the `#` in `BA Ref #` becomes `___` in the write field ID.

10. **Status option set**: Send the string value directly (e.g., `"Active"`) not an ID. Option sets in Bubble are stored as string values.

---

## 14. Confirmed vs. Inferred Write Field IDs

The current codebase uses Eidarix **workflow** endpoints (`wf/create-*`) for all creates, and the Bubble **Data API** only for library item PATCH updates. The transition to direct Data API for creates requires testing each POST field name.

**Confirmed** (used in existing PATCH calls in `bubble_sync_classifier.py` and `bubble_sync.py`):
```
title_text, date_date, length_end_time_date, full_day_boolean,
orgs__list_custom_organization, phone_number_and_access_code_text, timezone_code_text,
topics___dt_list_custom_newsreel_update (calendaritem),
name_text, url_text, file_name_text, organizations_list_custom_organization,
status_option_status, description_text
```

**Inferred** (follow Bubble naming convention but not yet tested in Data API POST):
```
ba_title_text, naic_title_text, ba_ref___text,
topics___dt_list_custom_chronicletopic (agendaitem),
type___dt_custom_libraryitemtype, type_custom_calendaritemtype,
space_custom_space, agenda_items_list_custom_agendaitem,
attached_agenda_items_list_custom_agendaitem, agenda_list_custom_libraryitem,
location_text, event_description_text, date_display_option_date_display, summary_text
```

**First thing to validate when building the new direct-API layer:** POST a test agendaitem using the inferred field IDs and verify the record is created correctly. The Bubble API returns a clear error if a field ID is unrecognized.
