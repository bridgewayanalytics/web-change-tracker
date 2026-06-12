# Bubble Data Model — bridgemind app

> Source: live Bubble Data API — `eidarix.bridgewayanalytics.com`  
> Generated: 2026-06-12  
> Space ID: `1768998437948x865417918648382000`  
> API constants & client: `bubble/bridgemind.py`

---

## API access

```
Base URL : https://eidarix.bridgewayanalytics.com/api/1.1/obj
API key  : 0a951ec86c08a59e274411913ce6aec3
```

All queries scoped to the space above require:
```json
constraints=[{"key":"space","constraint_type":"equals","value":"1768998437948x865417918648382000"}]
```

---

## Object types overview

| API endpoint | Display name | Records | Purpose |
|---|---|---|---|
| `calendaritem` | Calendar Item | ~631 | Meetings, RFCs, adopted guidelines, comment deadlines |
| `libraryitem` | Library item | ~1,041 | Agendas, materials, documents, PDFs |
| `organization` | Organization | ~143 | Org hierarchy tree (NAIC committees, federal agencies, etc.) |
| `chronicletopic` | Chronicle Topic | ~70 | Topic taxonomy nodes (tied to Chronicles) |
| `issue` | Issue | ~84 | Weekly newsreel editions |
| `chronicle` | Chronicle | ~11 | Top-level chronicle categories |
| `contentbit` | Content bit | — | Newsreel content blocks |
| `paragraph` | Paragraph | — | Sub-blocks within content bits |

**Types referenced in schemas but NOT queryable via GET API:**
- `agendaitem` (also called `subtopic` in field IDs) — linked to calendaritem and libraryitem; cannot be listed or fetched independently
- `calendaritemtype` — calendar item type lookup (Meeting, RFC, Adopted Guideline, etc.)
- `libraryitemtype` — library item type lookup (Agenda, Materials, RFC, etc.)
- `treenode`, `chroniclelink`, `sourcematerial`, `resourcesuggestion`, `newsitem`, `reminder`, `jurisdiction`

---

## calendaritem — Calendar Item

Meetings, RFCs, adopted guidelines, comment-period deadlines. One record per distinct event.

**Core fields — what we write from the pipeline:**

| Display name | Field ID | Type | Notes |
|---|---|---|---|
| `_id` | `_id` | text | Bubble unique ID (read-only) |
| `title` | `title_text` | text | Event title (e.g. "NAIC LATF \| VM-22 \| Meeting") |
| `date` | `date_date` | date | Start date/time (ISO 8601) |
| `End time` | `length_end_time_date` | date | End date/time |
| `full day` | `full_day_boolean` | boolean | True for all-day events |
| `Orgs ` | `orgs__list_custom_organization` | list\<organization\> | Linked organizations |
| `phone_number_and_access_code` | `phone_number_and_access_code_text` | text | Call-in number and access code |
| `location` | `location_text` | text | Physical location (if any) |
| `type` | `type_custom_naic_date_meeting_type` | calendaritemtype | Meeting type (not GET-queryable) |
| `Topics - dt` | `topics___dt_list_custom_newsreel_update` | list\<chronicletopic\> | Chronicle topics |

**Relationship fields:**

| Display name | Field ID | Type | Notes |
|---|---|---|---|
| `Agenda` | `relevant_resources_list_custom_resource` | list\<libraryitem\> | Agenda/materials documents linked to this event |
| `attached agenda items` | `attached_initiatives_list_custom_subtopic` | list\<agendaitem\> | Agenda item objects (not independently queryable) |
| `Relevant Documents` | `relevant_document__link__list_custom_chronicle_link` | list\<chroniclelink\> | Chronicle links |

**Other fields:**

