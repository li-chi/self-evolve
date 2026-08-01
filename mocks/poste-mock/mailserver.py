#!/usr/bin/env python3
"""A self-contained mail server standing in for Toolathlon's poste.io.

Toolathlon's `emails` tasks talk to a local poste.io instance on
localhost:1587 (SMTP) and localhost:1143 (IMAP). Three different clients
speak to it and none of them may change:

  * the agent, through upstream's real `emails-mcp` server (IMAP + SMTP)
  * upstream preprocess, through `utils.app_specific.poste.LocalEmailManager`
    (smtplib + imaplib)
  * upstream graders, through `imaplib` directly

So the substitution happens at the protocol level rather than in a client
shim: this process speaks enough SMTP and IMAP4rev1 for those clients, and
stores mail as JSON under $MAIL_STATE_DIR/<user>/<folder>.json.

    mailserver.py [--smtp-port 1587] [--imap-port 1143]
                  [--state-dir /var/lib/mock-state/mail]
                  [--users configs/users_data.json] [--domain mcp.com]

Accounts come from Toolathlon's own users_data.json, so every address a
task references already exists with the password the task config expects.
Any address at the served domain is accepted for delivery, and unknown
local recipients get a mailbox on first delivery — the same practical
behaviour as the catch-all poste.io deployment upstream uses.

Not implemented (nothing in Toolathlon uses it): TLS, IDLE, quotas,
partial FETCH body sections beyond the ones listed in _fetch_item, server
side threading/sorting.
"""

from __future__ import annotations

import argparse
import base64
import email
import email.policy
import email.utils
import json
import os
import re
import socket
import socketserver
import threading
import time
from email.message import EmailMessage

STATE_DIR = "/var/lib/mock-state/mail"
DOMAIN = "mcp.com"
USERS: dict = {}          # address -> {"password": str, "name": str}
_LOCK = threading.RLock()

DEFAULT_FOLDERS = ["INBOX", "Sent", "Drafts", "Junk", "Trash"]
# poste.io / Dovecot expose the special folders with these names; clients
# also use the "INBOX.Sent" form, which _canon_folder maps onto the same box.
_FOLDER_ALIASES = {
    "sent": "Sent", "sent items": "Sent", "sent messages": "Sent",
    "inbox.sent": "Sent", "drafts": "Drafts", "inbox.drafts": "Drafts",
    "junk": "Junk", "spam": "Junk", "inbox.junk": "Junk",
    "trash": "Trash", "deleted messages": "Trash", "inbox.trash": "Trash",
    "inbox": "INBOX",
}


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _canon_folder(name: str) -> str:
    name = (name or "INBOX").strip().strip('"')
    return _FOLDER_ALIASES.get(name.lower(), name)


def _user_dir(address: str) -> str:
    path = os.path.join(STATE_DIR, address.lower())
    os.makedirs(path, exist_ok=True)
    return path


