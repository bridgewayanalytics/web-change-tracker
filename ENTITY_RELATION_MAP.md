# Entity Relation Map

> Visual reference for the normalized data model described in VECTORIZATION_MODEL_SPEC.md

## Entity Relationship Diagram (Text)

```
┌─────────────────────┐
│   chronicle_topics   │
│─────────────────────│
│ id (PK)             │
│ name                │
│ parent_id (FK→self) │
│ path                │
│ level               │
└────────┬────────────┘
         │
         │ agenda_item_topics
         │ (agenda_item_id, topic_id)
         │
┌────────┴────────────┐         ┌──────────────────────┐
│    agenda_items      │         │     org_nodes         │
│─────────────────────│         │──────────────────────│
│ id (PK)             │         │ id (PK)              │
│ ba_title            │         │ name                 │
│ naic_title          │         │ parent_id (FK→self)  │
│ ba_ref              │         │ path                 │
│ ref_number          │         │ level                │
│ ref_prefix          │         └──────────┬───────────┘
│ description         │                    │
│ category            │  agenda_item_groups│
│ status              │←───────────────────┘
│ priority            │  (agenda_item_id, org_node_id)
│ primary_group_id ───│─── FK ────────────→ org_nodes.id
│ primary_group_name  │
└──┬──────────┬───────┘
   │          │
   │          │ agenda_item_resources
   │          │ (agenda_item_id, resource_id)
   │          │
   │   ┌──────┴──────────────┐
   │   │     resources        │
   │   │─────────────────────│
   │   │ id (PK)             │         ┌──────────────────────┐
   │   │ name                │         │   resource_types      │
   │   │ url                 │         │──────────────────────│
   │   │ date                │         │ id (PK)              │
   │   │ resource_type_id ───│── FK ──→│ name                 │
   │   │ resource_type       │         └──────────────────────┘
   │   │ topic_suggestion_id─│── FK ──→ chronicle_topics.id
   │   │ topic_suggestion_   │
   │   │   name              │
   │   │ is_archived         │
   │   │ is_internal         │
   │   │ vectorizable        │
   │   │ parent_resource_id  │── FK → resources.id (self)
   │   └──┬──────────┬───────┘
   │      │          │
   │      │          │ resource_organizations
   │      │          │ (resource_id, org_node_id)
   │      │          └────────────────────────→ org_nodes.id
   │      │
   │      │ resource_calendar_items
   │      │ (resource_id, calendar_item_id)
   │      │
   │   ┌──┴──────────────────┐
   │   │   calendar_items     │
   │   │─────────────────────│
   │   │ id (PK)             │
   │   │ title               │
   │   │ date                │
   │   │ end_time            │
   │   │ timezone            │
   │   │ location            │
   │   │ description         │
   │   │ is_full_day         │
   │   │ duration            │
   │   │ naic_group_id ──────│── FK ──→ org_nodes.id
   │   │ naic_group_name     │
   │   │ naic_group_path     │
   │   │ subtopic            │
   │   │ has_topic           │
   │   └──┬──────────┬───────┘
   │      │          │
   │      │          │ calendar_item_materials
   │      │          │ (calendar_item_id, url, title)
   │      │          │ [inline agenda links, NOT FKs]
   │      │
   │      │ calendar_item_agenda_items
   └──────│ (calendar_item_id, agenda_item_id)
          │
          │
   ┌──────┴──────────────────┐
   │       alerts             │
   │─────────────────────────│
   │ id (PK)                 │
   │ alert_type              │
   │ date                    │
   │ calendar_item_id ───────│── FK ──→ calendar_items.id
   │ trigger_url             │── soft join → resources.url
   └─────────────────────────┘
```

## Join Tables Summary

| Join Table | Left Entity | Right Entity | Cardinality | Source Field |
|-----------|-------------|--------------|-------------|-------------|
| `agenda_item_topics` | agenda_items | chronicle_topics | M:N | Agenda Item.Topics |
| `agenda_item_resources` | agenda_items | resources | M:N | Agenda Item.Resources |
| `agenda_item_groups` | agenda_items | org_nodes | M:N | Agenda Item.Discussed at list |
| `resource_calendar_items` | resources | calendar_items | M:N | Resource.Related calendar items |
| `resource_organizations` | resources | org_nodes | M:N | Resource.Organization |
| `calendar_item_agenda_items` | calendar_items | agenda_items | M:N | Calendar Item.attached agenda items |
| `calendar_item_materials` | calendar_items | (inline) | 1:N | Calendar Item.Agenda[] |

## Key Query Patterns

### "What topics does this meeting cover?"

```sql
SELECT DISTINCT ct.name, ct.path
FROM calendar_items ci
JOIN calendar_item_agenda_items ciai ON ci.id = ciai.calendar_item_id
JOIN agenda_item_topics ait ON ciai.agenda_item_id = ait.agenda_item_id
JOIN chronicle_topics ct ON ait.topic_id = ct.id
WHERE ci.id = ?
```

### "What resources relate to this agenda item?"

```sql
SELECT r.name, r.url, r.date, r.resource_type
FROM resources r
JOIN agenda_item_resources air ON r.id = air.resource_id
WHERE air.agenda_item_id = ?
```

### "What meetings discussed this topic?"

```sql
SELECT DISTINCT ci.title, ci.date, ci.naic_group_name
FROM chronicle_topics ct
JOIN agenda_item_topics ait ON ct.id = ait.topic_id
JOIN calendar_item_agenda_items ciai ON ait.agenda_item_id = ciai.agenda_item_id
JOIN calendar_items ci ON ciai.calendar_item_id = ci.id
WHERE ct.name = ?
ORDER BY ci.date DESC
```

### "All publications for a specific NAIC group"

```sql
SELECT r.name, r.url, r.date, r.topic_suggestion_name
FROM resources r
JOIN resource_organizations ro ON r.id = ro.resource_id
JOIN org_nodes o ON ro.org_node_id = o.id
WHERE o.path LIKE '%Capital Adequacy%'
  AND r.resource_type = 'Publication'
ORDER BY r.date DESC
```

### "Recent alerts with full context"

```sql
SELECT a.alert_type, a.date,
       ci.title as meeting_title, ci.date as meeting_date,
       ci.naic_group_name,
       r.name as triggering_resource
FROM alerts a
JOIN calendar_items ci ON a.calendar_item_id = ci.id
LEFT JOIN resources r ON a.trigger_url = r.url
ORDER BY a.date DESC
LIMIT 20
```

## Bubble ID Examples

For reference, Bubble IDs follow this format: `{timestamp}x{random}` (e.g., `1731019820042x652190297443795000`).

| Entity | Example ID |
|--------|-----------|
| Resource | `1731019820042x652190297443795000` |
| Calendar Item | `1693984156111x836761350932267000` |
| Agenda Item | `1715005045580x313445082397541200` |
| Tree Node (Org) | `1709122405701x990044753610408000` |
| Tree Node (Topic) | `1710771343109x631718453703900000` |
| Tree Node (Type1) | `1709756450783x980012943297740800` |
| Alert | `1773174648402x355828714295882700` |
| Tree | `1710771208905x698956612053237800` |