| Display name | Field ID | Type | Notes |
|---|---|---|---|
| `body` | `body_text` | text | HTML description body |
| `event description` | `event_description_text` | text | Plain text description |
| `Timezone Code` | `timezone_code_text` | text | e.g. `"America/New_York"` |
| `AMPM from` | `ampm_option_am_pm` | option | `AM` or `PM` |
| `AMPM until` | `ampm_until_option_am_pm` | option | `AM` or `PM` |
| `Hour from` | `hour_option_hours` | option | Start hour |
| `Minutes from` | `minutes_option_minutes` | option | Start minutes |
| `Hours until` | `hours_to_option_hours` | option | End hour |
| `Minutes until` | `minutes_until_option_minutes` | option | End minutes |
| `length` | `length_option_time_lengths` | option | Duration (e.g. `"0:30"`) |
| `Status` | `status_option_status` | option | Record status |
| `name for search` | `name_for_search_text` | text | Lowercase searchable title |
| `origin` | `origin_text` | text | Legacy origin ID |
| `Original creation date` | `original_creation_date_date` | date | Original record date |
| `Outlook Event ID` | `outlook_event_id_text` | text | Outlook calendar sync ID |
| `Outlook Event UID` | `outlook_event_uid_text` | text | Outlook UID |
| `Outlook last sync` | `outlook_last_sync_date` | date | Last Outlook sync timestamp |
| `Topic` | `topic_custom_newsreel_update` | chronicletopic | Single primary topic |
| `has topic` | `has_topic_boolean` | boolean | Whether any topic is linked |
| `NAIC Group (tree node)` | `naic_group__tree_node__custom_tree_nodes` | treenode | NAIC tree node ref |
| `subtopic` | `subtopic_text` | text | Legacy subtopic text |
| `no agenda type` | `no_agenda_type_text` | text | |
| `Temp` | `temp_boolean` | boolean | Temp flag |
| `text` | `text_text` | text | Extra text field |
| `color` | `color_text` | text | Display color |

**Match key for deduplication:** `"{primary_org} | {date[:10]}"` — used by the sync executor to find the existing calendar item before deciding create vs. update.

---

## libraryitem — Library item

Documents, agendas, materials, PDFs, guidelines. One record per distinct document.

**Core fields — what we write from the pipeline:**

| Display name | Field ID | Type | Notes |
|---|---|---|---|
| `_id` | `_id` | text | Bubble unique ID (read-only) |
| `Name` | `name_text` | text | Document title |
| `URL` | `url_text` | text | Source URL (PDF link, NAIC page URL) |
| `file` | `file_file` | file | Bubble CDN file (uploaded binary) |
| `file name` | `file_name_text` | text | Original filename |
| `date` | `date_date` | date | Publication / effective date |
| `Organizations` | `organizations_list_custom_organization` | list\<organization\> | Linked organizations |
| `Type - DT` | `type_custom_library_item_type` | libraryitemtype | Document type (Agenda, Materials, RFC, etc.) — not GET-queryable |
| `Topics - dt` | `topics___dt_list_custom_newsreel_update` | list\<chronicletopic\> | Chronicle topics |
| `Status` | `status_option_status` | option | Record status (e.g. `Active`) |
| `summary` | `description_text` | text | Rich text summary / description |

**Relationship fields:**

| Display name | Field ID | Type | Notes |
|---|---|---|---|
| `Events being created` | `events_being_created_list_custom_calendar_items` | list\<calendaritem\> | Calendar items this doc is linked to |
| `Related calendar items` | `related_meetings_list_custom_calendar_items` | list\<calendaritem\> | Other related events |
| `Agenda items` | `agenda_items_list_custom_subtopic` | list\<agendaitem\> | Agenda item objects for this doc |
| `Agenda items being created` | `agenda_items_being_created_list_custom_subtopic` | list\<agendaitem\> | Agenda items being populated |
| `parent` | `parent_custom_resource` | libraryitem | Parent document (self-referential) |
| `Topic` | `topic_custom_newsreel_update` | chronicletopic | Single primary topic |

**Other fields:**

| Display name | Field ID | Type | Notes |
|---|---|---|---|
| `Date display` | `date_display_option_date_display_type` | option | `"Full date"`, `"Month, Year"`, `"Year"` |
| `full date display?` | `full_date_display__option_yes_no` | option | `"Yes"` or `"No"` |
| `name-for-search` | `name_for_search_text` | text | Lowercase searchable name |
| `origin` | `origin_text` | text | Legacy origin ID |
| `Original creation date` | `original_creation_date_date` | date | Original record date |
| `Available To Vector Store` | `available_to_vector_store_boolean` | boolean | Indexed for RAG |
| `VS Content Type` | `vs_content_type_custom_vector_store_file` | vectorstorecontenttype | Vector store config |
| `archive` | `archive_boolean` | boolean | Archived flag |
| `internal` | `internal_boolean` | boolean | Internal-only flag |
| `favorited` | `favorited_boolean` | boolean | User-favorited |
| `Notes on chronicles` | `notes_on_chronicles_boolean` | boolean | Has chronicle notes |
| `Order` | `order_number` | number | Sort order |
| `reference` | `reference_text` | text | Short reference label |
| `Domain` | `domain_text` | text | Source domain |
| `org - text` | `org___text_list_text` | list\<text\> | Org names as text (denormalized) |
| `type - text` | `type___text_text` | text | Type as plain text |
| `Organization` | `tree_node_list_custom_tree_nodes` | list\<treenode\> | Tree node org refs |
| `Type` | `type_custom_tree_nodes` | treenode | Type tree node |
| `Type1` | `type1_list_custom_tree_nodes` | list\<treenode\> | Additional type tree nodes |
| `sync-response` | `sync_response_text` | text | Last sync response from external system |
| `News in print` | `news_in_print_custom_newsitem` | newsitem | Linked newsreel item |
| `News inprints being created` | `news_inprints_being_created_list_custom_newsitem` | list\<newsitem\> | Newsreel items being created |
| `file2` | `file2_file` | file | Secondary file |
| `Suggestion` | `suggestion_custom_resource_suggestion` | resourcesuggestion | Resource suggestion |
| `topic suggestion` | `topic_suggestion_custom_tree_nodes` | treenode | Topic suggestion |
| `source material` | `source_material_custom_source_material` | sourcematerial | Source material ref |

