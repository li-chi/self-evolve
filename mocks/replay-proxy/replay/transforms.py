"""Response-shape transforms for tool aliasing.

When a cassette records a response under a canonical tool (e.g.
maps_geocode) but the agent calls an equivalent alias tool (e.g.
maps_search_places), the response shape must be reshaped from the
canonical form back to what the alias-tool's schema promises.

Each transform takes (canonical_response, agent_args) and returns the
reshaped response. Register by name in TRANSFORMS so configs can refer
to them by string.
"""

from __future__ import annotations

import json
from typing import Any, Callable


def geocode_to_search_places(resp: Any, agent_args: dict) -> Any:
    """maps_geocode response → maps_search_places response.

    Input  (geocode):       {location, formatted_address, place_id, ...}
    Output (search_places): {places: [{name, formatted_address,
                                       location, place_id, ...}]}
    Derives `name` from the agent's `query` (text before first comma).
    """
    if not isinstance(resp, dict):
        return resp
    query = (agent_args or {}).get('query') or ''
    name = query.split(',', 1)[0].strip() if ',' in query else query.strip()
    place = dict(resp)
    place['name'] = name or place.get('formatted_address', '')
    return {'places': [place]}


def fetch_json_to_text(resp: Any, agent_args: dict) -> Any:
    """fetch_json response → fetch_txt response (serialized JSON as text)."""
    if isinstance(resp, str):
        return resp
    return json.dumps(resp, indent=2, ensure_ascii=False)


def fetch_json_to_markdown(resp: Any, agent_args: dict) -> Any:
    """fetch_json response → fetch_markdown response (fenced JSON block)."""
    if isinstance(resp, str):
        return resp
    return "```json\n" + json.dumps(resp, indent=2, ensure_ascii=False) + "\n```"


# Name → function registry. Configs refer to transforms by name string.
TRANSFORMS: dict[str, Callable[[Any, dict], Any]] = {
    'geocode_to_search_places': geocode_to_search_places,
    'fetch_json_to_text': fetch_json_to_text,
    'fetch_json_to_markdown': fetch_json_to_markdown,
}
