# tightbeam-workitem-pies

A small, read-only browser view of recorded work-item activity. The Python server uses only the standard library and serves the bundled browser UI. It reads SQLite in read-only mode and never writes to the database or calls a runtime service.

## Requirements

- Python 3.9 or newer
- A browser with JavaScript enabled
- A SQLite database with the tables and columns listed below

There is no package installation, build step, or frontend toolchain.

## Install and run

```sh
git clone https://github.com/clickety-clacks/tightbeam-workitem-pies.git
cd tightbeam-workitem-pies
cp groups.example.json groups.json
# Edit groups.json with the inventory IDs and groups for this deployment.
python3 server.py --db "$HOME/.tightbeam/state.db" --groups groups.json --port 4391
```

Open <http://localhost:4391/> in a browser. The server binds to loopback (`127.0.0.1`) and has no authentication, so treat it as trusted local or private-network access. Put it behind an authenticated, private reverse proxy if it must be reached from elsewhere. A proxy may mount it at `/work-circles/`; preserve that trailing slash and route `/work-circles/` and its relative API requests to the server.

The server has no deployment or service-manager configuration. Stop it with `Ctrl-C`.

## Inventory configuration

`groups.json` is local deployment data and is ignored by Git. The simplest form lists IDs and their display groups:

```json
{
  "source": "A description of this inventory",
  "items": [
    {"id": "real-work-item-id", "group": "A display group"}
  ]
}
```

For an existing inventory file, use `inventoryPath` instead. It points to a JSON array whose records contain `id` and `category`:

```json
{
  "source": "A description of this inventory",
  "inventoryPath": "inventory.json"
}
```

```json
[
  {"id": "real-work-item-id", "category": "A display group"}
]
```

A relative `inventoryPath` is resolved relative to the directory containing `groups.json`. Tilde paths are expanded. The example file contains only an obvious placeholder and is not a demo dataset.

## What the diagram means

Each pie is one configured work item. Its sectors are the archetypes present in that item's assignments or turns, in one deterministic global order. A turn uses its recorded session archetype. Assignment and review counts use the holder session archetype. Missing archetypes appear as `Unknown`; an item with no assignments or turns is an empty outline. The session archetype is current metadata, so it is not historical proof for every turn. The reader does not infer attribution from titles or silently discard coordination archetypes.

Within a sector, radial distance represents the gap between directly linked turns in that recorded archetype. The default log scale keeps short and long gaps visible together. The histogram opacity is the count in each duration bin relative to the fullest bin in that sector. Review returns control the blue-to-red color scale. Striped outer rims count gaps beyond the selected duration limit. The legend shows an illustrative archetype example; its bands are guides, not measurements.

A timing gap can include legitimate work elsewhere and does not establish a stall, idle time, current stage, or completion percentage. The database does not record enough information to claim the current stage or percentage complete, so the view does not infer either.

## Reader compatibility

The current reader expects these SQLite objects and columns:

- `work_items`: `id`, `title`, `state`, `createdAt`
- `sessions`: `sessionKey`, `archetype`
- `assignments`: `id`, `workItemId`, `holderKey`, `holderRole`, `reviewsAssignmentId`, `openedAt`, `state`
- `turns`: `seq`, `assignmentId`, `sessionKey`, `roleRef`, `status`, `startedAt`, `endedAt`
- `attests`: `id`, `assignmentId`, `ts`, `kind`, `verdictKind`

It reads verdicts where `kind` is `verdict` and `verdictKind` is `changes-requested`. The query shape is tied to this schema family; a schema migration or an older database with different names may require a reader update. The server applies `PRAGMA query_only=ON`, uses a read-only SQLite URI, and closes each bounded read without committing anything.

## Controls and refresh

The page refreshes live data every 15 seconds while visible. A failed read keeps the last view and marks it stale. Search and recorded-state filters work on the configured inventory. Hovering or focusing a pie shows its recorded archetype labels; a short hover dwell expands the pie, and clicking opens its recorded item details.

The right control rail provides knobs for circle radius, review-return color range, maximum gap duration, histogram bands, and hover dwell. Log radius can be disabled for a linear duration scale. Theme supports Auto, Light, and Dark; Auto follows the system preference, while an explicit choice is kept in browser storage for that origin.
