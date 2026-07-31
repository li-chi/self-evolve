# elevenlabs-mock

Mock MCP server that mirrors the public ElevenLabs REST API
(<https://elevenlabs.io/docs/api-reference/>). Tools are named after
their REST operations and accept/return the same field shapes the
real ElevenLabs API uses, with binary audio replaced by
base64-encoded fake bytes so the response stays JSON-safe.

This server is a **drop-in stand-in for the real ElevenLabs API
during RL rollouts** — it does **not** wrap the `elevenlabs-api` CLI
mock. All state lives in a single JSON file guarded by `fcntl.flock`.

## State

- Default location: `~/.openclaw/elevenlabs_mock/state.json`
- Override with: `ELEVENLABS_MOCK_STATE_DIR=/some/dir`
- Optional one-shot seed: `ELEVENLABS_MOCK_SEED_PATH=/path/to/state.json`
  (loaded only when no state file exists yet — per-rollout isolation
  should clear the state dir between rollouts.)

A fresh state pre-populates the standard ElevenLabs premade voices
(Rachel, Domi, Bella, Antoni, Elli, Josh, Arnold, Adam, Sam) and the
default model catalog (`eleven_multilingual_v2`, `eleven_turbo_v2_5`,
`eleven_turbo_v2`, `eleven_monolingual_v1`, `eleven_multilingual_v1`,
`scribe_v1`, `eleven_english_sts_v2`).

## Tool surface

| Tool                              | REST operation                                                |
|-----------------------------------|---------------------------------------------------------------|
| `text_to_speech`                  | `POST /v1/text-to-speech/{voice_id}`                          |
| `text_to_speech_stream`           | `POST /v1/text-to-speech/{voice_id}/stream`                   |
| `speech_to_speech`                | `POST /v1/speech-to-speech/{voice_id}`                        |
| `speech_to_text`                  | `POST /v1/speech-to-text` (Scribe)                            |
| `list_voices`                     | `GET /v1/voices`                                              |
| `get_voice`                       | `GET /v1/voices/{voice_id}`                                   |
| `delete_voice`                    | `DELETE /v1/voices/{voice_id}`                                |
| `edit_voice_settings`             | `POST /v1/voices/{voice_id}/settings/edit`                    |
| `add_voice`                       | `POST /v1/voices/add` (instant voice cloning)                 |
| `list_models`                     | `GET /v1/models`                                              |
| `get_user`                        | `GET /v1/user`                                                |
| `get_user_subscription`           | `GET /v1/user/subscription`                                   |
| `list_history_items`              | `GET /v1/history`                                             |
| `get_history_item`                | `GET /v1/history/{history_item_id}`                           |
| `delete_history_item`             | `DELETE /v1/history/{history_item_id}`                        |
| `list_pronunciation_dictionaries` | `GET /v1/pronunciation-dictionaries`                          |
| `get_pronunciation_dictionary`    | `GET /v1/pronunciation-dictionaries/{pronunciation_dictionary_id}` |

Mock-only debug surface (not exposed by the real ElevenLabs API):

- `mock_debug_state` — dump the full persisted state.
- `mock_debug_seed(user?, voices?, models?, history?,
  dictionaries?, replace=False)` — directly inject fixtures into
  state. `replace=True` first resets to the default catalog.

## Response shapes

Successful tools return the bare ElevenLabs JSON object (no envelope).
For example, `get_voice` returns the full voice dict with
`voice_id`, `name`, `samples`, `category` (`premade` / `cloned` /
`generated`), `fine_tuning`, `labels`, `description`, `preview_url`,
`available_for_tiers`, and `settings`.

Errors are returned (not raised) in ElevenLabs' shape:

```json
{"detail": {"status": "voice_not_found",
            "message": "A voice for the voice_id XYZ could not be found."}}
```

Audio responses (`text_to_speech`, `text_to_speech_stream`,
`speech_to_speech`) replace the binary audio body with:

```json
{
  "history_item_id": "...",
  "voice_id": "...",
  "model_id": "...",
  "character_count": 42,
  "output_format": "mp3_44100_128",
  "audio_format": "mp3",
  "sample_rate": 44100,
  "audio_base64": "<deterministic fake bytes, base64>"
}
```

`text_to_speech_stream` additionally returns a `chunks` array of
base64 fragments so the rollout can verify streaming-shape responses.

## IDs

ElevenLabs uses 20-char alphanumeric ids (e.g.
`21m00Tcm4TlvDq8ikWAM`). Generated voice/history/dictionary ids
follow that format; model ids follow ElevenLabs' string convention
(e.g. `eleven_multilingual_v2`, `scribe_v1`).

## Quota model

`text_to_speech` and `text_to_speech_stream` charge characters
against the subscription's `character_limit`. `speech_to_speech` and
`speech_to_text` do not charge characters (matching the real API).
Exceeding the quota returns `{"detail": {"status": "quota_exceeded", ...}}`.

## Call trace

Every tool call (including reads) appends to `state["calls"]` with
`{"op": "<tool>", "ts": "<iso8601>", ...}`. The verifier can replay
this trace to assert which operations the agent invoked.
