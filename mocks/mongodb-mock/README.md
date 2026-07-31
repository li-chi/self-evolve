# mongodb-mock

Mock MCP server that mirrors `mongodb-mcp-server@0.2.0` (the official
MongoDB MCP server registered by mcp-atlas, source:
[mongodb-js/mongodb-mcp-server](https://github.com/mongodb-js/mongodb-mcp-server)).
The upstream Node server speaks the real MongoDB driver to a live
`mongod`; this mock keeps the **same MCP tool names and argument
shapes** but is backed by an in-process Mongo-like layer that stores
each collection as a JSON array in a single state file.

Atlas's tool-name normalisation prefixes the server name and
underscores the dashes, so e.g. upstream `collection-schema` becomes
`mongodb_collection-schema` on the agent side -- the names registered
here use the **upstream** form.

## Tool surface

| tool                       | upstream class                       | notes                                                                 |
|----------------------------|--------------------------------------|-----------------------------------------------------------------------|
| `connect`                  | `ConnectTool`                        | no-op; logs `connectionString`                                        |
| `switch-connection`        | `ConnectTool` (connected variant)    | no-op                                                                 |
| `list-databases`           | `ListDatabasesTool`                  | `Name: <db>, Size: <bytes>` per db                                    |
| `list-collections`         | `ListCollectionsTool`                | `database` arg                                                        |
| `collection-schema`        | `CollectionSchemaTool`               | inferred from up to 5 docs, returns `mongodb-schema`-style field map  |
| `collection-indexes`       | `CollectionIndexesTool`              |                                                                       |
| `collection-storage-size`  | `CollectionStorageSizeTool`          | bytes summed from JSON serialised docs                                |
| `db-stats`                 | `DbStatsTool`                        |                                                                       |
| `find`                     | `FindTool`                           | `filter`, `projection`, `limit` (default 10), `sort`                  |
| `count`                    | `CountTool`                          | `query`                                                               |
| `aggregate`                | `AggregateTool`                      | `pipeline`                                                            |
| `insert-many`              | `InsertManyTool`                     | auto `_id` (24-hex ObjectId-like)                                     |
| `update-many`              | `UpdateManyTool`                     | `filter`, `update`, `upsert`                                          |
| `delete-many`              | `DeleteManyTool`                     | `filter`                                                              |
| `create-collection`        | `CreateCollectionTool`               |                                                                       |
| `create-index`             | `CreateIndexTool`                    |                                                                       |
| `drop-collection`          | `DropCollectionTool`                 |                                                                       |
| `drop-database`            | `DropDatabaseTool`                   |                                                                       |
| `rename-collection`        | `RenameCollectionTool`               |                                                                       |
| `explain`                  | `ExplainTool`                        | synthetic queryPlanner: picks the first index whose key is in filter  |
| `mongodb-logs`             | `LogsTool`                           | returns recent entries from the mock's own `calls` log                |

Plus two mock-only debug tools used by per-task setup/verification:

- `mock_debug_state` -- return the full persisted state dict.
- `mock_debug_seed` -- bulk-insert documents into `<database>.<collection>`
  with auto-`_id`, bypassing every check.

## Filter / query coverage

The filter engine implements the operators upstream mongodb-mcp-server
passes through to the driver:

| operator      | supported | notes                                            |
|---------------|-----------|--------------------------------------------------|
| `$eq` `$ne`   | yes       | implicit equality + nested-dot paths             |
| `$gt` `$gte` `$lt` `$lte` | yes | heterogeneous types compared safely      |
| `$in` `$nin`  | yes       |                                                  |
| `$and` `$or` `$nor` `$not` | yes |                                              |
| `$regex` `$options` | yes | `i`, `s`, `m` flags                            |
| `$exists`     | yes       |                                                  |
| `$size` `$all` `$elemMatch` | yes |                                              |
| `$type`       | yes       | numeric codes + names (`"string"`, ...)          |
| `$mod`        | yes       |                                                  |
| `$expr`       | partial   | literals, field refs, `$eq/$ne/$gt/.../$add/...` |
| `$where`      | no        | (security; returns no rows)                      |
| `$text` `$geoWithin` `$near` | no | not modelled                              |

## Aggregation pipeline coverage

| stage          | supported | notes                                                                   |
|----------------|-----------|-------------------------------------------------------------------------|
| `$match`       | yes       | same operator support as `find`                                         |
| `$group`       | yes       | accumulators `$sum`, `$avg`, `$min`, `$max`, `$first`, `$last`, `$push`, `$addToSet`, `$count` |
| `$sort`        | yes       |                                                                         |
| `$limit` `$skip` | yes     |                                                                         |
| `$project`     | yes       | inclusion + exclusion + `_id` toggle                                    |
| `$count`       | yes       | `{ $count: "<field>" }`                                                 |
| `$unwind`      | yes       | string or `{path, preserveNullAndEmptyArrays}`                          |
| `$addFields` / `$set` | yes |                                                                         |
| `$unset`       | yes       |                                                                         |
| `$replaceRoot` | partial   | constant or field-ref `newRoot`                                         |
| `$lookup`      | no        | skipped (cross-collection joins not modelled)                           |
| `$facet` `$bucket` `$graphLookup` `$merge` `$out` | no | skipped                                            |

## Update operators

`$set`, `$unset`, `$inc`, `$mul`, `$min`, `$max`, `$rename`, `$push`
(with `$each`), `$addToSet`, `$pull`, `$pop`, `$currentDate`. An
update payload with no `$`-prefixed key is treated as a **replacement
document** (preserves `_id`).

## State

State lives in `$MONGO_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/mongodb/state.json` inside the container;
`~/.openclaw/mongo_mock/state.json` outside). The file holds:

```jsonc
{
  "databases": {
    "<dbname>": {
      "collections": {
        "<collname>": {
          "documents": [ {"_id": "<24-hex>", ...}, ... ],
          "indexes":   [ {"name": "_id_", "key": {"_id": 1}, "unique": true} ]
        }
      }
    }
  },
  "calls": [ {"op": "find", "ts": "...", ...}, ... ]
}
```

Auto-generated `_id`s are 24-hex `secrets.token_hex(12)` strings,
matching ObjectId's printable form. File locking via `fcntl.flock`
makes concurrent calls safe; per-rollout isolation should reset the
state dir between rollouts.

Every tool (mutating or read) appends an entry to `calls`. The
verifier consumes this log.

## Seed loader

Set `MONGO_MOCK_SEED_PATH` to populate the initial state. Accepted
formats:

1. **State-shape JSON** -- a file with a top-level `"databases"` key,
   identical to what `state.json` looks like. Loaded verbatim.
2. **Nested JSON map** -- `{"<db>": {"<coll>": [<doc>, ...], ...}, ...}`.
   Each doc gets an auto `_id` if missing.
3. **`mongodump` directory** -- a directory laid out as
   `<root>/<db>/<coll>.bson` (or `<coll>.json` with a JSON array).
   `<coll>.metadata.json` files are ignored. URL-encoded collection
   names (`Purchase+History.bson`, `Purchase%20History.bson`) are
   decoded back to `Purchase History`. BSON decoding requires the
   `pymongo` package (declared in `pyproject.toml`).

The atlas eval ships
`data_exports/mongo_dump_video_game_store-UNZIP-FIRST.zip` -- unzip
it and point `MONGO_MOCK_SEED_PATH` at the resulting
`mongo_dump_video_game_store/` directory.

## Run

```bash
# local
MONGO_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  mongodb-mock:
    build:
      context: ../../mcp_servers/mongodb-mock
      dockerfile: Dockerfile
    image: mcp-env/mongodb-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      MONGO_MOCK_STATE_DIR: /workspace/output/end_state/mongodb
      MONGO_MOCK_SEED_PATH: /workspace/input/mongo_seed
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```

## Known limitations / things to flag

- BSON-only types (Decimal128, true ObjectId instances, BinData) are
  collapsed to plain JSON scalars at seed time. Equality comparisons
  on them therefore work on string form. If a task relies on driver-
  level type fidelity, the mock will mismatch.
- `$lookup`, `$graphLookup`, `$facet`, `$merge`, `$out`, `$bucket*`
  aggregation stages are not implemented (silently skipped).
- `explain` returns a synthetic queryPlanner stub keyed only on
  whether any index covers the filter; statistics like `executionTime`
  are fabricated. The verifier should not depend on real values.
- The mock has no notion of authentication, replica sets, transactions,
  change streams, or write concerns.
