"""ElevenLabs mock MCP server.

Mirrors the public ElevenLabs REST API
(https://elevenlabs.io/docs/api-reference/). Each tool is named after
its REST operation and accepts/returns the same field shapes (with
binary audio replaced by base64-encoded fake bytes so the response is
JSON-safe).

Backed by a single JSON state file (default
`$ELEVENLABS_MOCK_STATE_DIR/state.json`, falling back to
`~/.openclaw/elevenlabs_mock`). Reads and writes are guarded by an
fcntl flock so concurrent tool calls are safe.

Responses follow ElevenLabs conventions:

  Success:  the bare JSON object (no envelope), e.g. for a voice it
            includes `voice_id`, `name`, `samples`, `category`,
            `fine_tuning`, `labels`, `description`, `preview_url`,
            `available_for_tiers`, `settings`.

  Error:    `{"detail": {"status": "<error_code>", "message": "..."}}`
            returned as a Python dict (not raised) so the trace looks
            like the real HTTP error body.

  Audio:    The real API returns binary audio. The mock returns the
            same response object plus a base64 `audio_base64` field
            and `audio_format` / `sample_rate` metadata.

Tools cover the operations listed in the manifest scope:

  TTS:                   text_to_speech, text_to_speech_stream
  Voice conversion:      speech_to_speech
  Speech-to-text:        speech_to_text  (Scribe)
  Voices CRUD:           list_voices, get_voice, delete_voice,
                         edit_voice_settings, add_voice
  Models:                list_models
  User / subscription:   get_user, get_user_subscription
  History:               list_history_items, get_history_item,
                         delete_history_item
  Pronunciation dicts:   list_pronunciation_dictionaries,
                         get_pronunciation_dictionary

Mock-only helpers (not part of the real surface):
  mock_debug_state, mock_debug_seed

Every call (including reads) appends to `state["calls"]` so the
verifier can replay the trace.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import random
import string

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "ELEVENLABS_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/elevenlabs_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _now_unix() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp())


# ElevenLabs voice/model/history ids are 20-char alphanumeric strings
# (e.g. "21m00Tcm4TlvDq8ikWAM"). Generate matching ids deterministically
# from a seed so concurrent calls under flock stay stable.
_ID_ALPHABET = string.ascii_letters + string.digits


def _new_id(seed: str) -> str:
    rng = random.Random(seed)
    return "".join(rng.choices(_ID_ALPHABET, k=20))


def _new_id_unique(state: dict, kind: str) -> str:
    counter = state["next_id"].get(kind, 0) + 1
    state["next_id"][kind] = counter
    base = f"{kind}-{counter}-{_now_unix()}-{os.getpid()}"
    return _new_id(base)


def _empty_state() -> dict:
    """Seed the state with the standard premade voices and the
    default model catalog so a fresh run is usable out of the box."""
    user_id = "mockuser0000000000000000000000ab"  # 32-char hex
    return {
        "user": {
            "user_id": user_id,
            "first_name": "Mock",
            "is_new_user": False,
            "xi_api_key": "sk_mock_0000000000000000000000000000",
            "can_use_delayed_payment_methods": False,
            "is_onboarding_completed": True,
            "is_onboarding_checklist_completed": True,
            "subscription": {
                "tier": "creator",
                "character_count": 0,
                "character_limit": 100_000,
                "max_character_limit_extension": 0,
                "can_extend_character_limit": False,
                "allowed_to_extend_character_limit": False,
                "next_character_count_reset_unix": _now_unix() + 30 * 86400,
                "voice_slots_used": 0,
                "professional_voice_slots_used": 0,
                "voice_limit": 30,
                "max_voice_add_edits": 5,
                "voice_add_edit_counter": 0,
                "professional_voice_limit": 1,
                "can_extend_voice_limit": False,
                "can_use_instant_voice_cloning": True,
                "can_use_professional_voice_cloning": False,
                "currency": "usd",
                "status": "active",
                "billing_period": "monthly_period",
                "character_refresh_period": "monthly_period",
            },
        },
        "voices": {},
        "models": {},
        "history": {},        # history_item_id -> item dict
        "dictionaries": {},   # pronunciation_dictionary_id -> dict
        "next_id": {
            "voice": 0, "history": 0, "dict": 0, "sample": 0,
            "model": 0,
        },
        "calls": [],
    }


def _seed_default_catalog(state: dict) -> None:
    """Populate `voices` and `models` with realistic-looking defaults
    if they are empty. Called whenever an empty state is created."""
    if not state["voices"]:
        defaults = [
            ("21m00Tcm4TlvDq8ikWAM", "Rachel", "premade",
             {"accent": "american", "description": "calm",
              "age": "young", "gender": "female",
              "use_case": "narration"}),
            ("AZnzlk1XvdvUeBnXmlld", "Domi", "premade",
             {"accent": "american", "description": "strong",
              "age": "young", "gender": "female",
              "use_case": "narration"}),
            ("EXAVITQu4vr4xnSDxMaL", "Bella", "premade",
             {"accent": "american", "description": "soft",
              "age": "young", "gender": "female",
              "use_case": "narration"}),
            ("ErXwobaYiN019PkySvjV", "Antoni", "premade",
             {"accent": "american", "description": "well-rounded",
              "age": "young", "gender": "male",
              "use_case": "narration"}),
            ("MF3mGyEYCl7XYWbV9V6O", "Elli", "premade",
             {"accent": "american", "description": "emotional",
              "age": "young", "gender": "female",
              "use_case": "narration"}),
            ("TxGEqnHWrfWFTfGW9XjX", "Josh", "premade",
             {"accent": "american", "description": "deep",
              "age": "young", "gender": "male",
              "use_case": "narration"}),
            ("VR6AewLTigWG4xSOukaG", "Arnold", "premade",
             {"accent": "american", "description": "crisp",
              "age": "middle_aged", "gender": "male",
              "use_case": "narration"}),
            ("pNInz6obpgDQGcFmaJgB", "Adam", "premade",
             {"accent": "american", "description": "deep",
              "age": "middle_aged", "gender": "male",
              "use_case": "narration"}),
            ("yoZ06aMxZJJ28mfd3POQ", "Sam", "premade",
             {"accent": "american", "description": "raspy",
              "age": "young", "gender": "male",
              "use_case": "narration"}),
        ]
        for vid, name, category, labels in defaults:
            state["voices"][vid] = _make_voice(
                voice_id=vid, name=name, category=category,
                labels=labels,
                description=f"{labels.get('description','')} voice",
                preview_url=(f"https://storage.googleapis.com/eleven-"
                             f"public-prod/premade/voices/{vid}/preview.mp3"),
            )
    if not state["models"]:
        state["models"] = {
            "eleven_multilingual_v2": _make_model(
                "eleven_multilingual_v2", "Eleven Multilingual v2",
                ["en", "ja", "zh", "de", "hi", "fr", "ko", "pt",
                 "it", "es", "id", "nl", "tr", "fil", "pl", "sv",
                 "bg", "ro", "ar", "cs", "el", "fi", "hr", "ms",
                 "sk", "da", "ta", "uk", "ru"],
                can_finetune=False, tts=True, conversion=True,
                serves_pro=True, token_cost=1.0,
                max_chars=5000,
                description=("Our cutting-edge multilingual speech "
                             "synthesis model, designed for high "
                             "quality."),
            ),
            "eleven_turbo_v2_5": _make_model(
                "eleven_turbo_v2_5", "Eleven Turbo v2.5",
                ["en", "ja", "zh", "de", "hi", "fr", "ko", "pt",
                 "it", "es", "id", "nl", "tr", "fil", "pl", "sv",
                 "bg", "ro", "ar", "cs", "el", "fi", "hr", "ms",
                 "sk", "da", "ta", "uk", "ru"],
                can_finetune=False, tts=True, conversion=False,
                serves_pro=False, token_cost=0.5,
                max_chars=40_000,
                description=("Our latest low-latency turbo model, "
                             "ideal for conversational use cases."),
            ),
            "eleven_turbo_v2": _make_model(
                "eleven_turbo_v2", "Eleven Turbo v2",
                ["en"],
                can_finetune=False, tts=True, conversion=False,
                serves_pro=False, token_cost=0.5,
                max_chars=30_000,
                description=("Our English-only low-latency turbo "
                             "model."),
            ),
            "eleven_monolingual_v1": _make_model(
                "eleven_monolingual_v1", "Eleven Monolingual v1",
                ["en"],
                can_finetune=True, tts=True, conversion=True,
                serves_pro=False, token_cost=1.0,
                max_chars=5000,
                description=("Our first generation English-only "
                             "speech synthesis model."),
            ),
            "eleven_multilingual_v1": _make_model(
                "eleven_multilingual_v1", "Eleven Multilingual v1",
                ["en", "de", "pl", "es", "it", "fr", "pt", "hi"],
                can_finetune=False, tts=True, conversion=True,
                serves_pro=False, token_cost=1.0,
                max_chars=5000,
                description=("Our first multilingual generation "
                             "speech synthesis model."),
            ),
            "scribe_v1": _make_model(
                "scribe_v1", "Scribe v1",
                ["en", "es", "fr", "de", "it", "pt", "nl", "ja",
                 "zh", "ko"],
                can_finetune=False, tts=False, conversion=False,
                serves_pro=False, token_cost=0.0,
                max_chars=0,
                description=("Speech-to-text model with diarization "
                             "support."),
            ),
            "eleven_english_sts_v2": _make_model(
                "eleven_english_sts_v2", "Eleven English STS v2",
                ["en"],
                can_finetune=False, tts=False, conversion=True,
                serves_pro=False, token_cost=1.0,
                max_chars=0,
                description=("English speech-to-speech voice "
                             "conversion model."),
            ),
        }


def _make_voice(*, voice_id: str, name: str,
                category: str = "premade",
                labels: dict | None = None,
                description: str = "",
                preview_url: str = "",
                samples: list | None = None,
                fine_tuning: dict | None = None,
                settings: dict | None = None,
                available_for_tiers: list | None = None,
                sharing: dict | None = None,
                high_quality_base_model_ids: list | None = None) -> dict:
    return {
        "voice_id": voice_id,
        "name": name,
        "samples": samples or [],
        "category": category,
        "fine_tuning": fine_tuning or {
            "is_allowed_to_fine_tune": False,
            "state": {},
            "verification_failures": [],
            "verification_attempts_count": 0,
            "manual_verification_requested": False,
            "language": None,
            "progress": {},
            "message": {},
            "dataset_duration_seconds": None,
            "verification_attempts": None,
            "slice_ids": None,
            "manual_verification": None,
        },
        "labels": labels or {},
        "description": description,
        "preview_url": preview_url,
        "available_for_tiers": available_for_tiers or [],
        "settings": settings or {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        },
        "sharing": sharing,
        "high_quality_base_model_ids": high_quality_base_model_ids or [],
        "safety_control": None,
        "voice_verification": {
            "requires_verification": False,
            "is_verified": True,
            "verification_failures": [],
            "verification_attempts_count": 0,
            "language": None,
            "verification_attempts": None,
        },
        "owner_id": None,
        "permission_on_resource": None,
    }


def _make_model(model_id: str, name: str, languages: list,
                *, can_finetune: bool, tts: bool, conversion: bool,
                serves_pro: bool, token_cost: float,
                max_chars: int, description: str) -> dict:
    return {
        "model_id": model_id,
        "name": name,
        "can_be_finetuned": can_finetune,
        "can_do_text_to_speech": tts,
        "can_do_voice_conversion": conversion,
        "can_use_style": True,
        "can_use_speaker_boost": True,
        "serves_pro_voices": serves_pro,
        "token_cost_factor": token_cost,
        "description": description,
        "requires_alpha_access": False,
        "max_characters_request_free_user": min(max_chars, 500)
        if max_chars else 0,
        "max_characters_request_subscribed_user": max_chars,
        "maximum_text_length_per_request": max_chars,
        "languages": [{"language_id": lid,
                       "name": _LANG_NAMES.get(lid, lid)}
                      for lid in languages],
        "model_rates": {"character_cost_multiplier": token_cost},
        "concurrency_group": "standard",
    }


_LANG_NAMES = {
    "en": "English", "ja": "Japanese", "zh": "Chinese",
    "de": "German", "hi": "Hindi", "fr": "French",
    "ko": "Korean", "pt": "Portuguese", "it": "Italian",
    "es": "Spanish", "id": "Indonesian", "nl": "Dutch",
    "tr": "Turkish", "fil": "Filipino", "pl": "Polish",
    "sv": "Swedish", "bg": "Bulgarian", "ro": "Romanian",
    "ar": "Arabic", "cs": "Czech", "el": "Greek",
    "fi": "Finnish", "hr": "Croatian", "ms": "Malay",
    "sk": "Slovak", "da": "Danish", "ta": "Tamil",
    "uk": "Ukrainian", "ru": "Russian",
}


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("ELEVENLABS_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                state = json.load(f)
        else:
            state = _empty_state()
            _seed_default_catalog(state)
        return state
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    # Pre-seed state.json files may legitimately ship with empty
    # `models` (the workflow only cares about voices/history). Backfill
    # the default model catalog so text_to_speech can resolve the
    # default model when callers omit model_id.
    if not state.get("models"):
        _seed_default_catalog(state)
    return state


def _save_state(state: dict) -> None:
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


@contextlib.contextmanager
def _lock():
    lock_path = _state_path() + ".lock"
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


def _record(state: dict, op: str, **kwargs) -> None:
    entry = {"op": op, "ts": _now_iso()}
    entry.update(kwargs)
    state["calls"].append(entry)


def _err(status: str, message: str) -> dict:
    """ElevenLabs-shaped error: {"detail": {"status": ..., "message": ...}}."""
    return {"detail": {"status": status, "message": message}}


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

# Supported output formats follow ElevenLabs' real list.
_VALID_OUTPUT_FORMATS = {
    "mp3_22050_32", "mp3_44100_32", "mp3_44100_64",
    "mp3_44100_96", "mp3_44100_128", "mp3_44100_192",
    "pcm_8000", "pcm_16000", "pcm_22050", "pcm_24000",
    "pcm_44100", "ulaw_8000",
}


def _format_meta(fmt: str) -> tuple[str, int]:
    """Returns (codec, sample_rate_hz) for a format string."""
    if fmt.startswith("mp3_"):
        parts = fmt.split("_")
        # mp3_<sr>_<bitrate>
        return ("mp3", int(parts[1]) if len(parts) > 1 else 44100)
    if fmt.startswith("pcm_"):
        return ("pcm", int(fmt.split("_")[1]))
    if fmt.startswith("ulaw_"):
        return ("ulaw", int(fmt.split("_")[1]))
    return ("mp3", 44100)


def _fake_audio(seed: str, length_hint: int = 64) -> str:
    """Return deterministic base64 bytes that stand in for binary audio.
    Real audio data is replaced so the response stays JSON-safe."""
    h = hashlib.sha256(seed.encode("utf-8")).digest()
    payload = (h * ((length_hint // len(h)) + 1))[:max(16, length_hint)]
    return base64.b64encode(payload).decode("ascii")


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("elevenlabs-mock")


# ---------------------------------------------------------------------------
# Voices
# ---------------------------------------------------------------------------

@mcp.tool(name="list_voices")
def list_voices(show_legacy: bool = False,
                category: str = "",
                search: str = "",
                page_size: int = 30) -> dict:
    """ElevenLabs REST: GET /v1/voices — list the caller's voices
    (premade + cloned + generated). Returns `{"voices": [...]}`."""
    with _lock():
        s = _load_state()
        out = []
        q = (search or "").lower().strip()
        for v in s["voices"].values():
            if category and v.get("category") != category:
                continue
            if q:
                hay = " ".join([
                    v.get("name", ""),
                    v.get("description", ""),
                    " ".join(f"{k}:{val}" for k, val
                             in (v.get("labels") or {}).items()),
                ]).lower()
                if q not in hay:
                    continue
            out.append(v)
        if page_size and page_size > 0:
            out = out[:page_size]
        _record(s, "list_voices", count=len(out), category=category,
                search=search)
        _save_state(s)
        return {"voices": out, "has_more": False, "total_count": len(out),
                "next_page_token": None}


@mcp.tool(name="get_voice")
def get_voice(voice_id: str, with_settings: bool = True) -> dict:
    """ElevenLabs REST: GET /v1/voices/{voice_id} — retrieve a single
    voice. Returns the full voice object. Errors with
    `voice_not_found` if the id is unknown."""
    with _lock():
        s = _load_state()
        v = s["voices"].get(voice_id)
        _record(s, "get_voice", voice_id=voice_id,
                result="ok" if v else "voice_not_found")
        _save_state(s)
        if not v:
            return _err("voice_not_found",
                        f"A voice for the voice_id {voice_id} "
                        f"could not be found.")
        out = dict(v)
        if not with_settings:
            out.pop("settings", None)
        return out


@mcp.tool(name="delete_voice")
def delete_voice(voice_id: str) -> dict:
    """ElevenLabs REST: DELETE /v1/voices/{voice_id} — delete a cloned
    voice. Premade voices cannot be deleted."""
    with _lock():
        s = _load_state()
        v = s["voices"].get(voice_id)
        if not v:
            _record(s, "delete_voice", voice_id=voice_id,
                    result="voice_not_found")
            _save_state(s)
            return _err("voice_not_found",
                        f"A voice for the voice_id {voice_id} "
                        f"could not be found.")
        if v.get("category") == "premade":
            _record(s, "delete_voice", voice_id=voice_id,
                    result="cannot_delete_premade")
            _save_state(s)
            return _err("cannot_delete_premade_voice",
                        "Premade voices cannot be deleted.")
        del s["voices"][voice_id]
        # Decrement slot usage if it was a cloned voice.
        sub = s["user"]["subscription"]
        sub["voice_slots_used"] = max(0, sub.get("voice_slots_used", 1) - 1)
        _record(s, "delete_voice", voice_id=voice_id)
        _save_state(s)
        return {"status": "ok"}


@mcp.tool(name="edit_voice_settings")
def edit_voice_settings(voice_id: str,
                        stability: float | None = None,
                        similarity_boost: float | None = None,
                        style: float | None = None,
                        use_speaker_boost: bool | None = None) -> dict:
    """ElevenLabs REST: POST /v1/voices/{voice_id}/settings/edit —
    update voice generation settings. Each field is optional; only
    provided fields are updated."""
    with _lock():
        s = _load_state()
        v = s["voices"].get(voice_id)
        if not v:
            _record(s, "edit_voice_settings", voice_id=voice_id,
                    result="voice_not_found")
            _save_state(s)
            return _err("voice_not_found",
                        f"A voice for the voice_id {voice_id} "
                        f"could not be found.")
        settings = v.setdefault("settings", {
            "stability": 0.5, "similarity_boost": 0.75,
            "style": 0.0, "use_speaker_boost": True,
        })
        if stability is not None:
            if not 0.0 <= stability <= 1.0:
                return _err("invalid_voice_settings",
                            "stability must be between 0 and 1")
            settings["stability"] = float(stability)
        if similarity_boost is not None:
            if not 0.0 <= similarity_boost <= 1.0:
                return _err("invalid_voice_settings",
                            "similarity_boost must be between 0 and 1")
            settings["similarity_boost"] = float(similarity_boost)
        if style is not None:
            if not 0.0 <= style <= 1.0:
                return _err("invalid_voice_settings",
                            "style must be between 0 and 1")
            settings["style"] = float(style)
        if use_speaker_boost is not None:
            settings["use_speaker_boost"] = bool(use_speaker_boost)
        _record(s, "edit_voice_settings", voice_id=voice_id,
                settings=settings)
        _save_state(s)
        return {"status": "ok"}


@mcp.tool(name="add_voice")
def add_voice(name: str,
              description: str = "",
              labels: dict | None = None,
              files: list | None = None,
              remove_background_noise: bool = False) -> dict:
    """ElevenLabs REST: POST /v1/voices/add — instant voice cloning
    from one or more sample files.

    `files` is a list of `{name, content_base64, mime_type?}` (the real
    API takes multipart file uploads; the mock takes the same payload
    in JSON). At least one file is required and the bot's plan must
    allow instant voice cloning."""
    with _lock():
        s = _load_state()
        sub = s["user"]["subscription"]
        if not sub.get("can_use_instant_voice_cloning", True):
            _record(s, "add_voice", name=name,
                    result="instant_voice_cloning_not_available")
            _save_state(s)
            return _err("instant_voice_cloning_not_available",
                        "Your plan does not allow instant voice "
                        "cloning.")
        if not name:
            return _err("invalid_request", "name is required")
        if not files:
            return _err("invalid_request",
                        "at least one sample file is required")
        if sub.get("voice_slots_used", 0) >= sub.get("voice_limit", 30):
            _record(s, "add_voice", name=name, result="voice_limit_reached")
            _save_state(s)
            return _err("voice_limit_reached",
                        "You have reached your voice limit for this "
                        "subscription tier.")
        vid = _new_id_unique(s, "voice")
        samples = []
        for f in files:
            if not isinstance(f, dict):
                continue
            sample_id = _new_id_unique(s, "sample")
            content = f.get("content_base64", "")
            try:
                size = len(base64.b64decode(content)) if content else 0
            except Exception:
                size = 0
            samples.append({
                "sample_id": sample_id,
                "file_name": f.get("name", f"sample_{sample_id}.mp3"),
                "mime_type": f.get("mime_type", "audio/mpeg"),
                "size_bytes": size,
                "hash": hashlib.sha256(content.encode("utf-8")).hexdigest()
                if content else "",
                "duration_secs": max(1.0, size / 16_000.0),
                "remove_background_noise": bool(remove_background_noise),
            })
        voice = _make_voice(
            voice_id=vid, name=name, category="cloned",
            labels=labels or {}, description=description,
            samples=samples,
        )
        s["voices"][vid] = voice
        sub["voice_slots_used"] = sub.get("voice_slots_used", 0) + 1
        sub["voice_add_edit_counter"] = sub.get(
            "voice_add_edit_counter", 0) + 1
        _record(s, "add_voice", voice_id=vid, name=name,
                sample_count=len(samples))
        _save_state(s)
        return {"voice_id": vid, "requires_verification": False}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@mcp.tool(name="list_models")
def list_models() -> list:
    """ElevenLabs REST: GET /v1/models — list available models with
    capability flags (`can_do_text_to_speech`,
    `can_do_voice_conversion`, ...). Returns a JSON array."""
    with _lock():
        s = _load_state()
        out = list(s["models"].values())
        _record(s, "list_models", count=len(out))
        _save_state(s)
        return out


# ---------------------------------------------------------------------------
# Text-to-Speech
# ---------------------------------------------------------------------------

def _resolve_model(state: dict, model_id: str | None) -> dict | None:
    if not model_id:
        return state["models"].get("eleven_multilingual_v2")
    return state["models"].get(model_id)


def _tts_common(state: dict, *, voice_id: str, text: str,
                model_id: str | None, voice_settings: dict | None,
                output_format: str, source: str,
                language_code: str | None,
                pronunciation_dictionary_locators: list | None,
                seed: int | None,
                previous_text: str | None,
                next_text: str | None) -> dict:
    """Shared core for text_to_speech and text_to_speech_stream.
    Returns either an error dict or the success body (without the
    audio payload, which the wrappers attach so streaming can vary
    chunking)."""
    voice = state["voices"].get(voice_id)
    if not voice:
        return _err("voice_not_found",
                    f"A voice for the voice_id {voice_id} could "
                    f"not be found.")
    model = _resolve_model(state, model_id)
    if model is None:
        return _err("model_not_found",
                    f"Could not find model with model_id "
                    f"{model_id}.")
    if not model.get("can_do_text_to_speech"):
        return _err("invalid_model",
                    f"Model {model['model_id']} does not support "
                    f"text-to-speech.")
    if output_format and output_format not in _VALID_OUTPUT_FORMATS:
        return _err("invalid_output_format",
                    f"Unsupported output_format: {output_format}. "
                    f"Valid values: {sorted(_VALID_OUTPUT_FORMATS)}.")
    if not text:
        return _err("invalid_request",
                    "text is required and must be non-empty")
    cap = model.get("maximum_text_length_per_request",
                    model.get("max_characters_request_subscribed_user",
                              5000))
    if cap and len(text) > cap:
        return _err("text_too_long",
                    f"text length {len(text)} exceeds model "
                    f"{model['model_id']} cap of {cap}")
    sub = state["user"]["subscription"]
    new_count = sub.get("character_count", 0) + len(text)
    if new_count > sub.get("character_limit", 100_000):
        return _err("quota_exceeded",
                    "Character quota exceeded for current "
                    "subscription period.")
    sub["character_count"] = new_count
    hid = _new_id_unique(state, "history")
    history_item = {
        "history_item_id": hid,
        "request_id": _new_id_unique(state, "history"),
        "voice_id": voice_id,
        "voice_name": voice.get("name", ""),
        "voice_category": voice.get("category", "premade"),
        "model_id": model["model_id"],
        "text": text,
        "date_unix": _now_unix(),
        "character_count_change_from": new_count - len(text),
        "character_count_change_to": new_count,
        "content_type": ("audio/mpeg" if output_format.startswith("mp3")
                         else f"audio/{output_format.split('_')[0]}"),
        "state": "created",
        "settings": voice.get("settings", {}),
        "feedback": None,
        "share_link_id": None,
        "source": source,
        "alignments": None,
        "dialogue": None,
    }
    if language_code:
        history_item["language_code"] = language_code
    if voice_settings:
        history_item["settings"] = {**history_item["settings"],
                                    **voice_settings}
    state["history"][hid] = history_item
    return {
        "history_item_id": hid,
        "voice_id": voice_id,
        "model_id": model["model_id"],
        "character_count": len(text),
        "output_format": output_format,
    }


@mcp.tool(name="text_to_speech")
def text_to_speech(voice_id: str,
                   text: str,
                   model_id: str = "eleven_multilingual_v2",
                   voice_settings: dict | None = None,
                   output_format: str = "mp3_44100_128",
                   language_code: str | None = None,
                   pronunciation_dictionary_locators: list | None = None,
                   seed: int | None = None,
                   previous_text: str | None = None,
                   next_text: str | None = None,
                   previous_request_ids: list | None = None,
                   next_request_ids: list | None = None) -> dict:
    """ElevenLabs REST: POST /v1/text-to-speech/{voice_id} — synthesize
    `text` with `voice_id`.

    Real API returns the raw audio bytes. The mock returns a JSON
    object with `history_item_id`, `voice_id`, `model_id`,
    `character_count`, `output_format`, `audio_format`, `sample_rate`,
    and the base64-encoded fake audio in `audio_base64`."""
    with _lock():
        s = _load_state()
        result = _tts_common(
            s, voice_id=voice_id, text=text, model_id=model_id,
            voice_settings=voice_settings, output_format=output_format,
            source="TTS", language_code=language_code,
            pronunciation_dictionary_locators=pronunciation_dictionary_locators,
            seed=seed, previous_text=previous_text, next_text=next_text,
        )
        if "detail" in result:
            _record(s, "text_to_speech", voice_id=voice_id,
                    result=result["detail"]["status"])
            _save_state(s)
            return result
        codec, sr = _format_meta(output_format)
        audio = _fake_audio(
            f"{result['history_item_id']}|{text}|{voice_id}|{output_format}",
            length_hint=min(2048, max(64, len(text) * 4)),
        )
        result.update({
            "audio_base64": audio,
            "audio_format": codec,
            "sample_rate": sr,
        })
        _record(s, "text_to_speech", voice_id=voice_id,
                model_id=result["model_id"], text_len=len(text),
                history_item_id=result["history_item_id"])
        _save_state(s)
        return result


@mcp.tool(name="text_to_speech_stream")
def text_to_speech_stream(voice_id: str,
                          text: str,
                          model_id: str = "eleven_multilingual_v2",
                          voice_settings: dict | None = None,
                          output_format: str = "mp3_44100_128",
                          language_code: str | None = None,
                          optimize_streaming_latency: int = 0,
                          chunk_length_schedule: list | None = None,
                          pronunciation_dictionary_locators: list | None = None,
                          seed: int | None = None) -> dict:
    """ElevenLabs REST: POST /v1/text-to-speech/{voice_id}/stream —
    same as `text_to_speech` but the real API streams chunked audio.
    The mock returns the same JSON shape plus a `chunks` array of
    base64 fragments and an `optimize_streaming_latency` value
    (0-4)."""
    with _lock():
        s = _load_state()
        result = _tts_common(
            s, voice_id=voice_id, text=text, model_id=model_id,
            voice_settings=voice_settings, output_format=output_format,
            source="TTS", language_code=language_code,
            pronunciation_dictionary_locators=pronunciation_dictionary_locators,
            seed=seed, previous_text=None, next_text=None,
        )
        if "detail" in result:
            _record(s, "text_to_speech_stream", voice_id=voice_id,
                    result=result["detail"]["status"])
            _save_state(s)
            return result
        codec, sr = _format_meta(output_format)
        # Split into 3 chunks for streaming illusion.
        chunks = []
        for idx in range(3):
            chunks.append(_fake_audio(
                (f"{result['history_item_id']}|chunk{idx}|"
                 f"{text}|{voice_id}|{output_format}"),
                length_hint=min(1024, max(48, len(text) * 2)),
            ))
        result.update({
            "audio_base64": "".join(chunks),
            "chunks": chunks,
            "audio_format": codec,
            "sample_rate": sr,
            "optimize_streaming_latency":
                max(0, min(4, int(optimize_streaming_latency))),
        })
        _record(s, "text_to_speech_stream", voice_id=voice_id,
                model_id=result["model_id"], text_len=len(text),
                history_item_id=result["history_item_id"])
        _save_state(s)
        return result


# ---------------------------------------------------------------------------
# Speech-to-Speech
# ---------------------------------------------------------------------------

@mcp.tool(name="speech_to_speech")
def speech_to_speech(voice_id: str,
                     audio_base64: str,
                     model_id: str = "eleven_english_sts_v2",
                     voice_settings: dict | None = None,
                     output_format: str = "mp3_44100_128",
                     remove_background_noise: bool = False,
                     seed: int | None = None) -> dict:
    """ElevenLabs REST: POST /v1/speech-to-speech/{voice_id} — convert
    an input audio recording into speech using `voice_id`'s voice
    characteristics.

    Real API takes binary audio multipart; the mock takes the same
    audio as a base64 string. Returns a `history_item_id` plus
    base64-encoded fake output audio."""
    with _lock():
        s = _load_state()
        voice = s["voices"].get(voice_id)
        if not voice:
            _record(s, "speech_to_speech", voice_id=voice_id,
                    result="voice_not_found")
            _save_state(s)
            return _err("voice_not_found",
                        f"A voice for the voice_id {voice_id} "
                        f"could not be found.")
        model = _resolve_model(s, model_id)
        if model is None or not model.get("can_do_voice_conversion"):
            _record(s, "speech_to_speech", voice_id=voice_id,
                    result="invalid_model")
            _save_state(s)
            return _err("invalid_model",
                        f"Model {model_id} does not support voice "
                        f"conversion.")
        if not audio_base64:
            return _err("invalid_request", "audio_base64 is required")
        if output_format and output_format not in _VALID_OUTPUT_FORMATS:
            return _err("invalid_output_format",
                        f"Unsupported output_format: {output_format}.")
        try:
            input_size = len(base64.b64decode(audio_base64))
        except Exception:
            return _err("invalid_request",
                        "audio_base64 is not valid base64")
        codec, sr = _format_meta(output_format)
        hid = _new_id_unique(s, "history")
        # STS doesn't count toward the character quota.
        item = {
            "history_item_id": hid,
            "request_id": _new_id_unique(s, "history"),
            "voice_id": voice_id,
            "voice_name": voice.get("name", ""),
            "voice_category": voice.get("category", "premade"),
            "model_id": model["model_id"],
            "text": "",
            "date_unix": _now_unix(),
            "character_count_change_from": 0,
            "character_count_change_to": 0,
            "content_type": ("audio/mpeg" if output_format.startswith("mp3")
                             else f"audio/{codec}"),
            "state": "created",
            "settings": {**voice.get("settings", {}), **(voice_settings or {})},
            "source": "STS",
            "input_size_bytes": input_size,
        }
        s["history"][hid] = item
        audio = _fake_audio(
            f"{hid}|sts|{voice_id}|{output_format}|{input_size}",
            length_hint=min(2048, max(128, input_size // 8)),
        )
        _record(s, "speech_to_speech", voice_id=voice_id,
                model_id=model["model_id"], history_item_id=hid,
                input_size_bytes=input_size)
        _save_state(s)
        return {
            "history_item_id": hid,
            "voice_id": voice_id,
            "model_id": model["model_id"],
            "output_format": output_format,
            "audio_base64": audio,
            "audio_format": codec,
            "sample_rate": sr,
        }


# ---------------------------------------------------------------------------
# Speech-to-Text (Scribe)
# ---------------------------------------------------------------------------

_FAKE_TRANSCRIPT_WORDS = [
    "hello", "this", "is", "a", "test", "of", "the",
    "mock", "scribe", "transcription", "service", "today",
    "we", "are", "demoing", "speech", "recognition",
]


@mcp.tool(name="speech_to_text")
def speech_to_text(audio_base64: str,
                   model_id: str = "scribe_v1",
                   language_code: str | None = None,
                   tag_audio_events: bool = True,
                   num_speakers: int | None = None,
                   timestamps_granularity: str = "word",
                   diarize: bool = False) -> dict:
    """ElevenLabs REST: POST /v1/speech-to-text — transcribe audio
    using Scribe. Returns `language_code`, `language_probability`,
    `text`, and (if requested) `words` with timestamps and speaker
    labels."""
    with _lock():
        s = _load_state()
        model = _resolve_model(s, model_id)
        if model is None or model.get("model_id", "") != "scribe_v1":
            _record(s, "speech_to_text", result="invalid_model",
                    model_id=model_id)
            _save_state(s)
            return _err("invalid_model",
                        f"Model {model_id} is not a speech-to-text model.")
        if not audio_base64:
            return _err("invalid_request", "audio_base64 is required")
        try:
            input_size = len(base64.b64decode(audio_base64))
        except Exception:
            return _err("invalid_request",
                        "audio_base64 is not valid base64")
        if timestamps_granularity not in ("none", "word", "character"):
            return _err("invalid_request",
                        "timestamps_granularity must be one of "
                        "'none', 'word', 'character'")
        # Deterministic fake transcript based on payload size.
        rng = random.Random(hashlib.sha256(audio_base64.encode())
                            .hexdigest())
        word_count = max(3, min(20, input_size // 1024 or 4))
        words_used = [rng.choice(_FAKE_TRANSCRIPT_WORDS)
                      for _ in range(word_count)]
        full_text = " ".join(words_used)
        speakers = (num_speakers if num_speakers and num_speakers > 0
                    else (2 if diarize else 1))
        word_objs = []
        if timestamps_granularity != "none":
            cursor = 0.0
            for i, w in enumerate(words_used):
                dur = round(0.18 + 0.04 * (i % 4), 3)
                word_objs.append({
                    "text": w,
                    "type": "word",
                    "start": round(cursor, 3),
                    "end": round(cursor + dur, 3),
                    "speaker_id":
                        f"speaker_{(i % speakers) + 1}" if diarize
                        else "speaker_1",
                })
                cursor += dur + 0.06
        lang = language_code or "en"
        _record(s, "speech_to_text", model_id="scribe_v1",
                input_size_bytes=input_size, diarize=diarize,
                language_code=lang)
        _save_state(s)
        return {
            "language_code": lang,
            "language_probability": 0.99,
            "text": full_text,
            "words": word_objs,
            "additional_formats": [],
            "transcription_id": _new_id_unique(s, "history"),
            "model_id": "scribe_v1",
        }


# ---------------------------------------------------------------------------
# User / subscription
# ---------------------------------------------------------------------------

@mcp.tool(name="get_user")
def get_user() -> dict:
    """ElevenLabs REST: GET /v1/user — return the authenticated
    user's profile and subscription summary."""
    with _lock():
        s = _load_state()
        _record(s, "get_user")
        _save_state(s)
        u = dict(s["user"])
        return u