def _folder_path(address: str, folder: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", _canon_folder(folder))
    return os.path.join(_user_dir(address), f"{safe}.json")


def load_folder(address: str, folder: str) -> list:
    path = _folder_path(address, folder)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_folder(address: str, folder: str, messages: list) -> None:
    path = _folder_path(address, folder)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(messages, f, indent=1)
    os.replace(tmp, path)


def list_folders(address: str) -> list:
    d = _user_dir(address)
    have = [os.path.splitext(f)[0] for f in os.listdir(d)
            if f.endswith(".json")]
    return sorted(set(DEFAULT_FOLDERS) | set(have))


def next_uid(address: str) -> int:
    path = os.path.join(_user_dir(address), ".uidnext")
    uid = 1
    if os.path.exists(path):
        uid = int(open(path).read().strip() or 1)
    with open(path, "w") as f:
        f.write(str(uid + 1))
    return uid


def deliver(address: str, raw: bytes, folder: str = "INBOX",
            flags: list = None) -> int:
    """Append a message to a mailbox and return its UID."""
    with _LOCK:
        msgs = load_folder(address, folder)
        uid = next_uid(address)
        msgs.append({
            "uid": uid,
            "flags": list(flags or []),
            "internaldate": email.utils.formatdate(localtime=True),
            "raw": base64.b64encode(raw).decode("ascii"),
        })
        save_folder(address, folder, msgs)
        return uid


def raw_of(msg: dict) -> bytes:
    return base64.b64decode(msg["raw"])


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------

class SMTPHandler(socketserver.StreamRequestHandler):
    timeout = 300

    def _send(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())
        self.wfile.flush()

    def handle(self) -> None:
        self._send("220 mock-poste ESMTP ready")
        mail_from, rcpt = None, []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").strip()
            upper = line.upper()

            if upper.startswith(("HELO", "EHLO")):
                host = line.split(" ", 1)[1] if " " in line else "localhost"
                if upper.startswith("EHLO"):
                    self._send(f"250-mock-poste greets {host}")
                    self._send("250-AUTH PLAIN LOGIN")
                    self._send("250-8BITMIME")
                    self._send("250 SMTPUTF8")
                else:
                    self._send("250 mock-poste")
            elif upper.startswith("AUTH LOGIN"):
                self._send("334 VXNlcm5hbWU6")
                self.rfile.readline()
                self._send("334 UGFzc3dvcmQ6")
                self.rfile.readline()
                self._send("235 2.7.0 Authentication successful")
            elif upper.startswith("AUTH PLAIN"):
                if len(line.split()) == 2:
                    self._send("334 ")
                    self.rfile.readline()
                self._send("235 2.7.0 Authentication successful")
            elif upper.startswith("MAIL FROM"):
                mail_from = _addr(line)
                rcpt = []
                self._send("250 2.1.0 Ok")
            elif upper.startswith("RCPT TO"):
                rcpt.append(_addr(line))
                self._send("250 2.1.5 Ok")
            elif upper == "DATA":
                self._send("354 End data with <CR><LF>.<CR><LF>")
                body = self._read_data()
                self._deliver(mail_from, rcpt, body)
                self._send("250 2.0.0 Ok: queued")
            elif upper == "RSET":
                mail_from, rcpt = None, []
                self._send("250 2.0.0 Ok")
            elif upper == "NOOP":
                self._send("250 2.0.0 Ok")
            elif upper == "QUIT":
                self._send("221 2.0.0 Bye")
                return
            else:
                self._send("250 2.0.0 Ok")

    def _read_data(self) -> bytes:
        chunks = []
        while True:
            line = self.rfile.readline()
            if not line or line in (b".\r\n", b".\n"):
                break
            if line.startswith(b".."):
                line = line[1:]
            chunks.append(line)
        return b"".join(chunks)

    @staticmethod
    def _stamp(body: bytes) -> bytes:
        """Add the headers an MTA adds on delivery when the client omitted
        them (poste.io does this; clients sort on Date)."""
        try:
            msg = email.message_from_bytes(body, policy=email.policy.default)
        except Exception:  # noqa: BLE001
            return body
        changed = False
        if not msg.get("Date"):
            msg["Date"] = email.utils.formatdate(localtime=True)
            changed = True
        if not msg.get("Message-ID"):
            msg["Message-ID"] = email.utils.make_msgid(domain=DOMAIN)
            changed = True
        return msg.as_bytes() if changed else body

    def _deliver(self, mail_from: str, rcpt: list, body: bytes) -> None:
        body = self._stamp(body)
        for address in rcpt:
            if address.lower().endswith("@" + DOMAIN) or address.lower() in USERS:
                deliver(address.lower(), body, "INBOX")
        # Keep the sender's own copy where poste.io puts it, so graders that
        # read the sender's Sent folder see what the agent sent.
        if mail_from and (mail_from.lower() in USERS
                          or mail_from.lower().endswith("@" + DOMAIN)):
            deliver(mail_from.lower(), body, "Sent", flags=["\\Seen"])


def _addr(line: str) -> str:
    m = re.search(r"<([^>]*)>", line)
    if m:
        return m.group(1).strip()
    return line.split(":", 1)[1].strip() if ":" in line else line.strip()


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

class IMAPHandler(socketserver.StreamRequestHandler):
    timeout = 300

    def setup(self):
        super().setup()
        self.user = None
        self.folder = None
        self.readonly = False

    def _send(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())
        self.wfile.flush()

    def handle(self) -> None:
        self._send("* OK [CAPABILITY IMAP4rev1 AUTH=PLAIN LOGIN] mock-poste "
                   "ready")
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            try:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
            except Exception:  # noqa: BLE001
                continue
            if not line.strip():
                continue
            tag, _, rest = line.partition(" ")
            cmd, _, args = rest.partition(" ")
            cmd = cmd.upper()
            try:
                if not self._dispatch(tag, cmd, args):
                    return
            except Exception as e:  # noqa: BLE001 - never drop the connection
                self._send(f"{tag} NO {cmd} failed: {e}")

    def _dispatch(self, tag: str, cmd: str, args: str) -> bool:
        if cmd == "CAPABILITY":
            self._send("* CAPABILITY IMAP4rev1 AUTH=PLAIN LOGIN")
            self._send(f"{tag} OK CAPABILITY completed")
        elif cmd == "LOGIN":
            self._login(tag, args)
        elif cmd == "AUTHENTICATE":
            self._send("+ ")
            payload = self.rfile.readline().strip()
            try:
                parts = base64.b64decode(payload).split(b"\x00")
                self.user = parts[1].decode().lower()
            except Exception:  # noqa: BLE001
                self._send(f"{tag} NO AUTHENTICATE failed")
                return True
            self._send(f"{tag} OK AUTHENTICATE completed")
        elif cmd == "LOGOUT":
            self._send("* BYE mock-poste logging out")
            self._send(f"{tag} OK LOGOUT completed")
            return False
        elif cmd == "NOOP":
            self._send(f"{tag} OK NOOP completed")
        elif cmd == "LIST" or cmd == "LSUB":
            for f in list_folders(self.user or ""):
                attrs = r"\HasNoChildren"
                self._send(f'* {cmd} ({attrs}) "." "{f}"')
            self._send(f"{tag} OK {cmd} completed")
        elif cmd in ("SELECT", "EXAMINE"):
            self._select(tag, args, readonly=(cmd == "EXAMINE"))
        elif cmd == "CREATE":
            folder = _canon_folder(args)
            if not os.path.exists(_folder_path(self.user, folder)):
                save_folder(self.user, folder, [])
            self._send(f"{tag} OK CREATE completed")
        elif cmd == "DELETE":
            path = _folder_path(self.user, args)
            if os.path.exists(path):
                os.remove(path)
            self._send(f"{tag} OK DELETE completed")
        elif cmd == "STATUS":
            self._status(tag, args)
        elif cmd == "APPEND":
            self._append(tag, args)
        elif cmd == "SEARCH":
            self._search(tag, args, uid_mode=False)
        elif cmd == "FETCH":
            self._fetch(tag, args, uid_mode=False)
        elif cmd == "STORE":
            self._store(tag, args, uid_mode=False)
        elif cmd == "COPY":
            self._copy(tag, args, uid_mode=False)
        elif cmd == "UID":
            sub, _, rest = args.partition(" ")
            sub = sub.upper()
            if sub == "SEARCH":
                self._search(tag, rest, uid_mode=True)
            elif sub == "FETCH":
                self._fetch(tag, rest, uid_mode=True)
            elif sub == "STORE":
                self._store(tag, rest, uid_mode=True)
            elif sub == "COPY":
                self._copy(tag, rest, uid_mode=True)
            else:
                self._send(f"{tag} BAD unsupported UID {sub}")
        elif cmd == "EXPUNGE":
            self._expunge(tag)
        elif cmd == "CLOSE":
            if self.folder and not self.readonly:
                self._purge_deleted()
            self.folder = None
            self._send(f"{tag} OK CLOSE completed")
        elif cmd == "STARTTLS":
            self._send(f"{tag} NO TLS not supported by the mock server")
        else:
            self._send(f"{tag} BAD unsupported command {cmd}")
        return True

    # -- commands ---------------------------------------------------------

    def _login(self, tag: str, args: str) -> None:
        parts = _split_atoms(args)
        if len(parts) < 2:
            self._send(f"{tag} BAD LOGIN needs a user and a password")
            return
        user, password = parts[0].lower(), parts[1]
        known = USERS.get(user)
        if known is not None and known.get("password") not in (None, password):
            self._send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials")
            return
        if known is None and not user.endswith("@" + DOMAIN):
            self._send(f"{tag} NO [AUTHENTICATIONFAILED] No such user")
            return
        self.user = user
        _user_dir(user)
        self._send(f"{tag} OK LOGIN completed")

    def _select(self, tag: str, args: str, readonly: bool) -> None:
        self.folder = _canon_folder(_split_atoms(args)[0])
        self.readonly = readonly
        msgs = load_folder(self.user, self.folder)
        unseen = [i + 1 for i, m in enumerate(msgs)
                  if "\\Seen" not in m["flags"]]
        self._send(f"* {len(msgs)} EXISTS")
        self._send(f"* {len(unseen)} RECENT")
        self._send(r"* FLAGS (\Answered \Flagged \Deleted \Seen \Draft)")
        self._send(r"* OK [PERMANENTFLAGS (\Answered \Flagged \Deleted "
                   r"\Seen \Draft)] Limited")
        if unseen:
            self._send(f"* OK [UNSEEN {unseen[0]}] First unseen")
        self._send("* OK [UIDVALIDITY 1] UIDs valid")
        state = "READ-ONLY" if readonly else "READ-WRITE"
        self._send(f"{tag} OK [{state}] SELECT completed")

    def _status(self, tag: str, args: str) -> None:
        parts = _split_atoms(args)
        folder = _canon_folder(parts[0])
        msgs = load_folder(self.user, folder)
        unseen = sum(1 for m in msgs if "\\Seen" not in m["flags"])
        self._send(f'* STATUS "{folder}" (MESSAGES {len(msgs)} '
                   f"UNSEEN {unseen} UIDNEXT {len(msgs) + 1})")
        self._send(f"{tag} OK STATUS completed")

    def _append(self, tag: str, args: str) -> None:
        m = re.match(r'\s*("?[^"{]+"?)\s*(\([^)]*\))?\s*("[^"]*")?\s*\{(\d+)\+?\}',
                     args)
        if not m:
            self._send(f"{tag} BAD APPEND syntax")
            return
        folder = _canon_folder(m.group(1))
        flags = (m.group(2) or "").strip("()").split()
        size = int(m.group(4))
        self._send("+ Ready for literal data")
        data = self.rfile.read(size)
        self.rfile.readline()  # trailing CRLF
        deliver(self.user, data, folder, flags)
        self._send(f"{tag} OK [APPENDUID 1 1] APPEND completed")

    def _search(self, tag: str, args: str, uid_mode: bool) -> None:
        msgs = load_folder(self.user, self.folder or "INBOX")
        crit = _parse_search(args)
        hits = []
        for idx, msg in enumerate(msgs, start=1):
            if _match_search(msg, crit):
                hits.append(msg["uid"] if uid_mode else idx)
        self._send("* SEARCH" + ("".join(f" {h}" for h in hits)))
        self._send(f"{tag} OK SEARCH completed")

    def _resolve(self, seq: str, uid_mode: bool) -> list:
        """Map a sequence/UID set onto (index, message) pairs."""
        msgs = load_folder(self.user, self.folder or "INBOX")
        keys = [m["uid"] for m in msgs] if uid_mode \
            else list(range(1, len(msgs) + 1))
        wanted = _parse_set(seq, max(keys) if keys else 0)
        return [(i + 1, m) for i, (m, k) in enumerate(zip(msgs, keys))
                if k in wanted]

    def _fetch(self, tag: str, args: str, uid_mode: bool) -> None:
        seq, _, items = args.partition(" ")
        for idx, msg in self._resolve(seq, uid_mode):
            parts = _fetch_parts(msg, items, uid_mode)
            self.wfile.write(f"* {idx} FETCH (".encode())
            for i, part in enumerate(parts):
                if i:
                    self.wfile.write(b" ")
                if part[0] == "atom":
                    self.wfile.write(part[1].encode())
                else:
                    _, label, data = part
                    self.wfile.write(f"{label} {{{len(data)}}}\r\n".encode())
                    self.wfile.write(data)
            self.wfile.write(b")\r\n")
            self.wfile.flush()
        self._send(f"{tag} OK FETCH completed")

    def _store(self, tag: str, args: str, uid_mode: bool) -> None:
        parts = args.split(" ", 2)
        seq, item, value = parts[0], parts[1].upper(), parts[2]
        flags = value.strip("()").split()
        msgs = load_folder(self.user, self.folder or "INBOX")
        keys = [m["uid"] for m in msgs] if uid_mode \
            else list(range(1, len(msgs) + 1))
        wanted = _parse_set(seq, max(keys) if keys else 0)
        for m, k in zip(msgs, keys):
            if k not in wanted:
                continue
            cur = set(m["flags"])
            if item.startswith("+FLAGS"):
                cur |= set(flags)
            elif item.startswith("-FLAGS"):
                cur -= set(flags)
            else:
                cur = set(flags)
            m["flags"] = sorted(cur)
        save_folder(self.user, self.folder or "INBOX", msgs)
        self._send(f"{tag} OK STORE completed")

    def _copy(self, tag: str, args: str, uid_mode: bool) -> None:
        seq, _, target = args.partition(" ")
        target = _canon_folder(target)
        for _idx, msg in self._resolve(seq, uid_mode):
            deliver(self.user, raw_of(msg), target, msg["flags"])
        self._send(f"{tag} OK COPY completed")

    def _purge_deleted(self) -> None:
        msgs = load_folder(self.user, self.folder)
        save_folder(self.user, self.folder,
                    [m for m in msgs if "\\Deleted" not in m["flags"]])

    def _expunge(self, tag: str) -> None:
        msgs = load_folder(self.user, self.folder or "INBOX")
        kept, removed = [], []
        for i, m in enumerate(msgs, start=1):
            (removed if "\\Deleted" in m["flags"] else kept).append((i, m))
        for offset, (i, _m) in enumerate(removed):
            self._send(f"* {i - offset} EXPUNGE")
        save_folder(self.user, self.folder or "INBOX", [m for _i, m in kept])
        self._send(f"{tag} OK EXPUNGE completed")


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------

def _split_atoms(s: str) -> list:
    return [a.strip('"') for a in re.findall(r'"[^"]*"|\S+', s or "")]


def _parse_set(spec: str, highest: int) -> set:
    out = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            a, b = part.split(":", 1)
            a = highest if a == "*" else int(a)
            b = highest if b == "*" else int(b)
            out.update(range(min(a, b), max(a, b) + 1))
        else:
            out.add(highest if part == "*" else int(part))
    return out


_SEARCH_WITH_ARG = {"FROM", "TO", "CC", "BCC", "SUBJECT", "BODY", "TEXT",
                    "HEADER", "SINCE", "BEFORE", "ON", "LARGER", "SMALLER",
                    "KEYWORD", "UID"}


def _parse_search(args: str) -> list:
    tokens = re.findall(r'"[^"]*"|\S+', args or "")
    crit, i = [], 0
    while i < len(tokens):
        t = tokens[i].strip('"').upper()
        if t == "CHARSET":
            i += 2
            continue
        if t == "HEADER" and i + 2 < len(tokens):
            crit.append(("HEADER", tokens[i + 1].strip('"'),
                         tokens[i + 2].strip('"')))
            i += 3
        elif t in _SEARCH_WITH_ARG and i + 1 < len(tokens):
            crit.append((t, tokens[i + 1].strip('"')))
            i += 2
        else:
            crit.append((t,))
            i += 1
    return crit


def _msg_of(m: dict):
    return email.message_from_bytes(raw_of(m), policy=email.policy.default)


def _body_text(msg) -> str:
    try:
        if msg.is_multipart():
            out = []
            for part in msg.walk():
                if part.get_content_maintype() == "text":
                    out.append(part.get_content())
            return "\n".join(out)
        return msg.get_content() if msg.get_content_maintype() == "text" else ""
    except Exception:  # noqa: BLE001
        return ""


def _match_search(m: dict, crit: list) -> bool:
    if not crit:
        return True
    msg = None
    for c in crit:
        key = c[0]
        if key in ("ALL",):
            continue
        if key == "UNSEEN":
            if "\\Seen" in m["flags"]:
                return False
            continue
        if key == "SEEN":
            if "\\Seen" not in m["flags"]:
                return False
            continue
        if key == "DELETED":
            if "\\Deleted" not in m["flags"]:
                return False
            continue
        if key == "UNDELETED":
            if "\\Deleted" in m["flags"]:
                return False
            continue
        if key == "FLAGGED":
            if "\\Flagged" not in m["flags"]:
                return False
            continue
        if key == "UID":
            if m["uid"] not in _parse_set(c[1], m["uid"]):
                return False
            continue
        if msg is None:
            msg = _msg_of(m)
        if key in ("FROM", "TO", "CC", "BCC", "SUBJECT"):
            if c[1].lower() not in str(msg.get(key.capitalize(), "")).lower():
                return False
        elif key == "HEADER":
            if c[2].lower() not in str(msg.get(c[1], "")).lower():
                return False
        elif key in ("BODY", "TEXT"):
            haystack = _body_text(msg).lower()
            if key == "TEXT":
                haystack += str(msg).lower()
            if c[1].lower() not in haystack:
                return False
        elif key in ("SINCE", "BEFORE", "ON"):
            when = email.utils.parsedate_tz(m["internaldate"])
            target = email.utils.parsedate_tz(c[1]) or \
                time.strptime(c[1], "%d-%b-%Y")
            if not when:
                continue
            a = time.mktime(tuple(when[:9]))
            b = time.mktime(tuple(target[:9]) if isinstance(target, tuple)
                            else target)
            if key == "SINCE" and a < b:
                return False
            if key == "BEFORE" and a >= b:
                return False
            if key == "ON" and abs(a - b) > 86400:
                return False
    return True


_SECTION_RE = re.compile(r"BODY(?:\.PEEK)?\[([^\]]*)\]", re.IGNORECASE)


def _header_block(raw: bytes) -> bytes:
    for sep in (b"\r\n\r\n", b"\n\n"):
        if sep in raw:
            return raw.split(sep, 1)[0] + b"\r\n\r\n"
    return raw + b"\r\n\r\n"


def _body_block(raw: bytes) -> bytes:
    for sep in (b"\r\n\r\n", b"\n\n"):
        if sep in raw:
            return raw.split(sep, 1)[1]
    return b""


def _header_fields(raw: bytes, names: list, exclude: bool) -> bytes:
    wanted = {n.lower() for n in names}
    out = []
    for line in _header_block(raw).replace(b"\r\n", b"\n").split(b"\n"):
        if line[:1] in (b" ", b"\t"):          # folded continuation
            if out:
                out.append(line)
            continue
        name = line.split(b":", 1)[0].decode("latin-1").strip().lower()
        keep = (name not in wanted) if exclude else (name in wanted)
        out.append(line) if keep else out.append(None)
        if out and out[-1] is None:
            out.pop()
            out.append(b"\x00SKIP")
    kept, skipping = [], False
    for line in out:
        if line == b"\x00SKIP":
            skipping = True
            continue
        if line[:1] in (b" ", b"\t"):
            if not skipping:
                kept.append(line)
            continue
        skipping = False
        kept.append(line)
    return b"\r\n".join(l for l in kept if l) + b"\r\n\r\n"


def _fetch_parts(m: dict, items: str, uid_mode: bool) -> list:
    """Build the response parts for one FETCH, in the requested order.

    Each part is ("atom", text) or ("literal", label, bytes). Matching the
    real server matters here: a client asking for RFC822.SIZE must not be
    handed a whole-message literal, and the section label is echoed back
    exactly as requested (clients key off it).
    """
    items_u = (items or "").upper()
    raw = raw_of(m)
    parts = []

    if uid_mode or re.search(r"\bUID\b", items_u):
        parts.append(("atom", f"UID {m['uid']}"))
    if re.search(r"\bFLAGS\b", items_u):
        parts.append(("atom", f"FLAGS ({' '.join(m['flags'])})"))
    if re.search(r"\bINTERNALDATE\b", items_u):
        parts.append(("atom", f'INTERNALDATE "{m["internaldate"]}"'))
    if re.search(r"\bRFC822\.SIZE\b", items_u):
        parts.append(("atom", f"RFC822.SIZE {len(raw)}"))

    if re.search(r"\bRFC822\.HEADER\b", items_u):
        parts.append(("literal", "RFC822.HEADER", _header_block(raw)))
    if re.search(r"\bRFC822\.TEXT\b", items_u):
        parts.append(("literal", "RFC822.TEXT", _body_block(raw)))
    if re.search(r"\bRFC822\b(?!\.)", items_u):
        parts.append(("literal", "RFC822", raw))

    for m_sec in _SECTION_RE.finditer(items or ""):
        section = m_sec.group(1).strip()
        label = f"BODY[{section}]"
        up = section.upper()
        if not section:
            data = raw
        elif up == "HEADER":
            data = _header_block(raw)
        elif up == "TEXT":
            data = _body_block(raw)
        elif up.startswith("HEADER.FIELDS"):
            names = re.findall(r"[A-Za-z0-9-]+", section.split("(", 1)[1]) \
                if "(" in section else []
            data = _header_fields(raw, names, exclude=".NOT" in up)
        else:
            data = raw          # unmodelled section: give the whole message
        parts.append(("literal", label, data))

    if not parts:
        parts.append(("atom", f"UID {m['uid']}"))
    return parts


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def load_users(path: str, domain: str) -> dict:
    users = {}
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for u in data.get("users", []):
            users[u["email"].lower()] = {"password": u.get("password"),
                                         "name": u.get("full_name")}
    return users


def main() -> None:
    global STATE_DIR, DOMAIN, USERS
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smtp-port", type=int, default=1587)
    ap.add_argument("--imap-port", type=int, default=1143)
    ap.add_argument("--state-dir", default=STATE_DIR)
    ap.add_argument("--users", default="/opt/toolathlon/configs/users_data.json")
    ap.add_argument("--domain", default=DOMAIN)
    ap.add_argument("--ready-file", default="")
    args = ap.parse_args()

    STATE_DIR = args.state_dir
    DOMAIN = args.domain
    os.makedirs(STATE_DIR, exist_ok=True)
    USERS = load_users(args.users, DOMAIN)

    smtp = _Server(("127.0.0.1", args.smtp_port), SMTPHandler)
    imap = _Server(("127.0.0.1", args.imap_port), IMAPHandler)
    threading.Thread(target=smtp.serve_forever, daemon=True).start()
    threading.Thread(target=imap.serve_forever, daemon=True).start()
    print(f"[mock-poste] SMTP :{args.smtp_port} IMAP :{args.imap_port} "
          f"state={STATE_DIR} users={len(USERS)}", flush=True)
    if args.ready_file:
        with open(args.ready_file, "w") as f:
            f.write("ready")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
