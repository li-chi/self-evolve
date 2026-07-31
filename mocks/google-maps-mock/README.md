# google-maps-mock

Deterministic mock of [`@modelcontextprotocol/server-google-maps`](https://github.com/modelcontextprotocol/servers-archived/tree/main/src/google-maps)
(atlas names `google-maps` / `google_map`).

Serves the upstream tool surface from a seeded `state.json` — no Google Maps
Platform key. Geocoding/place lookups hit seeded places; distance and
directions use a seeded leg when present, else a deterministic great-circle
estimate (per-mode speed) between two geocodable endpoints.

## Tools
`maps_geocode`, `maps_reverse_geocode`, `maps_place_details`,
`maps_distance_matrix`, `maps_elevation`, `maps_directions` — exact lookups by a
supplied address / place_id / coordinate, the deterministic default surface.

`maps_search_places` is **gated OFF by default** (free-text ranking). Set
`GOOGLE_MAPS_MOCK_ENABLE_SEARCH=1` to expose it.

## State
`$GOOGLE_MAPS_MOCK_STATE_DIR/state.json`, seeded from
`$GOOGLE_MAPS_MOCK_SEED_PATH`. Build seeds with
`synth/mock_seed/google_maps.py`. Calls append to `state["calls"]`.