@mcp.tool(name="get_user_subscription")
def get_user_subscription() -> dict:
    """ElevenLabs REST: GET /v1/user/subscription — return the
    authenticated user's subscription block (character usage, tier,
    voice slot usage, refresh window)."""
    with _lock():
        s = _load_state()
        _record(s, "get_user_subscription")
        _save_state(s)
        return dict(s["user"]["subscription"])


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@mcp.tool(name="list_history_items")
def list_history_items(page_size: int = 100,
                       start_after_history_item_id: str = "",
                       voice_id: str = "",
                       search: str = "",
                       source: str = "") -> dict:
    """ElevenLabs REST: GET /v1/history — list past generation items.
    Sorted newest-first; cursor pagination via
    `start_after_history_item_id`."""
    with _lock():
        s = _load_state()
        items = list(s["history"].values())
        if voice_id:
            items = [i for i in items if i.get("voice_id") == voice_id]
        if source:
            items = [i for i in items if i.get("source") == source]
        if search:
            q = search.lower()
            items = [i for i in items
                     if q in (i.get("text", "") or "").lower()
                     or q in (i.get("voice_name", "") or "").lower()]
        items.sort(key=lambda i: i.get("date_unix", 0), reverse=True)
        start = 0
        if start_after_history_item_id:
            for idx, it in enumerate(items):
                if it["history_item_id"] == start_after_history_item_id:
                    start = idx + 1
                    break
        ps = max(1, min(int(page_size or 100), 1000))
        page = items[start: start + ps]
        last_id = page[-1]["history_item_id"] if page else None
        has_more = start + ps < len(items)
        _record(s, "list_history_items", count=len(page),
                voice_id=voice_id, source=source)
        _save_state(s)
        return {
            "history": page,
            "last_history_item_id": last_id,
            "has_more": has_more,
        }