---

## organization — Organization

Org hierarchy tree. 143 records across 6 levels. Used to scope calendar items and library items.

| Display name | Field ID | Type | Notes |
|---|---|---|---|
| `_id` | `_id` | text | Bubble unique ID |
| `Name` | `name_text` | text | Full org name (e.g. "Life Actuarial (A) Task Force") |
| `Short Name` | `short_name_text` | text | Abbreviation (e.g. "NAIC LATF") |
| `Level` | `level_number` | number | Depth in tree (1=root, 6=deepest) |
| `Parent` | `parent_custom_organization` | organization | Direct parent org |
| `heritage` | `heritage_list_custom_organization` | list\<organization\> | All ancestors (root → parent) |
| `heritage text1` | `heritage_text1_text` | text | Human-readable path: `"NAIC , Organization/Publisher"` |
| `heritage text` | `heritage_text_list_text` | list\<text\> | Ancestor names as text list |
| `URL` | `url_text` | text | Monitoring URL — the NAIC committee page to scrape **(key field for dynamic targets)** |
| `Description` | `description_text` | text | Org description |
| `Order` | `order_number` | number | Sort order within parent |
| `Fav` | `fav_boolean` | boolean | Favorited flag |
| `New` | `new_boolean` | boolean | Newly added flag |
| `Type` | `type_custom_organization_type` | organizationtype | Org type classification |
| `Jurisdiction` | `jurisdiction_custom_jurisdiction` | jurisdiction | Jurisdiction ref |
| `Origin` | `origin_text` | text | Legacy origin ID |
| `Space` | `space_custom_space` | space | Space this org belongs to |

**Tree structure:** Level 1 = "Organization/Publisher" (root). Level 2 = NAIC, Federal, International, etc. Levels 3-6 = committees, task forces, working groups, subgroups.

**Dynamic targets:** Populating `URL` on an org record makes it a monitoring target. The pipeline will query orgs where `URL` is set and use those as scrape targets, replacing static `targets.json` entries.

---

## chronicletopic — Chronicle Topic

Topic taxonomy nodes. Each belongs to a parent `Chronicle`. Used to tag calendar items and library items.

| Display name | Field ID | Type | Notes |
|---|---|---|---|
| `_id` | `_id` | text | Bubble unique ID |
| `Title` | `title_text` | text | Topic title |
| `Description` | `description_text` | text | Topic description |
| `Chronicle DT` | `chronicle_dt_custom_chronicle` | chronicle | Parent chronicle category |
| `Chronicle` | `chronicle_option_chronicles0` | option | Chronicle option value |
| `ordering` | `ordering_number` | number | Sort order |
| `Title order` | `title_order_number` | number | Display order |
| `shortened topic` | `shortened_topic_text` | text | Short label |
| `UUID` | `uuid_text` | text | UUID for external reference |
| `topic_url_slug` | `topic_url_slug_text` | text | URL slug |
| `Whats next` | `adopted_or_possible_changes_text` | text | "What's next" description |
| `Context and State of Play` | `implications_for_investment_and_business_strategy_text` | text | Context description |
| `Calendar Items` | `calendar_items_list_custom_calendar_items` | list\<calendaritem\> | Events tagged with this topic |
| `Favorite Resources` | `favorite_resources_list_custom_resource` | list\<libraryitem\> | Favorited library items |
| `Related Resources` | `related_resources_list_custom_resource` | list\<libraryitem\> | Related library items |
| `Show related resource` | `show_related_resource_list_custom_resource` | list\<libraryitem\> | Featured related resources |
| `Content Bits` | `content_bits_list_custom_content_bit` | list\<contentbit\> | Newsreel content blocks |
| `Content Bits 2` | `content_bits_2_list_custom_content_bit` | list\<contentbit\> | Secondary content blocks |
| `Status` | `status_option_status` | option | `Active` / `Inactive` |
| `hidden y/n` | `hidden_y_n_boolean` | boolean | Hidden from display |
| `Empty topic` | `empty_topic_boolean` | boolean | No content yet |
| `Views` | `views_number` | number | View count |
| `ARTIC Type` | `artic_type_option_artic_tables` | option | ARTIC classification |
| `NewsReel Data Type` | `newsreel_data_type_option_newsreel_data_types` | option | NewsReel classification |

