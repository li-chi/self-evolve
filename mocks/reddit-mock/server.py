"""Reddit JSON API mock MCP server.

Mirrors the Reddit JSON API exposed at https://oauth.reddit.com/...
(see https://www.reddit.com/dev/api/). Every read endpoint returns the
canonical Reddit "Thing" / "Listing" envelopes; every write endpoint
returns the `{"json": {"data": {...}, "errors": []}}` shape Reddit
uses for `POST /api/...` form-encoded calls.

Thing type prefixes (Reddit convention):
    t1_  Comment
    t2_  User (Account)
    t3_  Post (Link)
    t4_  Message
    t5_  Subreddit
    t6_  Award (not used by this mock)

Listing envelope:
    {"kind": "Listing",
     "data": {"after": <fullname|None>, "before": <fullname|None>,
              "modhash": None, "dist": <int>,
              "children": [<Thing>, ...]}}

Thing envelope:
    {"kind": "t3"|"t1"|..., "data": {"id": "abc123", "name": "t3_abc123",
                                       ...}}

Implemented tool surface (29 + 8 mock helpers):

  Subreddits
    list_subreddits, search_subreddits, get_subreddit_about,
    subscribe_subreddit, list_user_subscriptions
  Posts
    list_posts, get_post, submit_post, edit_post, delete_post,
    search_posts
  Comments
    submit_comment, edit_comment, delete_comment, list_comments
  Engagement / Saved
    vote, save, unsave, list_saved
  Users / Identity
    get_user_about, list_user_posts, list_user_comments, get_me
  Inbox / Messages
    list_inbox, send_message, mark_message_read
  Moderation
    get_modqueue, approve, remove
  Mock-only
    mock_debug_state, mock_debug_seed_subreddit,
    mock_debug_seed_user, mock_debug_seed_post,
    mock_debug_seed_comment, mock_debug_seed_message,
    mock_debug_seed_subscription, mock_debug_set_self_user

State at `$REDDIT_MOCK_STATE_DIR/state.json` (default
`~/.openclaw/reddit_mock`). Optional `REDDIT_MOCK_SEED_PATH` preloads
state when no state.json exists yet. Per-rollout isolation should
clear the state dir between rollouts. Every call (including reads)
appends to `state["calls"]` so verifiers can replay the trace.

State shape:
    {
        "self_user":     "<username>",
        "users":         {<username>: <User Thing data>},
        "subreddits":    {<display_name>: <Subreddit Thing data>},
        "posts":         {<post_id>: <Post Thing data>},
        "comments":      {<comment_id>: <Comment Thing data>},
        "messages":      {<message_id>: <Message Thing data>},
        "subscriptions": [{"user": <username>,
                            "subreddit": <display_name>}],
        "votes":         {<username>: {<thing_fullname>: 1|-1}},
        "saved":         {<username>: [<thing_fullname>, ...]},
        "next_id":       {...},
        "calls":         [{op, ts, ...}],
    }

Deliberately unsupported (out of scope for the mock):
  - Real OAuth flow / token refresh (mock has no auth)
  - Image / video / gallery upload pipeline (lease, s3 PUT, websocket)
  - Awards / coins / gold economy
  - Multi-reddits (custom feeds)
  - Live threads, websocket events
  - Modmail conversations API (mod_conversations.*)
  - GraphQL endpoints
  - Polls, predictions, gilding
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "REDDIT_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/reddit_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_epoch() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def _empty_state() -> dict:
    self_user = "mock_user"
    state: dict[str, Any] = {
        "self_user": self_user,
        "users": {},
        "subreddits": {},
        "posts": {},
        "comments": {},
        "messages": {},
        "subscriptions": [],
        "votes": {},
        "saved": {},
        "next_id": {
            "post": 1, "comment": 1, "message": 1,
            "user": 1, "subreddit": 1,
        },
        "calls": [],
    }
    # Default self user + a default subreddit, mirroring real-world
    # Reddit (every account has at least one subreddit it can browse).
    state["users"][self_user] = _new_user(state, name=self_user,
                                          link_karma=100, comment_karma=50)
    state["subreddits"]["AskReddit"] = _new_subreddit(
        state, display_name="AskReddit",
        title="Ask Reddit...",
        public_description="r/AskReddit is the place to ask and answer "
                           "thought-provoking questions.",
        subscribers=40_000_000,
    )
    return state


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("REDDIT_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                return json.load(f)
        return _empty_state()
    with open(path, "r", encoding="utf-8") as f:
        s = json.load(f)
    # A state file written as {} (or partially) by another process must
    # not KeyError downstream - merge the skeleton's missing keys.
    for k, v in _empty_state().items():
        s.setdefault(k, v)
    return s


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
    entry = {"op": op, "ts": _now_epoch()}
    entry.update(kwargs)
    state["calls"].append(entry)


# ---------------------------------------------------------------------------
# Reddit IDs / fullnames
# ---------------------------------------------------------------------------

_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_NON_BASE36_RE = re.compile(r"[^0-9a-z]")


def _gen_id(min_len: int = 6, max_len: int = 8) -> str:
    """Reddit IDs are lowercase base-36, 6-8 chars wide."""
    n = secrets.choice(range(min_len, max_len + 1))
    return "".join(secrets.choice(_BASE36) for _ in range(n))


def _fullname(kind_prefix: str, _id: str) -> str:
    return f"{kind_prefix}_{_id}"


_FULLNAME_RE = re.compile(r"^(t[1-6])_([0-9a-z]+)$")


def _parse_fullname(fn: str) -> tuple[str, str] | None:
    if not fn:
        return None
    m = _FULLNAME_RE.match(fn)
    if not m:
        return None
    return m.group(1), m.group(2)


def _resolve_thing(state: dict, fullname: str
                   ) -> tuple[str, dict] | tuple[None, None]:
    """Return (kind_prefix, thing_data_dict) for a fullname or (None, None)."""
    parsed = _parse_fullname(fullname)
    if not parsed:
        return None, None
    kind, _id = parsed
    if kind == "t3":
        p = state["posts"].get(_id)
        return ("t3", p) if p else (None, None)
    if kind == "t1":
        c = state["comments"].get(_id)
        return ("t1", c) if c else (None, None)
    if kind == "t4":
        m = state["messages"].get(_id)
        return ("t4", m) if m else (None, None)
    if kind == "t5":
        # subreddit lookup by id (we also key subreddits by display_name)
        for sr in state["subreddits"].values():
            if sr.get("id") == _id:
                return "t5", sr
        return None, None
    if kind == "t2":
        for u in state["users"].values():
            if u.get("id") == _id:
                return "t2", u
        return None, None
    return None, None


# ---------------------------------------------------------------------------
# Thing constructors (return the inner "data" dict only)
# ---------------------------------------------------------------------------

def _new_user(state: dict, *, name: str,
              link_karma: int = 0, comment_karma: int = 0,
              created_utc: float | None = None,
              **extra: Any) -> dict:
    uid = _gen_id()
    ts = float(created_utc) if created_utc is not None else _now_epoch()
    user: dict[str, Any] = {
        "id": uid,
        "name": name,
        "created": ts,
        "created_utc": ts,
        "link_karma": int(link_karma),
        "comment_karma": int(comment_karma),
        "total_karma": int(link_karma) + int(comment_karma),
        "awardee_karma": 0,
        "awarder_karma": 0,
        "is_employee": False,
        "is_friend": False,
        "is_mod": False,
        "is_gold": False,
        "has_verified_email": True,
        "verified": True,
        "accept_followers": True,
        "hide_from_robots": False,
        "snoovatar_img": "",
        "icon_img": "",
        "pref_show_snoovatar": False,
        "subreddit": {
            "display_name": f"u_{name}",
            "display_name_prefixed": f"u/{name}",
            "name": "",
            "title": name,
            "public_description": "",
            "subscribers": 0,
            "over_18": False,
            "subreddit_type": "user",
            "url": f"/user/{name}/",
        },
    }
    user.update(extra or {})
    return user


def _new_subreddit(state: dict, *, display_name: str,
                   title: str | None = None,
                   public_description: str = "",
                   description: str = "",
                   subscribers: int = 0,
                   subreddit_type: str = "public",
                   submission_type: str = "any",
                   over18: bool = False,
                   created_utc: float | None = None,
                   **extra: Any) -> dict:
    sid = _gen_id()
    ts = float(created_utc) if created_utc is not None else _now_epoch()
    sr: dict[str, Any] = {
        "id": sid,
        "name": _fullname("t5", sid),
        "display_name": display_name,
        "display_name_prefixed": f"r/{display_name}",
        "title": title or display_name,
        "public_description": public_description,
        "description": description or public_description,
        "description_html": ("<p>" + (description or public_description)
                              + "</p>") if (description
                                            or public_description) else "",
        "subscribers": int(subscribers),
        "accounts_active": max(1, int(subscribers) // 1000),
        "created": ts,
        "created_utc": ts,
        "over18": bool(over18),
        "subreddit_type": subreddit_type,
        "submission_type": submission_type,
        "lang": "en",
        "advertiser_category": "",
        "primary_color": "",
        "key_color": "",
        "banner_background_color": "",
        "header_img": None,
        "icon_img": "",
        "community_icon": "",
        "url": f"/r/{display_name}/",
        "quarantine": False,
        "hide_ads": False,
        "is_enrolled_in_new_modmail": False,
        "allow_videos": True,
        "allow_images": True,
        "restrict_posting": False,
        "restrict_commenting": False,
    }
    sr.update(extra or {})
    return sr


def _new_post(state: dict, *, post_id: str | None = None,
              subreddit: str, author: str, title: str,
              selftext: str = "", url: str | None = None,
              is_self: bool = True,
              score: int = 1, num_comments: int = 0,
              created_utc: float | None = None,
              nsfw: bool = False, spoiler: bool = False,
              flair_id: str | None = None,
              flair_text: str | None = None,
              sendreplies: bool = True,
              kind: str = "self",
              **extra: Any) -> dict:
    pid = post_id or _gen_id()
    ts = float(created_utc) if created_utc is not None else _now_epoch()
    sr = state["subreddits"].get(subreddit)
    sr_id = sr["id"] if sr else _gen_id()
    permalink = (f"/r/{subreddit}/comments/{pid}/"
                 f"{re.sub(r'[^a-z0-9]+', '_', (title or '').lower())[:50]}/")
    if is_self or kind == "self":
        post_url = f"https://www.reddit.com{permalink}"
        post_domain = f"self.{subreddit}"
        post_hint = "self"
    else:
        post_url = url or ""
        post_hint = "link" if kind == "link" else kind
        post_domain = re.sub(r"^https?://", "",
                              post_url or "").split("/", 1)[0] or "unknown"
    post: dict[str, Any] = {
        "id": pid,
        "name": _fullname("t3", pid),
        "title": title,
        "selftext": selftext if (is_self or kind == "self") else "",
        "selftext_html": (("<!-- SC_OFF --><div class=\"md\"><p>"
                            + selftext + "</p></div><!-- SC_ON -->")
                           if (is_self or kind == "self") and selftext
                           else None),
        "author": author,
        "subreddit": subreddit,
        "subreddit_id": _fullname("t5", sr_id),
        "subreddit_name_prefixed": f"r/{subreddit}",
        "score": int(score),
        "ups": max(0, int(score)),
        "downs": 0,
        "upvote_ratio": 1.0 if int(score) >= 0 else 0.0,
        "num_comments": int(num_comments),
        "created": ts,
        "created_utc": ts,
        "edited": False,
        "permalink": permalink,
        "url": post_url,
        "is_self": bool(is_self or kind == "self"),
        "over_18": bool(nsfw),
        "spoiler": bool(spoiler),
        "stickied": False,
        "locked": False,
        "archived": False,
        "distinguished": None,
        "gilded": 0,
        "total_awards_received": 0,
        "link_flair_text": flair_text,
        "link_flair_css_class": None,
        "author_flair_text": None,
        "thumbnail": "self" if (is_self or kind == "self") else "",
        "domain": post_domain,
        "is_video": (kind == "video"),
        "post_hint": post_hint,
        "num_crossposts": 0,
        "view_count": None,
        "removed_by_category": None,
        "saved": False,
        "hidden": False,
        "clicked": False,
        "hide_score": False,
        "no_follow": False,
        "send_replies": bool(sendreplies),
        "contest_mode": False,
        "is_robot_indexable": True,
        # internal moderation/removal tracking (not part of the public Thing,
        # but consumed by mod tooling below)
        "_removed": False,
        "_approved": True,
        "_modqueue": False,
        "_deleted": False,
    }
    if flair_id:
        post["link_flair_template_id"] = flair_id
    post.update(extra or {})
    return post


def _new_comment(state: dict, *, comment_id: str | None = None,
                 link_id: str, parent_id: str, author: str,
                 body: str, score: int = 1,
                 created_utc: float | None = None,
                 depth: int = 0,
                 subreddit: str = "",
                 **extra: Any) -> dict:
    cid = comment_id or _gen_id()
    ts = float(created_utc) if created_utc is not None else _now_epoch()
    # Permalink follows the post's permalink + comment id
    pid_parsed = _parse_fullname(link_id)
    post_id = pid_parsed[1] if pid_parsed else ""
    post = state["posts"].get(post_id) if post_id else None
    sr_name = subreddit or (post or {}).get("subreddit", "")
    sr = state["subreddits"].get(sr_name)
    sr_id_fn = _fullname("t5", sr["id"]) if sr else ""
    permalink = (f"/r/{sr_name}/comments/{post_id}/_/{cid}/"
                 if post_id else f"/comments/{cid}/")
    comment: dict[str, Any] = {
        "id": cid,
        "name": _fullname("t1", cid),
        "link_id": link_id,
        "parent_id": parent_id,
        "author": author,
        "body": body,
        "body_html": ("<div class=\"md\"><p>" + body + "</p></div>")
                       if body else "",
        "subreddit": sr_name,
        "subreddit_id": sr_id_fn,
        "score": int(score),
        "ups": max(0, int(score)),
        "downs": 0,
        "controversiality": 0,
        "created": ts,
        "created_utc": ts,
        "edited": False,
        "replies": "",  # set to a Listing when rendering the tree
        "depth": int(depth),
        "permalink": permalink,
        "distinguished": None,
        "stickied": False,
        "score_hidden": False,
        "archived": False,
        "locked": False,
        "collapsed": False,
        "is_submitter": bool(post and post.get("author") == author),
        "total_awards_received": 0,
        "gilded": 0,
        "saved": False,
        # internal moderation/removal tracking
        "_removed": False,
        "_approved": True,
        "_modqueue": False,
        "_deleted": False,
    }
    comment.update(extra or {})
    return comment


def _new_message(state: dict, *, message_id: str | None = None,
                 author: str, dest: str, subject: str, body: str,
                 created_utc: float | None = None,
                 new: bool = True,
                 was_comment: bool = False,
                 parent_id: str | None = None,
                 **extra: Any) -> dict:
    mid = message_id or _gen_id()
    ts = float(created_utc) if created_utc is not None else _now_epoch()
    msg: dict[str, Any] = {
        "id": mid,
        "name": _fullname("t4", mid),
        "author": author,
        "dest": dest,
        "subject": subject,
        "body": body,
        "body_html": ("<div class=\"md\"><p>" + body + "</p></div>")
                      if body else "",
        "was_comment": bool(was_comment),
        "new": bool(new),
        "created": ts,
        "created_utc": ts,
        "parent_id": parent_id,
        "first_message_name": parent_id or _fullname("t4", mid),
        "distinguished": None,
        "subreddit": None,
        "context": "",
        "replies": "",
    }
    msg.update(extra or {})
    return msg


# ---------------------------------------------------------------------------
# Listing helpers
# ---------------------------------------------------------------------------

def _public_thing(data: dict) -> dict:
    """Strip mock-internal fields (those starting with `_`)."""
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _wrap(kind: str, data: dict) -> dict:
    return {"kind": kind, "data": _public_thing(data)}


def _listing(children: list[dict], after: str | None = None,
             before: str | None = None) -> dict:
    return {
        "kind": "Listing",
        "data": {
            "after": after,
            "before": before,
            "modhash": None,
            "dist": len(children),
            "children": list(children),
            "geo_filter": "",
        },
    }


def _paginate(items: list[dict], *, limit: int, after: str | None,
              before: str | None,
              fullname_of: callable) -> tuple[list[dict], str | None,
                                              str | None]:
    """Paginate a list of Thing-data dicts by `name` fullname cursor.

    Reddit's pagination is cursor-based: `after` = "fetch items strictly
    AFTER this fullname"; `before` = "fetch items strictly BEFORE this
    fullname". The result `after` is the last item's fullname when more
    pages remain; `before` is the first item's fullname when an earlier
    page exists.
    """
    if limit <= 0:
        limit = 25
    if limit > 100:
        limit = 100
    names = [fullname_of(it) for it in items]
    start = 0
    end = len(items)
    if after:
        try:
            start = names.index(after) + 1
        except ValueError:
            start = 0
    elif before:
        try:
            end = names.index(before)
        except ValueError:
            end = len(items)
    page = items[start:end][:limit]
    next_after = (fullname_of(page[-1])
                  if page and (start + len(page)) < end else None)
    next_before = (fullname_of(page[0])
                   if page and start > 0 else None)
    return page, next_after, next_before


def _resolve_self(state: dict) -> str:
    return state.get("self_user") or "mock_user"


def _ensure_user(state: dict, username: str) -> dict:
    u = state["users"].get(username)
    if not u:
        u = _new_user(state, name=username)
        state["users"][username] = u
    return u


def _ensure_subreddit(state: dict, display_name: str) -> dict:
    sr = state["subreddits"].get(display_name)
    if not sr:
        sr = _new_subreddit(state, display_name=display_name)
        state["subreddits"][display_name] = sr
    return sr


def _norm_sr_name(name: str) -> str:
    """Strip leading 'r/' or '/r/' if present and trim whitespace."""
    if not name:
        return ""
    name = name.strip()
    if name.startswith("/r/"):
        name = name[3:]
    elif name.startswith("r/"):
        name = name[2:]
    return name.strip("/")


# ---------------------------------------------------------------------------
# Error envelopes
# ---------------------------------------------------------------------------

def _api_error(field: str, msg_id: str, message: str) -> dict:
    """Reddit POST /api/* error shape: {json: {errors: [[id, msg, field]]}}."""
    return {"json": {"errors": [[msg_id, message, field]]}}


def _api_ok(data: dict | None = None) -> dict:
    return {"json": {"errors": [], "data": data or {}}}


# ---------------------------------------------------------------------------
# Filtering / sorting helpers
# ---------------------------------------------------------------------------

def _post_visible(post: dict) -> bool:
    return not (post.get("_removed") or post.get("_deleted"))


def _comment_visible(c: dict) -> bool:
    return not (c.get("_removed") or c.get("_deleted"))


def _sort_posts(posts: list[dict], sort: str,
                t: str = "all") -> list[dict]:
    sort = (sort or "hot").lower()
    if sort == "new":
        return sorted(posts, key=lambda p: p.get("created_utc", 0),
                       reverse=True)
    if sort == "top":
        cutoff = _time_cutoff(t)
        filtered = [p for p in posts
                    if (p.get("created_utc", 0) >= cutoff)] if cutoff else posts
        return sorted(filtered, key=lambda p: p.get("score", 0), reverse=True)
    if sort == "controversial":
        return sorted(posts, key=lambda p: (
            -(p.get("ups", 0) + abs(p.get("downs", 0))),
            -p.get("num_comments", 0),
        ))
    if sort == "rising":
        return sorted(posts, key=lambda p: (
            -p.get("score", 0), -p.get("created_utc", 0)))
    # hot — simple Reddit-style: score / age + a comment-volume bump
    now = _now_epoch()
    def _hot(p: dict) -> float:
        age_hr = max(1.0, (now - p.get("created_utc", now)) / 3600.0)
        return (p.get("score", 0) + 0.5 * p.get("num_comments", 0)) / (
            (age_hr + 2.0) ** 1.5)
    return sorted(posts, key=_hot, reverse=True)


_TIME_WINDOWS = {
    "hour": 3600, "day": 86_400, "week": 604_800,
    "month": 2_629_800, "year": 31_557_600, "all": None,
}


def _time_cutoff(t: str) -> float | None:
    if not t:
        return None
    secs = _TIME_WINDOWS.get((t or "all").lower())
    if secs is None:
        return None
    return _now_epoch() - secs


def _sort_comments(comments: list[dict], sort: str) -> list[dict]:
    sort = (sort or "best").lower()
    if sort in ("new", "old"):
        return sorted(comments, key=lambda c: c.get("created_utc", 0),
                       reverse=(sort == "new"))
    if sort == "top":
        return sorted(comments, key=lambda c: c.get("score", 0), reverse=True)
    if sort == "controversial":
        return sorted(comments, key=lambda c: -(c.get("ups", 0)
                                                 + abs(c.get("downs", 0))))
    if sort == "qa":
        # Roughly: submitter answers first, then highest-scored
        return sorted(comments, key=lambda c: (
            not c.get("is_submitter"), -c.get("score", 0)))
    # "best" / default
    return sorted(comments, key=lambda c: c.get("score", 0), reverse=True)


_QUOTED_KV_RE = re.compile(r'(\w+):("[^"]+"|\S+)')


def _parse_search_q(q: str) -> tuple[list[str], dict[str, str]]:
    """Extract Reddit-style structured filters from a free-text query.

    Supported: subreddit:<name>, author:<user>, flair:<text>,
    nsfw:yes|no. Everything else is a free-text term to AND-match
    against post title + body."""
    filters: dict[str, str] = {}
    remaining = q or ""
    for m in _QUOTED_KV_RE.finditer(q or ""):
        key = m.group(1).lower()
        val = m.group(2).strip('"')
        if key in {"subreddit", "sr", "author", "flair", "nsfw", "self"}:
            filters[key] = val
            remaining = remaining.replace(m.group(0), " ")
    terms = [t for t in re.split(r"\s+", remaining.strip()) if t]
    return terms, filters


def _post_matches_search(post: dict, terms: list[str],
                         filters: dict[str, str]) -> bool:
    body = f"{post.get('title', '')} {post.get('selftext', '')}".lower()
    for t in terms:
        if t.lower() not in body:
            return False
    if "subreddit" in filters and post.get("subreddit", "").lower() != \
            filters["subreddit"].lower():
        return False
    if "sr" in filters and post.get("subreddit", "").lower() != \
            filters["sr"].lower():
        return False
    if "author" in filters and post.get("author", "").lower() != \
            filters["author"].lower():
        return False
    if "flair" in filters:
        if (post.get("link_flair_text") or "").lower() != \
                filters["flair"].lower():
            return False
    if "nsfw" in filters:
        want = filters["nsfw"].lower() in ("yes", "1", "true")
        if bool(post.get("over_18")) != want:
            return False
    return True


# ---------------------------------------------------------------------------
# FastMCP
# ---------------------------------------------------------------------------

mcp = FastMCP("reddit-mock")


# ---------------------------------------------------------------------------
# Subreddits
# ---------------------------------------------------------------------------

@mcp.tool(name="list_subreddits")
def list_subreddits(where: str = "popular",
                    limit: int = 25,
                    after: str | None = None,
                    before: str | None = None) -> dict:
    """`GET /subreddits/{where}` where `where` ∈
    {popular, new, default, gold}. Returns a Listing of Subreddit
    (`t5`) Things, sorted by subscribers (popular/default), creation
    time (new), or all "gold" subreddits (none in the mock)."""
    where = (where or "popular").lower()
    with _lock():
        s = _load_state()
        srs = list(s["subreddits"].values())
        if where == "new":
            srs.sort(key=lambda x: x.get("created_utc", 0), reverse=True)
        elif where == "gold":
            srs = [x for x in srs if x.get("subreddit_type") == "gold"]
        else:  # popular / default
            srs.sort(key=lambda x: x.get("subscribers", 0), reverse=True)
        page, next_after, next_before = _paginate(
            srs, limit=limit, after=after, before=before,
            fullname_of=lambda x: x["name"],
        )
        children = [_wrap("t5", sr) for sr in page]
        _record(s, "list_subreddits", where=where, count=len(children))
        _save_state(s)
        return _listing(children, after=next_after, before=next_before)


@mcp.tool(name="search_subreddits")
def search_subreddits(q: str,
                      limit: int = 25,
                      sort: str = "relevance") -> dict:
    """`GET /subreddits/search`. Searches subreddit display_name, title,
    and public_description (case-insensitive substring). `sort` ∈
    {relevance, activity}. Returns a Listing of Subreddit Things."""
    with _lock():
        s = _load_state()
        needle = (q or "").lower().strip()
        hits: list[dict] = []
        for sr in s["subreddits"].values():
            hay = " ".join([
                sr.get("display_name", ""),
                sr.get("title", ""),
                sr.get("public_description", ""),
            ]).lower()
            if not needle or needle in hay:
                hits.append(sr)
        if (sort or "relevance").lower() == "activity":
            hits.sort(key=lambda x: x.get("accounts_active", 0), reverse=True)
        else:
            # relevance: exact display_name match wins, then by subscribers
            hits.sort(key=lambda x: (
                0 if x.get("display_name", "").lower() == needle else 1,
                -x.get("subscribers", 0),
            ))
        if limit <= 0:
            limit = 25
        if limit > 100:
            limit = 100
        hits = hits[:limit]
        children = [_wrap("t5", sr) for sr in hits]
        _record(s, "search_subreddits", q=q, sort=sort, count=len(children))
        _save_state(s)
        return _listing(children)


@mcp.tool(name="get_subreddit_about")
def get_subreddit_about(subreddit: str) -> dict:
    """`GET /r/{subreddit}/about`. Returns the Subreddit (`t5`) Thing
    for `subreddit` (display name), or a 404 envelope."""
    name = _norm_sr_name(subreddit)
    with _lock():
        s = _load_state()
        sr = s["subreddits"].get(name)
        if not sr:
            _record(s, "get_subreddit_about", subreddit=name,
                    result="not_found")
            _save_state(s)
            return {"error": 404, "message": "Not Found",
                    "reason": "banned" if False else "private_or_missing"}
        _record(s, "get_subreddit_about", subreddit=name)
        _save_state(s)
        return _wrap("t5", sr)


@mcp.tool(name="subscribe_subreddit")
def subscribe_subreddit(action: str, sr_name: str) -> dict:
    """`POST /api/subscribe`. `action` ∈ {sub, unsub}; `sr_name` is the
    subreddit display name (without `r/`). Returns `{}` on success or
    a `{json:{errors:[...]}}` envelope on bad input."""
    action = (action or "").lower()
    name = _norm_sr_name(sr_name)
    if action not in ("sub", "unsub"):
        return _api_error("action", "INVALID_OPTION",
                          "Action must be 'sub' or 'unsub'")
    with _lock():
        s = _load_state()
        sr = s["subreddits"].get(name)
        if not sr:
            _record(s, "subscribe_subreddit", subreddit=name,
                    result="not_found")
            _save_state(s)
            return _api_error("sr_name", "SUBREDDIT_NOEXIST",
                              f"that subreddit doesn't exist: {name}")
        me = _resolve_self(s)
        _ensure_user(s, me)
        edge = {"user": me, "subreddit": name}
        exists = edge in s["subscriptions"]
        if action == "sub":
            if not exists:
                s["subscriptions"].append(edge)
                sr["subscribers"] = int(sr.get("subscribers", 0)) + 1
        else:
            if exists:
                s["subscriptions"].remove(edge)
                sr["subscribers"] = max(
                    0, int(sr.get("subscribers", 0)) - 1)
        _record(s, "subscribe_subreddit", subreddit=name, action=action)
        _save_state(s)
        return {}


@mcp.tool(name="list_user_subscriptions")
def list_user_subscriptions(limit: int = 25,
                            after: str | None = None,
                            before: str | None = None) -> dict:
    """`GET /subreddits/mine/subscriber`. Returns a Listing of Subreddit
    Things the authenticated user subscribes to."""
    with _lock():
        s = _load_state()
        me = _resolve_self(s)
        names = [e["subreddit"] for e in s["subscriptions"]
                 if e["user"] == me]
        srs = [s["subreddits"][n] for n in names if n in s["subreddits"]]
        srs.sort(key=lambda x: x.get("display_name", "").lower())
        page, next_after, next_before = _paginate(
            srs, limit=limit, after=after, before=before,
            fullname_of=lambda x: x["name"],
        )
        children = [_wrap("t5", sr) for sr in page]
        _record(s, "list_user_subscriptions", count=len(children))
        _save_state(s)
        return _listing(children, after=next_after, before=next_before)


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

@mcp.tool(name="list_posts")
def list_posts(subreddit: str,
               sort: str = "hot",
               t: str = "all",
               limit: int = 25,
               after: str | None = None,
               before: str | None = None) -> dict:
    """`GET /r/{subreddit}/{sort}` where `sort` ∈
    {hot, new, top, rising, controversial}. `t` is the time filter
    used by `top` and `controversial` (hour|day|week|month|year|all).
    Returns a Listing of Post (`t3`) Things."""
    name = _norm_sr_name(subreddit)
    with _lock():
        s = _load_state()
        if name and name not in s["subreddits"]:
            _record(s, "list_posts", subreddit=name, result="not_found")
            _save_state(s)
            return _listing([])
        posts = [p for p in s["posts"].values()
                 if (not name or p.get("subreddit") == name)
                 and _post_visible(p)]
        posts = _sort_posts(posts, sort, t)
        page, next_after, next_before = _paginate(
            posts, limit=limit, after=after, before=before,
            fullname_of=lambda x: x["name"],
        )
        # Apply per-call viewer state (saved, voted) before returning
        me = _resolve_self(s)
        children = []
        for p in page:
            view = dict(p)
            view["saved"] = p["name"] in s["saved"].get(me, [])
            v = s["votes"].get(me, {}).get(p["name"])
            view["likes"] = (True if v == 1 else
                              False if v == -1 else None)
            children.append(_wrap("t3", view))
        _record(s, "list_posts", subreddit=name, sort=sort,
                count=len(children))
        _save_state(s)
        return _listing(children, after=next_after, before=next_before)


def _build_comment_tree(state: dict, link_fullname: str,
                        sort: str = "best",
                        depth: int | None = None) -> list[dict]:
    """Build a tree of `t1_` Things rooted at the direct children of
    `link_fullname` (a post). Comments whose `parent_id` is a `t1_*`
    nest under that parent's `replies` Listing."""
    direct: list[dict] = []
    children_by_parent: dict[str, list[dict]] = {}
    for c in state["comments"].values():
        if not _comment_visible(c):
            continue
        if c.get("link_id") != link_fullname:
            continue
        parent = c.get("parent_id", "")
        children_by_parent.setdefault(parent, []).append(c)
        if parent == link_fullname:
            direct.append(c)

    def _render(c: dict, cur_depth: int) -> dict:
        kids_raw = _sort_comments(
            children_by_parent.get(c["name"], []), sort)
        view = dict(c)
        if depth is None or cur_depth < depth:
            kids = [_render(k, cur_depth + 1) for k in kids_raw]
            view["replies"] = (_listing([{"kind": "t1",
                                           "data": _public_thing(k)}
                                          for k in kids])
                                if kids else "")
        else:
            view["replies"] = ""
        return view

    return [_render(c, 0) for c in _sort_comments(direct, sort)]


@mcp.tool(name="get_post")
def get_post(postId: str,
             subreddit: str | None = None) -> list:
    """`GET /comments/{id}` (or `GET /r/{subreddit}/comments/{id}`).
    Returns a 2-element array: [post Listing (1 child), comment
    Listing (N children, as a tree)]."""
    with _lock():
        s = _load_state()
        post = s["posts"].get(postId)
        if not post or not _post_visible(post):
            _record(s, "get_post", post_id=postId, result="not_found")
            _save_state(s)
            return [{"error": 404, "message": "Not Found"}]
        if subreddit:
            name = _norm_sr_name(subreddit)
            if name and post.get("subreddit") != name:
                _record(s, "get_post", post_id=postId,
                        result="subreddit_mismatch")
                _save_state(s)
                return [{"error": 404, "message": "Not Found"}]
        me = _resolve_self(s)
        view = dict(post)
        view["saved"] = post["name"] in s["saved"].get(me, [])
        v = s["votes"].get(me, {}).get(post["name"])
        view["likes"] = (True if v == 1 else
                          False if v == -1 else None)
        comments_tree = _build_comment_tree(s, post["name"], sort="best")
        _record(s, "get_post", post_id=postId)
        _save_state(s)
        return [
            _listing([_wrap("t3", view)]),
            _listing([_wrap("t1", c) for c in comments_tree]),
        ]


@mcp.tool(name="submit_post")
def submit_post(subreddit: str,
                kind: str,
                title: str,
                text: str | None = None,
                url: str | None = None,
                nsfw: bool = False,
                spoiler: bool = False,
                sendreplies: bool = True,
                flair_id: str | None = None,
                flair_text: str | None = None) -> dict:
    """`POST /api/submit`. `kind` ∈ {self, link, image, video}. For
    `self`, provide `text`; for `link/image/video`, provide `url`.
    Returns Reddit's `{json:{data:{id, name, url}, errors:[]}}`
    envelope on success."""
    name = _norm_sr_name(subreddit)
    kind = (kind or "self").lower()
    if kind not in ("self", "link", "image", "video"):
        return _api_error("kind", "INVALID_OPTION",
                          f"unsupported kind: {kind}")
    if not title:
        return _api_error("title", "NO_TEXT",
                          "title is required")
    if len(title) > 300:
        return _api_error("title", "TOO_LONG",
                          "title must be <= 300 chars")
    if kind == "self" and not isinstance(text, str):
        text = text or ""
    if kind != "self" and not url:
        return _api_error("url", "NO_URL",
                          "url is required for link/image/video posts")
    with _lock():
        s = _load_state()
        if name not in s["subreddits"]:
            _record(s, "submit_post", subreddit=name, result="not_found")
            _save_state(s)
            return _api_error("sr", "SUBREDDIT_NOEXIST",
                              f"no such subreddit: {name}")
        me = _resolve_self(s)
        _ensure_user(s, me)
        post = _new_post(
            s, subreddit=name, author=me, title=title,
            selftext=text or "", url=url, kind=kind,
            is_self=(kind == "self"),
            nsfw=nsfw, spoiler=spoiler,
            sendreplies=sendreplies,
            flair_id=flair_id, flair_text=flair_text,
        )
        s["posts"][post["id"]] = post
        # author karma + subreddit volume hints
        s["users"][me]["link_karma"] = int(
            s["users"][me].get("link_karma", 0)) + 1
        s["users"][me]["total_karma"] = int(
            s["users"][me].get("total_karma", 0)) + 1
        _record(s, "submit_post", subreddit=name, post_id=post["id"],
                kind=kind, title=title)
        _save_state(s)
        return _api_ok({
            "id": post["id"],
            "name": post["name"],
            "url": post["url"],
            "drafts_count": 0,
        })


@mcp.tool(name="edit_post")
def edit_post(thing_id: str, text: str) -> dict:
    """`POST /api/editusertext` against a `t3_` (post) fullname. Only
    works on self-posts owned by the authenticated user."""
    parsed = _parse_fullname(thing_id)
    if not parsed or parsed[0] != "t3":
        return _api_error("thing_id", "NOT_AUTHOR",
                          "expected a t3_ post fullname")
    with _lock():
        s = _load_state()
        post = s["posts"].get(parsed[1])
        if not post or not _post_visible(post):
            _record(s, "edit_post", thing_id=thing_id,
                    result="not_found")
            _save_state(s)
            return _api_error("thing_id", "NO_THING_ID",
                              "thing not found")
        me = _resolve_self(s)
        if post.get("author") != me:
            return _api_error("thing_id", "NOT_AUTHOR",
                              "not the author")
        if not post.get("is_self"):
            return _api_error("thing_id", "NOT_SELF",
                              "cannot edit a link post body")
        post["selftext"] = text or ""
        post["selftext_html"] = ("<!-- SC_OFF --><div class=\"md\"><p>"
                                  + (text or "") + "</p></div><!-- SC_ON -->"
                                  if text else None)
        post["edited"] = _now_epoch()
        _record(s, "edit_post", thing_id=thing_id)
        _save_state(s)
        return _api_ok({
            "things": [{"kind": "t3", "data": _public_thing(post)}],
        })


@mcp.tool(name="delete_post")
def delete_post(id: str) -> dict:  # noqa: A002 (mirror upstream param)
    """`POST /api/del`. Tombstone a `t3_` post owned by the
    authenticated user. Idempotent; returns `{}` on success."""
    return _delete_thing(id)


def _delete_thing(thing_id: str) -> dict:
    parsed = _parse_fullname(thing_id)
    if not parsed:
        return _api_error("id", "NO_THING_ID", "expected a fullname")
    kind, _id = parsed
    with _lock():
        s = _load_state()
        me = _resolve_self(s)
        if kind == "t3":
            post = s["posts"].get(_id)
            if post and post.get("author") == me:
                post["_deleted"] = True
                post["author"] = "[deleted]"
                post["selftext"] = "[deleted]"
                post["selftext_html"] = None
                _record(s, "delete_post", thing_id=thing_id)
                _save_state(s)
                return {}
        elif kind == "t1":
            c = s["comments"].get(_id)
            if c and c.get("author") == me:
                c["_deleted"] = True
                c["author"] = "[deleted]"
                c["body"] = "[deleted]"
                c["body_html"] = "<div class=\"md\"><p>[deleted]</p></div>"
                _record(s, "delete_comment", thing_id=thing_id)
                _save_state(s)
                return {}
        _record(s, "delete_thing", thing_id=thing_id, result="not_found")
        _save_state(s)
        return _api_error("id", "NO_THING_ID",
                          "thing not found or not owned by self")


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

@mcp.tool(name="submit_comment")
def submit_comment(thing_id: str, text: str) -> dict:
    """`POST /api/comment`. Reply to a post (`t3_`) or another comment
    (`t1_`). Returns `{json:{data:{things:[<t1 Thing>]}, errors:[]}}`."""
    parsed = _parse_fullname(thing_id)
    if not parsed or parsed[0] not in ("t3", "t1"):
        return _api_error("thing_id", "NO_THING_ID",
                          "thing_id must be a t3_ or t1_ fullname")
    if not text:
        return _api_error("text", "NO_TEXT", "comment body required")
    with _lock():
        s = _load_state()
        me = _resolve_self(s)
        _ensure_user(s, me)
        parent_kind, parent_local = parsed
        if parent_kind == "t3":
            post = s["posts"].get(parent_local)
            if not post or not _post_visible(post):
                _record(s, "submit_comment", thing_id=thing_id,
                        result="post_not_found")
                _save_state(s)
                return _api_error("thing_id", "NO_THING_ID",
                                  "parent post not found")
            link_id = post["name"]
            parent_id = post["name"]
            depth = 0
            sr_name = post.get("subreddit", "")
        else:
            parent_c = s["comments"].get(parent_local)
            if not parent_c or not _comment_visible(parent_c):
                _record(s, "submit_comment", thing_id=thing_id,
                        result="comment_not_found")
                _save_state(s)
                return _api_error("thing_id", "NO_THING_ID",
                                  "parent comment not found")
            link_id = parent_c["link_id"]
            parent_id = parent_c["name"]
            depth = int(parent_c.get("depth", 0)) + 1
            sr_name = parent_c.get("subreddit", "")
        comment = _new_comment(
            s, link_id=link_id, parent_id=parent_id,
            author=me, body=text, depth=depth,
            subreddit=sr_name,
        )
        s["comments"][comment["id"]] = comment
        # bump parent post comment count
        post_parsed = _parse_fullname(link_id)
        if post_parsed and post_parsed[1] in s["posts"]:
            p = s["posts"][post_parsed[1]]
            p["num_comments"] = int(p.get("num_comments", 0)) + 1
        s["users"][me]["comment_karma"] = int(
            s["users"][me].get("comment_karma", 0)) + 1
        s["users"][me]["total_karma"] = int(
            s["users"][me].get("total_karma", 0)) + 1
        _record(s, "submit_comment", thing_id=thing_id,
                comment_id=comment["id"])
        _save_state(s)
        return _api_ok({
            "things": [{"kind": "t1", "data": _public_thing(comment)}],
        })


@mcp.tool(name="edit_comment")
def edit_comment(thing_id: str, text: str) -> dict:
    """`POST /api/editusertext` against a `t1_` (comment) fullname.
    Only the comment author can edit."""
    parsed = _parse_fullname(thing_id)
    if not parsed or parsed[0] != "t1":
        return _api_error("thing_id", "NOT_AUTHOR",
                          "expected a t1_ comment fullname")
    with _lock():
        s = _load_state()
        c = s["comments"].get(parsed[1])
        if not c or not _comment_visible(c):
            _record(s, "edit_comment", thing_id=thing_id,
                    result="not_found")
            _save_state(s)
            return _api_error("thing_id", "NO_THING_ID",
                              "comment not found")
        me = _resolve_self(s)
        if c.get("author") != me:
            return _api_error("thing_id", "NOT_AUTHOR",
                              "not the author")
        c["body"] = text or ""
        c["body_html"] = ("<div class=\"md\"><p>" + (text or "")
                           + "</p></div>") if text else ""
        c["edited"] = _now_epoch()
        _record(s, "edit_comment", thing_id=thing_id)
        _save_state(s)
        return _api_ok({
            "things": [{"kind": "t1", "data": _public_thing(c)}],
        })


@mcp.tool(name="delete_comment")
def delete_comment(id: str) -> dict:  # noqa: A002
    """`POST /api/del` for a `t1_` comment fullname owned by self."""
    return _delete_thing(id)


@mcp.tool(name="list_comments")
def list_comments(postId: str,
                  subreddit: str | None = None,
                  sort: str = "best",
                  limit: int = 100,
                  depth: int | None = None) -> dict:
    """`GET /r/{subreddit}/comments/{post_id}`. Returns a Listing of
    `t1_` Things — nested `replies` are themselves Listings.
    `sort` ∈ {best, top, new, controversial, old, qa}."""
    with _lock():
        s = _load_state()
        post = s["posts"].get(postId)
        if not post or not _post_visible(post):
            _record(s, "list_comments", post_id=postId,
                    result="not_found")
            _save_state(s)
            return _listing([])
        if subreddit:
            name = _norm_sr_name(subreddit)
            if name and post.get("subreddit") != name:
                _record(s, "list_comments", post_id=postId,
                        result="subreddit_mismatch")
                _save_state(s)
                return _listing([])
        tree = _build_comment_tree(s, post["name"], sort=sort, depth=depth)
        if limit > 0:
            tree = tree[:limit]
        children = [_wrap("t1", c) for c in tree]
        _record(s, "list_comments", post_id=postId, sort=sort,
                count=len(children))
        _save_state(s)
        return _listing(children)


# ---------------------------------------------------------------------------
# Engagement / Saved
# ---------------------------------------------------------------------------

@mcp.tool(name="vote")
def vote(id: str, dir: int) -> dict:  # noqa: A002
    """`POST /api/vote`. `dir` ∈ {1 (upvote), 0 (clear), -1 (downvote)}.
    Adjusts the target Thing's score; idempotent for repeated identical
    directions."""
    parsed = _parse_fullname(id)
    if not parsed or parsed[0] not in ("t3", "t1"):
        return _api_error("id", "NO_THING_ID",
                          "id must be a t3_ or t1_ fullname")
    try:
        d = int(dir)
    except (TypeError, ValueError):
        return _api_error("dir", "INVALID_OPTION",
                          "dir must be -1, 0, or 1")
    if d not in (-1, 0, 1):
        return _api_error("dir", "INVALID_OPTION",
                          "dir must be -1, 0, or 1")
    with _lock():
        s = _load_state()
        me = _resolve_self(s)
        _ensure_user(s, me)
        kind, local = parsed
        target = (s["posts"].get(local) if kind == "t3"
                   else s["comments"].get(local))
        if not target:
            _record(s, "vote", id=id, result="not_found")
            _save_state(s)
            return _api_error("id", "NO_THING_ID", "thing not found")
        prev = s["votes"].setdefault(me, {}).get(id, 0)
        delta = d - prev
        target["score"] = int(target.get("score", 0)) + delta
        target["ups"] = max(0, int(target.get("ups", 0)) + max(0, delta))
        if d == 0:
            s["votes"][me].pop(id, None)
        else:
            s["votes"][me][id] = d
        _record(s, "vote", id=id, dir=d, delta=delta)
        _save_state(s)
        return {}


@mcp.tool(name="save")
def save(id: str) -> dict:  # noqa: A002
    """`POST /api/save`. Saves a post or comment to the authenticated
    user's saved list. Idempotent."""
    return _set_saved(id, saved=True)


@mcp.tool(name="unsave")
def unsave(id: str) -> dict:  # noqa: A002
    """`POST /api/unsave`. Removes a thing from the user's saved list.
    Idempotent."""
    return _set_saved(id, saved=False)


def _set_saved(thing_id: str, *, saved: bool) -> dict:
    parsed = _parse_fullname(thing_id)
    if not parsed or parsed[0] not in ("t3", "t1"):
        return _api_error("id", "NO_THING_ID",
                          "expected a t3_ or t1_ fullname")
    with _lock():
        s = _load_state()
        me = _resolve_self(s)
        bucket = s["saved"].setdefault(me, [])
        if saved and thing_id not in bucket:
            bucket.append(thing_id)
        elif not saved and thing_id in bucket:
            bucket.remove(thing_id)
        _record(s, "save" if saved else "unsave", id=thing_id)
        _save_state(s)
        return {}


@mcp.tool(name="list_saved")
def list_saved(username: str,
               limit: int = 25,
               after: str | None = None) -> dict:
    """`GET /user/{username}/saved`. Returns a Listing of the user's
    saved Things (posts + comments interleaved by save time)."""
    with _lock():
        s = _load_state()
        bucket = list(s["saved"].get(username, []))
        items: list[tuple[str, dict, str]] = []  # (kind, data, fullname)
        for fn in bucket:
            kind, data = _resolve_thing(s, fn)
            if kind and data:
                items.append((kind, data, fn))
        if limit <= 0:
            limit = 25
        if limit > 100:
            limit = 100
        start = 0
        if after:
            for i, (_, _, fn) in enumerate(items):
                if fn == after:
                    start = i + 1
                    break
        page = items[start:start + limit]
        children = [_wrap(kind, data) for kind, data, _ in page]
        next_after = (items[start + limit - 1][2]
                       if start + limit < len(items) and page else None)
        _record(s, "list_saved", username=username, count=len(children))
        _save_state(s)
        return _listing(children, after=next_after)


# ---------------------------------------------------------------------------
# Users / Identity
# ---------------------------------------------------------------------------

@mcp.tool(name="get_user_about")
def get_user_about(username: str) -> dict:
    """`GET /user/{username}/about`. Returns the User (`t2`) Thing."""
    with _lock():
        s = _load_state()
        u = s["users"].get(username)
        if not u:
            _record(s, "get_user_about", username=username,
                    result="not_found")
            _save_state(s)
            return {"error": 404, "message": "Not Found"}
        _record(s, "get_user_about", username=username)
        _save_state(s)
        return _wrap("t2", u)


@mcp.tool(name="list_user_posts")
def list_user_posts(username: str,
                    sort: str = "new",
                    limit: int = 25,
                    after: str | None = None) -> dict:
    """`GET /user/{username}/submitted`. Posts authored by `username`.
    `sort` ∈ {new, top, hot, controversial}."""
    with _lock():
        s = _load_state()
        posts = [p for p in s["posts"].values()
                 if p.get("author") == username and _post_visible(p)]
        posts = _sort_posts(posts, sort)
        page, next_after, _ = _paginate(
            posts, limit=limit, after=after, before=None,
            fullname_of=lambda x: x["name"],
        )
        children = [_wrap("t3", p) for p in page]
        _record(s, "list_user_posts", username=username, sort=sort,
                count=len(children))
        _save_state(s)
        return _listing(children, after=next_after)


@mcp.tool(name="list_user_comments")
def list_user_comments(username: str,
                       sort: str = "new",
                       limit: int = 25,
                       after: str | None = None) -> dict:
    """`GET /user/{username}/comments`. Comments authored by `username`,
    flat (not nested). `sort` ∈ {new, top, controversial, old}."""
    with _lock():
        s = _load_state()
        cs = [c for c in s["comments"].values()
              if c.get("author") == username and _comment_visible(c)]
        cs = _sort_comments(cs, sort)
        page, next_after, _ = _paginate(
            cs, limit=limit, after=after, before=None,
            fullname_of=lambda x: x["name"],
        )
        children = [_wrap("t1", c) for c in page]
        _record(s, "list_user_comments", username=username, sort=sort,
                count=len(children))
        _save_state(s)
        return _listing(children, after=next_after)


@mcp.tool(name="get_me")
def get_me() -> dict:
    """`GET /api/v1/me`. Returns the User Thing for the authenticated
    session (`self_user`). Unlike `/user/{name}/about`, this returns
    the raw data dict without the `t2` wrapper — matching the Reddit
    response shape for `/api/v1/me`."""
    with _lock():
        s = _load_state()
        me = _resolve_self(s)
        u = s["users"].get(me)
        if not u:
            u = _ensure_user(s, me)
            _save_state(s)
        _record(s, "get_me", username=me)
        _save_state(s)
        return _public_thing(u)


# ---------------------------------------------------------------------------
# Inbox / Messages
# ---------------------------------------------------------------------------

@mcp.tool(name="list_inbox")
def list_inbox(limit: int = 25,
               mark: bool = False) -> dict:
    """`GET /message/inbox`. Returns a Listing of `t4_` Message Things
    addressed to the authenticated user, newest first. When `mark` is
    true, all returned messages are marked read (matching the upstream
    auto-mark behavior)."""
    with _lock():
        s = _load_state()
        me = _resolve_self(s)
        msgs = [m for m in s["messages"].values()
                if m.get("dest") == me]
        msgs.sort(key=lambda m: m.get("created_utc", 0), reverse=True)
        if limit <= 0:
            limit = 25
        if limit > 100:
            limit = 100
        page = msgs[:limit]
        if mark:
            for m in page:
                m["new"] = False
        children = [_wrap("t4", m) for m in page]
        _record(s, "list_inbox", count=len(children), mark=bool(mark))
        _save_state(s)
        return _listing(children)


@mcp.tool(name="send_message")
def send_message(to: str, subject: str, text: str) -> dict:
    """`POST /api/compose`. Sends a private message from the
    authenticated user to `to` (username). Returns the standard
    `{json:{errors:[]}}` envelope."""
    if not to:
        return _api_error("to", "USER_REQUIRED",
                          "recipient is required")
    if not subject:
        return _api_error("subject", "NO_SUBJECT",
                          "subject is required")
    with _lock():
        s = _load_state()
        me = _resolve_self(s)
        _ensure_user(s, me)
        _ensure_user(s, to)
        msg = _new_message(
            s, author=me, dest=to, subject=subject, body=text or "",
        )
        s["messages"][msg["id"]] = msg
        _record(s, "send_message", to=to, message_id=msg["id"])
        _save_state(s)
        return _api_ok({"things": [{"kind": "t4",
                                      "data": _public_thing(msg)}]})


@mcp.tool(name="mark_message_read")
def mark_message_read(id: str) -> dict:  # noqa: A002
    """`POST /api/read_message`. Mark a single `t4_` message as read.
    Returns `{}` on success or an error envelope."""
    parsed = _parse_fullname(id)
    if not parsed or parsed[0] != "t4":
        return _api_error("id", "NO_THING_ID",
                          "expected a t4_ message fullname")
    with _lock():
        s = _load_state()
        m = s["messages"].get(parsed[1])
        if not m:
            _record(s, "mark_message_read", id=id, result="not_found")
            _save_state(s)
            return _api_error("id", "NO_THING_ID", "message not found")
        m["new"] = False
        _record(s, "mark_message_read", id=id)
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@mcp.tool(name="search_posts")
def search_posts(q: str,
                 subreddit: str | None = None,
                 sort: str = "relevance",
                 t: str = "all",
                 restrict_sr: bool = False,
                 limit: int = 25,
                 after: str | None = None) -> dict:
    """`GET /search` (or `GET /r/{subreddit}/search`). Free-text query
    across post title + selftext, AND-combined. Supports inline
    filters: `subreddit:<name>`, `author:<user>`, `flair:<text>`,
    `nsfw:yes|no`. `sort` ∈ {relevance, hot, top, new, comments}."""
    terms, filters = _parse_search_q(q or "")
    sr_name = _norm_sr_name(subreddit) if subreddit else ""
    if restrict_sr and sr_name:
        filters["subreddit"] = sr_name
    with _lock():
        s = _load_state()
        cutoff = _time_cutoff(t)
        posts = []
        for p in s["posts"].values():
            if not _post_visible(p):
                continue
            if cutoff is not None and p.get("created_utc", 0) < cutoff:
                continue
            if sr_name and not restrict_sr and p.get("subreddit") != sr_name:
                # `/r/<sr>/search` without restrict_sr still scopes to that sr
                continue
            if not _post_matches_search(p, terms, filters):
                continue
            posts.append(p)
        sort_lc = (sort or "relevance").lower()
        if sort_lc == "comments":
            posts.sort(key=lambda p: p.get("num_comments", 0),
                        reverse=True)
        elif sort_lc in ("hot", "top", "new"):
            posts = _sort_posts(posts, sort_lc, t)
        else:  # relevance — score by term hits in title + body
            def _rel(p: dict) -> int:
                hay = f"{p.get('title','')} {p.get('selftext','')}".lower()
                return sum(hay.count(t.lower()) for t in terms)
            posts.sort(key=lambda p: (-_rel(p),
                                       -p.get("score", 0),
                                       -p.get("created_utc", 0)))
        page, next_after, _ = _paginate(
            posts, limit=limit, after=after, before=None,
            fullname_of=lambda x: x["name"],
        )
        children = [_wrap("t3", p) for p in page]
        _record(s, "search_posts", q=q, subreddit=sr_name,
                sort=sort_lc, count=len(children))
        _save_state(s)
        return _listing(children, after=next_after)


# ---------------------------------------------------------------------------
# Moderation
# ---------------------------------------------------------------------------

@mcp.tool(name="get_modqueue")
def get_modqueue(subreddit: str,
                 limit: int = 25) -> dict:
    """`GET /r/{subreddit}/about/modqueue`. Returns a Listing of
    Things (posts + comments) that have been flagged for moderation
    review (in the mock: anything whose `_modqueue` flag is true,
    or that has been `remove()`d). Mod-only in real Reddit; the
    mock does not enforce mod status."""
    name = _norm_sr_name(subreddit)
    with _lock():
        s = _load_state()
        items: list[tuple[str, dict]] = []
        for p in s["posts"].values():
            if p.get("subreddit") != name:
                continue
            if p.get("_modqueue") or p.get("_removed"):
                items.append(("t3", p))
        for c in s["comments"].values():
            if c.get("subreddit") != name:
                continue
            if c.get("_modqueue") or c.get("_removed"):
                items.append(("t1", c))
        items.sort(key=lambda kv: kv[1].get("created_utc", 0), reverse=True)
        if limit <= 0:
            limit = 25
        if limit > 100:
            limit = 100
        items = items[:limit]
        children = [_wrap(k, d) for k, d in items]
        _record(s, "get_modqueue", subreddit=name, count=len(children))
        _save_state(s)
        return _listing(children)


@mcp.tool(name="approve")
def approve(id: str) -> dict:  # noqa: A002
    """`POST /api/approve`. Clear removal/modqueue flags on the target
    Thing. Mod-only on real Reddit; the mock does not enforce."""
    parsed = _parse_fullname(id)
    if not parsed or parsed[0] not in ("t3", "t1"):
        return _api_error("id", "NO_THING_ID",
                          "expected a t3_ or t1_ fullname")
    with _lock():
        s = _load_state()
        kind, local = parsed
        target = (s["posts"].get(local) if kind == "t3"
                   else s["comments"].get(local))
        if not target:
            _record(s, "approve", id=id, result="not_found")
            _save_state(s)
            return _api_error("id", "NO_THING_ID", "thing not found")
        target["_removed"] = False
        target["_modqueue"] = False
        target["_approved"] = True
        target["removed_by_category"] = None
        _record(s, "approve", id=id)
        _save_state(s)
        return {}


@mcp.tool(name="remove")
def remove(id: str, spam: bool = False) -> dict:  # noqa: A002
    """`POST /api/remove`. Mark the target Thing as removed (and
    optionally as spam). Removed Things stop appearing in listings,
    show up in `get_modqueue`, and can be reinstated via `approve`."""
    parsed = _parse_fullname(id)
    if not parsed or parsed[0] not in ("t3", "t1"):
        return _api_error("id", "NO_THING_ID",
                          "expected a t3_ or t1_ fullname")
    with _lock():
        s = _load_state()
        kind, local = parsed
        target = (s["posts"].get(local) if kind == "t3"
                   else s["comments"].get(local))
        if not target:
            _record(s, "remove", id=id, result="not_found")
            _save_state(s)
            return _api_error("id", "NO_THING_ID", "thing not found")
        target["_removed"] = True
        target["_modqueue"] = True
        target["_approved"] = False
        target["removed_by_category"] = "moderator" if not spam else "spam"
        _record(s, "remove", id=id, spam=bool(spam))
        _save_state(s)
        return {}


# ---------------------------------------------------------------------------
# Mock-only debug helpers
# ---------------------------------------------------------------------------

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state for verifier
    introspection. Not part of the Reddit API."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed_subreddit")
def mock_debug_seed_subreddit(display_name: str,
                              title: str | None = None,
                              public_description: str = "",
                              description: str = "",
                              subscribers: int = 0,
                              subreddit_type: str = "public",
                              over18: bool = False,
                              created_utc: float | None = None) -> dict:
    """Mock-only: insert (or overwrite) a Subreddit fixture."""
    name = _norm_sr_name(display_name)
    with _lock():
        s = _load_state()
        sr = _new_subreddit(
            s, display_name=name, title=title,
            public_description=public_description,
            description=description, subscribers=subscribers,
            subreddit_type=subreddit_type, over18=over18,
            created_utc=created_utc,
        )
        s["subreddits"][name] = sr
        _record(s, "debug_seed_subreddit", display_name=name)
        _save_state(s)
        return _wrap("t5", sr)


@mcp.tool(name="mock_debug_seed_user")
def mock_debug_seed_user(name: str,
                         link_karma: int = 0,
                         comment_karma: int = 0,
                         created_utc: float | None = None,
                         is_mod: bool = False,
                         is_gold: bool = False) -> dict:
    """Mock-only: insert (or overwrite) a User fixture."""
    with _lock():
        s = _load_state()
        u = _new_user(s, name=name, link_karma=link_karma,
                       comment_karma=comment_karma,
                       created_utc=created_utc,
                       is_mod=is_mod, is_gold=is_gold)
        s["users"][name] = u
        _record(s, "debug_seed_user", name=name)
        _save_state(s)
        return _wrap("t2", u)


@mcp.tool(name="mock_debug_seed_post")
def mock_debug_seed_post(subreddit: str,
                         author: str,
                         title: str,
                         selftext: str = "",
                         url: str | None = None,
                         is_self: bool = True,
                         score: int = 1,
                         num_comments: int = 0,
                         created_utc: float | None = None,
                         nsfw: bool = False,
                         spoiler: bool = False,
                         flair_text: str | None = None,
                         post_id: str | None = None) -> dict:
    """Mock-only: insert a Post fixture."""
    name = _norm_sr_name(subreddit)
    with _lock():
        s = _load_state()
        if name not in s["subreddits"]:
            s["subreddits"][name] = _new_subreddit(s, display_name=name)
        if author not in s["users"]:
            s["users"][author] = _new_user(s, name=author)
        post = _new_post(
            s, post_id=post_id, subreddit=name, author=author,
            title=title, selftext=selftext, url=url,
            is_self=is_self, score=score, num_comments=num_comments,
            created_utc=created_utc, nsfw=nsfw, spoiler=spoiler,
            flair_text=flair_text,
            kind="self" if is_self else "link",
        )
        s["posts"][post["id"]] = post
        _record(s, "debug_seed_post", post_id=post["id"])
        _save_state(s)
        return _wrap("t3", post)


@mcp.tool(name="mock_debug_seed_comment")
def mock_debug_seed_comment(link_id: str,
                            parent_id: str,
                            author: str,
                            body: str,
                            score: int = 1,
                            created_utc: float | None = None,
                            depth: int = 0,
                            subreddit: str = "",
                            comment_id: str | None = None) -> dict:
    """Mock-only: insert a Comment fixture. `link_id` and `parent_id`
    are Thing fullnames (e.g. `t3_abc`, `t1_xyz`)."""
    with _lock():
        s = _load_state()
        if author not in s["users"]:
            s["users"][author] = _new_user(s, name=author)
        c = _new_comment(
            s, comment_id=comment_id, link_id=link_id,
            parent_id=parent_id, author=author, body=body,
            score=score, created_utc=created_utc, depth=depth,
            subreddit=subreddit,
        )
        s["comments"][c["id"]] = c
        # bump parent post's num_comments if link_id resolves
        parsed = _parse_fullname(link_id)
        if parsed and parsed[1] in s["posts"]:
            p = s["posts"][parsed[1]]
            p["num_comments"] = int(p.get("num_comments", 0)) + 1
        _record(s, "debug_seed_comment", comment_id=c["id"])
        _save_state(s)
        return _wrap("t1", c)


@mcp.tool(name="mock_debug_seed_message")
def mock_debug_seed_message(author: str,
                            dest: str,
                            subject: str,
                            body: str,
                            created_utc: float | None = None,
                            new: bool = True,
                            message_id: str | None = None) -> dict:
    """Mock-only: insert a Message fixture into the destination
    user's inbox."""
    with _lock():
        s = _load_state()
        if author not in s["users"]:
            s["users"][author] = _new_user(s, name=author)
        if dest not in s["users"]:
            s["users"][dest] = _new_user(s, name=dest)
        m = _new_message(
            s, message_id=message_id, author=author, dest=dest,
            subject=subject, body=body, created_utc=created_utc, new=new,
        )
        s["messages"][m["id"]] = m
        _record(s, "debug_seed_message", message_id=m["id"])
        _save_state(s)
        return _wrap("t4", m)


@mcp.tool(name="mock_debug_seed_subscription")
def mock_debug_seed_subscription(user: str, subreddit: str) -> dict:
    """Mock-only: register a subscription edge directly, bypassing
    `subscribe_subreddit` (no subscriber-count adjustment)."""
    name = _norm_sr_name(subreddit)
    with _lock():
        s = _load_state()
        if user not in s["users"]:
            s["users"][user] = _new_user(s, name=user)
        if name not in s["subreddits"]:
            s["subreddits"][name] = _new_subreddit(s, display_name=name)
        edge = {"user": user, "subreddit": name}
        if edge not in s["subscriptions"]:
            s["subscriptions"].append(edge)
        _record(s, "debug_seed_subscription", user=user, subreddit=name)
        _save_state(s)
        return {"ok": True, "user": user, "subreddit": name}


@mcp.tool(name="mock_debug_set_self_user")
def mock_debug_set_self_user(username: str) -> dict:
    """Mock-only: change the username the mock treats as the
    authenticated session. Used by per-task seeders to align the
    `self_user` with a workflow's protagonist."""
    with _lock():
        s = _load_state()
        if username not in s["users"]:
            s["users"][username] = _new_user(s, name=username)
        s["self_user"] = username
        _record(s, "debug_set_self_user", username=username)
        _save_state(s)
        return {"ok": True, "self_user": username}


if __name__ == "__main__":
    mcp.run()
