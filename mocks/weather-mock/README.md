# weather-mock

Deterministic mock of the common `weather` MCP tool surface (the Model Context
Protocol weather quickstart / NWS + Open-Meteo family; atlas names `weather` /
`weather-data`).

Serves point forecasts and US state alerts from a seeded `state.json` — no
NOAA/Open-Meteo calls. Point lookups match the seeded (lat, lon) rounded to 2
decimals (with a small nearest-point tolerance).

## Tools
`get_current_weather(latitude, longitude)`,
`get_forecast(latitude, longitude, days=7)`, `get_alerts(state)`.

## State
`$WEATHER_MOCK_STATE_DIR/state.json`, seeded from `$WEATHER_MOCK_SEED_PATH`.
Build seeds with `synth/mock_seed/weather.py`. Calls append to `state["calls"]`.