---

## issue — Issue (Newsreel Edition)

Weekly newsreel editions. Also accessible via the `newsreel` endpoint alias.

| Display name | Field ID | Type | Notes |
|---|---|---|---|
| `_id` | `_id` | text | Bubble unique ID |
| `Name` | `name_text` | text | Edition name (e.g. "March 5, 2026") |
| `Date` | `date_date` | date | Publication date |
| `Published Date` | `published_date_date` | date | When published |
| `NR Summary` | `nr_summary_text` | text | Full newsreel body (BBCode rich text) |
| `NR Link` | `nr_link_text` | text | Mailchimp / external link |
| `Current` | `current_boolean` | boolean | Is the current edition |
| `ready` | `ready_boolean` | boolean | Ready to publish |
| `Origin` | `origin_text` | text | Legacy origin ID |

---

## chronicle — Chronicle

Top-level chronicle categories (e.g. "Digital Assets, Crypto & CBDC").

| Display name | Field ID | Type | Notes |
|---|---|---|---|
| `_id` | `_id` | text | Bubble unique ID |
| `Title` | `title_text` | text | Chronicle title |
| `Order` | `order_number` | number | Display order |
| `status` | `status_option_status` | option | `Active` / `Inactive` |
| `Hidden` | `hidden_boolean` | boolean | Hidden from display |
| `Imgae` | `imgae_image` | image | Cover image (note: typo in Bubble schema) |
| `Slug` | `Slug` | text | URL slug |

---

## agendaitem (not GET-accessible)

Agenda item objects linked to both `calendaritem` (via `attached agenda items`) and `libraryitem` (via `Agenda items`). The Bubble Data API does not expose a GET endpoint for this type — agenda items cannot be listed or fetched independently.

They are referenced in field schemas as `custom.agendaitem` and `custom.subtopic` (same type, two names used in different field IDs).

Fields known from context (not confirmed via API):
- Title / agenda item text
- Chronicle topics linked to that agenda item
- Position/order within the meeting

---

## Workflow (POST) endpoints

| Endpoint | Parameters | Purpose |
|---|---|---|
| `json-parser` | `json: text` | Parse JSON text |
| `cb-and-paragraphs` | `_wf_request_data` | Create content bits and paragraphs |
| `connect-resources-to-topics-from-ba` | `_wf_request_data` | Link library items to chronicle topics from Bridgeway Analytics |
| `handle-auth0-code` | `code: text` | Auth0 OAuth callback |
| `sync-chronicles-page-views-to-ba` | `space` | Sync chronicle page views to BA |
| `news-in-print-convert-files-to-url_copy` | `item: newsitem` | Convert newsreel file to URL |

---

## Field ID reference for writes

When writing to the Bubble API via PATCH/POST, use the **field ID** (e.g. `title_text`), not the display name.

```python
# Example: create a calendar item
fields = {
    "title_text": "NAIC LATF | VM-22 | Meeting",
    "date_date": "2026-06-15T09:00:00.000Z",
    "length_end_time_date": "2026-06-15T11:00:00.000Z",
    "full_day_boolean": False,
    "orgs__list_custom_organization": ["<org_id>"],
    "phone_number_and_access_code_text": "888-123-4567 / 9876543",
    "timezone_code_text": "America/New_York",
}

# Example: create a library item
fields = {
    "name_text": "LATF Meeting Agenda - June 2026",
    "url_text": "https://content.naic.org/sites/default/files/...",
    "date_date": "2026-06-15T00:00:00.000Z",
    "organizations_list_custom_organization": ["<org_id>"],
    "status_option_status": "Active",
}
```