@mcp.tool(name="get_history_item")
def get_history_item(history_item_id: str) -> dict:
    """ElevenLabs REST: GET /v1/history/{history_item_id} — fetch a
    single history item (no audio bytes)."""
    with _lock():
        s = _load_state()
        item = s["history"].get(history_item_id)
        _record(s, "get_history_item", history_item_id=history_item_id,
                result="ok" if item else "history_item_not_found")
        _save_state(s)
        if not item:
            return _err("history_item_not_found",
                        f"A history item with id {history_item_id} "
                        f"could not be found.")
        return dict(item)


@mcp.tool(name="delete_history_item")
def delete_history_item(history_item_id: str) -> dict:
    """ElevenLabs REST: DELETE /v1/history/{history_item_id} — remove
    a single history item permanently."""
    with _lock():
        s = _load_state()
        item = s["history"].get(history_item_id)
        if not item:
            _record(s, "delete_history_item",
                    history_item_id=history_item_id,
                    result="history_item_not_found")
            _save_state(s)
            return _err("history_item_not_found",
                        f"A history item with id {history_item_id} "
                        f"could not be found.")
        del s["history"][history_item_id]
        _record(s, "delete_history_item", history_item_id=history_item_id)
        _save_state(s)
        return {"status": "ok"}


# ---------------------------------------------------------------------------
# Pronunciation dictionaries
# ---------------------------------------------------------------------------

@mcp.tool(name="list_pronunciation_dictionaries")
def list_pronunciation_dictionaries(page_size: int = 30,
                                    cursor: str = "",
                                    sort: str = "creation_time_unix",
                                    sort_direction: str = "descending"
                                    ) -> dict:
    """ElevenLabs REST: GET /v1/pronunciation-dictionaries — list the
    user's pronunciation dictionaries."""
    with _lock():
        s = _load_state()
        items = list(s["dictionaries"].values())
        key_fn = (lambda d, k=sort: d.get(k, 0)
                  if sort in ("creation_time_unix",
                              "latest_version_id",
                              "name")
                  else d.get("creation_time_unix", 0))
        items.sort(key=key_fn,
                   reverse=(sort_direction == "descending"))
        start = 0
        if cursor:
            for idx, it in enumerate(items):
                if it["pronunciation_dictionary_id"] == cursor:
                    start = idx + 1
                    break
        ps = max(1, min(int(page_size or 30), 100))
        page = items[start: start + ps]
        next_cursor = (page[-1]["pronunciation_dictionary_id"]
                       if page and start + ps < len(items) else None)
        _record(s, "list_pronunciation_dictionaries", count=len(page))
        _save_state(s)
        return {
            "pronunciation_dictionaries": page,
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
        }


@mcp.tool(name="get_pronunciation_dictionary")
def get_pronunciation_dictionary(pronunciation_dictionary_id: str) -> dict:
    """ElevenLabs REST: GET /v1/pronunciation-dictionaries/
    {pronunciation_dictionary_id} — retrieve a single pronunciation
    dictionary's metadata (rules listed under `rules`)."""
    with _lock():
        s = _load_state()
        d = s["dictionaries"].get(pronunciation_dictionary_id)
        _record(s, "get_pronunciation_dictionary",
                pronunciation_dictionary_id=pronunciation_dictionary_id,
                result="ok" if d else "dictionary_not_found")
        _save_state(s)
        if not d:
            return _err("dictionary_not_found",
                        f"A pronunciation dictionary with id "
                        f"{pronunciation_dictionary_id} could not "
                        f"be found.")
        return dict(d)


# ---------------------------------------------------------------------------
# Mock-only helpers
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state. Not part of the real
    ElevenLabs surface; for verifier introspection."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed")
def mock_debug_seed(user: dict | None = None,
                    voices: list | None = None,
                    models: list | None = None,
                    history: list | None = None,
                    dictionaries: list | None = None,
                    replace: bool = False) -> dict:
    """Mock-only: seed the persisted state. If `replace` is true,
    the state is fully reset (and the default catalog re-applied)
    before seeding.

    Each input is a list of ElevenLabs-shaped dicts. Voices and models
    use their natural ids (`voice_id`, `model_id`); if missing, the
    mock generates an id. History items must include `history_item_id`
    or one will be generated."""
    with _lock():
        if replace:
            s = _empty_state()
            _seed_default_catalog(s)
        else:
            s = _load_state()
        if user:
            s["user"].update(user)
            if user.get("subscription"):
                s["user"]["subscription"].update(user["subscription"])
        for v in voices or []:
            if not isinstance(v, dict):
                continue
            vid = v.get("voice_id") or _new_id_unique(s, "voice")
            base = _make_voice(
                voice_id=vid,
                name=v.get("name", vid),
                category=v.get("category", "cloned"),
                labels=v.get("labels") or {},
                description=v.get("description", ""),
                preview_url=v.get("preview_url", ""),
                samples=v.get("samples") or [],
                fine_tuning=v.get("fine_tuning"),
                settings=v.get("settings"),
                available_for_tiers=v.get("available_for_tiers") or [],
                sharing=v.get("sharing"),
                high_quality_base_model_ids=v.get(
                    "high_quality_base_model_ids") or [],
            )
            s["voices"][vid] = base
        for m in models or []:
            if not isinstance(m, dict):
                continue
            mid = m.get("model_id") or f"model_{_new_id_unique(s, 'model')}"
            s["models"][mid] = {**s["models"].get(mid, {}), **m,
                                "model_id": mid}
        for h in history or []:
            if not isinstance(h, dict):
                continue
            hid = h.get("history_item_id") or _new_id_unique(s, "history")
            item = {
                "history_item_id": hid,
                "request_id": h.get("request_id", hid),
                "voice_id": h.get("voice_id", ""),
                "voice_name": h.get("voice_name", ""),
                "voice_category": h.get("voice_category", "premade"),
                "model_id": h.get("model_id", "eleven_multilingual_v2"),
                "text": h.get("text", ""),
                "date_unix": int(h.get("date_unix", _now_unix())),
                "character_count_change_from": h.get(
                    "character_count_change_from", 0),
                "character_count_change_to": h.get(
                    "character_count_change_to",
                    len(h.get("text", ""))),
                "content_type": h.get("content_type", "audio/mpeg"),
                "state": h.get("state", "created"),
                "settings": h.get("settings", {}),
                "source": h.get("source", "TTS"),
            }
            s["history"][hid] = item
        for d in dictionaries or []:
            if not isinstance(d, dict):
                continue
            did = (d.get("pronunciation_dictionary_id")
                   or _new_id_unique(s, "dict"))
            rules = d.get("rules") or []
            entry = {
                "pronunciation_dictionary_id": did,
                "latest_version_id": d.get(
                    "latest_version_id",
                    _new_id_unique(s, "dict")),
                "name": d.get("name", did),
                "description": d.get("description", ""),
                "creation_time_unix": int(d.get("creation_time_unix",
                                                _now_unix())),
                "created_by": d.get("created_by",
                                    s["user"]["user_id"]),
                "rules": rules,
                "rule_count": len(rules),
                "version_rules_num": d.get("version_rules_num",
                                           len(rules)),
                "permission_on_resource": d.get(
                    "permission_on_resource", "admin"),
            }
            s["dictionaries"][did] = entry
        _record(s, "debug_seed",
                counts={"voices": len(voices or []),
                        "models": len(models or []),
                        "history": len(history or []),
                        "dictionaries": len(dictionaries or [])},
                replace=replace)
        _save_state(s)
        return {
            "ok": True,
            "voice_ids": list(s["voices"].keys()),
            "model_ids": list(s["models"].keys()),
            "history_ids": list(s["history"].keys()),
            "dictionary_ids": list(s["dictionaries"].keys()),
        }


if __name__ == "__main__":
    mcp.run()
