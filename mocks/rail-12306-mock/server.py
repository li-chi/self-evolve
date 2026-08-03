"""12306 mock MCP server.

Mirrors the tool surface of the npm package `12306-mcp`
(github.com/lockon-n/12306-mcp, the Toolathlon fork of Joooook's
upstream). The real server proxies kyfw.12306.cn and CAN PLACE ACTUAL
RAIL BOOKINGS — this mock is what RL training MUST use.

Tool surface (verbatim names + parameters from upstream `src/index.ts`):

  get-current-date          ()
  get-stations-code-in-city (city)
  get-station-code-of-citys (citys)            # | -separated
  get-station-code-by-names (stationNames)     # | -separated
  get-station-by-telecode   (stationTelecode)
  get-tickets               (date, fromStation, toStation,
                             trainFilterFlags='', earliestStartTime=0,
                             latestStartTime=24, sortFlag='',
                             sortReverse=False, limitedNum=0,
                             csvFormat=False)
  get-interline-tickets     (date, fromStation, toStation,
                             middleStation='', showWZ=False,
                             trainFilterFlags='', earliestStartTime=0,
                             latestStartTime=24, sortFlag='',
                             sortReverse=False, limitedNum=10)
  get-train-route-stations  (trainNo, fromStationTelecode,
                             toStationTelecode, departDate)

Plus mock-only debug helpers `mock_debug_state` and
`mock_debug_seed`.

`get-tickets` returns the upstream's exact human-readable text format
because the Toolathlon `train-ticket-plan` evaluator parses it with
regex:

    车次 | 出发站 -> 到达站 | 出发时间 -> 到达时间 | 历时
    G1(实际车次train_no: 240000G10336) 北京南(telecode: VNP) -> 上海虹桥(telecode: AOH) 06:00 -> 10:30 历时：04:30
    - 商务座: 剩余12张票 1748元
    - 一等座: 剩余89张票 933元
    - 二等座: 剩余250张票 553元

Errors mirror upstream — they're returned as plain text content (e.g.
`Error: Station not found.` or `没有查询到相关车次信息`), not raised.

State is a single JSON file at `$RAIL12306_MOCK_STATE_DIR/state.json`
(default `~/.openclaw/rail12306_mock`). Optional one-shot seed via
`$RAIL12306_MOCK_SEED_PATH`. The mock ships a built-in default
fixture with the BJ/Shanghai/Nanjing/Qufu corridor that
`train-ticket-plan` exercises, so the server is useful out of the box.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP


VERSION = "0.3.5-mock"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "RAIL12306_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/rail12306_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now_shanghai() -> datetime.datetime:
    # Asia/Shanghai = UTC+8 (no DST). Avoid zoneinfo dep.
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)


def _record(state: dict, op: str, **kwargs) -> None:
    state.setdefault("calls", []).append(
        {"op": op, "ts": _now_shanghai().isoformat(timespec="seconds"),
         **kwargs}
    )


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("RAIL12306_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                return json.load(f)
        return _default_state()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
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


# ---------------------------------------------------------------------------
# Default fixture — covers the train-ticket-plan corridor.
#
# Station codes follow 12306's actual 3-letter telecode convention
# (BJP=Beijing, VNP=Beijingnan, SHH=Shanghai, AOH=Shanghai Hongqiao,
# NJH=Nanjing, NKH=Nanjingnan, QFK=Qufu, QAK=Qufudong, QFN=Qufunan).
# ---------------------------------------------------------------------------

_DEFAULT_STATIONS = [
    # name, code, pinyin, short, city
    ("北京",     "BJP", "beijing",         "bj",   "北京"),
    ("北京南",   "VNP", "beijingnan",      "bjn",  "北京"),
    ("北京西",   "BXP", "beijingxi",       "bxp",  "北京"),
    ("上海",     "SHH", "shanghai",        "sh",   "上海"),
    ("上海虹桥", "AOH", "shanghaihongqiao","shhq", "上海"),
    ("南京",     "NJH", "nanjing",         "nj",   "南京"),
    ("南京南",   "NKH", "nanjingnan",      "njn",  "南京"),
    ("曲阜",     "QFK", "qufu",            "qf",   "曲阜"),
    ("曲阜东",   "QAK", "qufudong",        "qfd",  "曲阜"),
    ("曲阜南",   "QFN", "qufunan",         "qfn",  "曲阜"),
    ("济南",     "JNK", "jinan",           "jn",   "济南"),
    ("济南西",   "JGK", "jinanxi",         "jnx",  "济南"),
    ("天津",     "TJP", "tianjin",         "tj",   "天津"),
    ("天津南",   "TIP", "tianjinnan",      "tjn",  "天津"),
]

# Each train: code (G1), train_no (internal), seats (price+available),
# dw_flag list, and ordered stops with arrive/depart/day. Defining stops
# here lets get-tickets answer any segment query and get-train-route-stations
# return the full route.
_DEFAULT_TRAINS = [
    {
        "train_code": "G1",
        "train_no": "240000G10001",
        "dw_flag": ["复兴号"],
        "seats": [
            ("商务座", "swz", "9",  1748, 12),
            ("一等座", "zy",  "M",  933,  89),
            ("二等座", "ze",  "O",  553,  250),
        ],
        "stops": [
            ("北京南",   "--",    "06:00", 1),
            ("南京南",   "09:18", "09:20", 1),
            ("上海虹桥", "10:30", "--",    1),
        ],
    },
    {
        "train_code": "G3",
        "train_no": "240000G30003",
        "dw_flag": ["复兴号", "智能动车组"],
        "seats": [
            ("商务座", "swz", "9", 1748, 8),
            ("一等座", "zy",  "M", 933,  60),
            ("二等座", "ze",  "O", 553,  180),
        ],
        "stops": [
            ("北京南",   "--",    "09:00", 1),
            ("上海虹桥", "13:28", "--",    1),
        ],
    },
    {
        "train_code": "G103",
        "train_no": "240000G10303",
        "dw_flag": ["复兴号"],
        "seats": [
            ("商务座", "swz", "9", 1748, 5),
            ("一等座", "zy",  "M", 933,  40),
            ("二等座", "ze",  "O", 553,  120),
        ],
        "stops": [
            ("北京南",   "--",    "07:00", 1),
            ("济南西",   "08:34", "08:36", 1),
            ("曲阜东",   "09:08", "09:10", 1),
            ("南京南",   "10:55", "10:57", 1),
            ("上海虹桥", "12:28", "--",    1),
        ],
    },
    # 北京南 -> 曲阜东 evening trains used by the train-ticket-plan task
    {
        "train_code": "G155",
        "train_no": "240000G15505",
        "dw_flag": ["复兴号"],
        "seats": [
            ("商务座", "swz", "9", 685, 6),
            ("一等座", "zy",  "M", 365, 50),
            ("二等座", "ze",  "O", 228, 200),
        ],
        "stops": [
            ("北京南", "--",    "17:25", 1),
            ("曲阜东", "19:38", "--",    1),
        ],
    },
    {
        "train_code": "G163",
        "train_no": "240000G16307",
        "dw_flag": ["复兴号"],
        "seats": [
            ("商务座", "swz", "9", 685, 4),
            ("一等座", "zy",  "M", 365, 30),
            ("二等座", "ze",  "O", 228, 150),
        ],
        "stops": [
            ("北京南", "--",    "18:00", 1),
            ("济南西", "19:30", "19:32", 1),
            ("曲阜东", "20:14", "--",    1),
        ],
    },
    # 上海虹桥 -> 曲阜东 evening trains arriving close to BJ trains
    {
        "train_code": "G220",
        "train_no": "240000G22008",
        "dw_flag": ["复兴号"],
        "seats": [
            ("商务座", "swz", "9", 919, 6),
            ("一等座", "zy",  "M", 491, 50),
            ("二等座", "ze",  "O", 307, 200),
        ],
        "stops": [
            ("上海虹桥", "--",    "17:30", 1),
            ("南京南",   "18:32", "18:34", 1),
            ("曲阜东",   "20:00", "--",    1),
        ],
    },
    {
        "train_code": "G224",
        "train_no": "240000G22409",
        "dw_flag": ["复兴号", "智能动车组"],
        "seats": [
            ("商务座", "swz", "9", 919, 4),
            ("一等座", "zy",  "M", 491, 40),
            ("二等座", "ze",  "O", 307, 160),
        ],
        "stops": [
            ("上海虹桥", "--",    "18:15", 1),
            ("曲阜东",   "20:25", "--",    1),
        ],
    },
    # Sunday return: 曲阜东 -> 北京南
    {
        "train_code": "G164",
        "train_no": "240000G16408",
        "dw_flag": ["复兴号"],
        "seats": [
            ("商务座", "swz", "9", 685, 4),
            ("一等座", "zy",  "M", 365, 35),
            ("二等座", "ze",  "O", 228, 140),
        ],
        "stops": [
            ("曲阜东", "--",    "15:00", 1),
            ("北京南", "17:14", "--",    1),
        ],
    },
    # Sunday return: 曲阜东 -> 上海虹桥
    {
        "train_code": "G221",
        "train_no": "240000G22109",
        "dw_flag": ["复兴号"],
        "seats": [
            ("商务座", "swz", "9", 919, 4),
            ("一等座", "zy",  "M", 491, 38),
            ("二等座", "ze",  "O", 307, 145),
        ],
        "stops": [
            ("曲阜东",   "--",    "15:12", 1),
            ("南京南",   "16:36", "16:38", 1),
            ("上海虹桥", "17:42", "--",    1),
        ],
    },
]


def _default_state() -> dict:
    stations = []
    for i, (name, code, pinyin, short, city) in enumerate(_DEFAULT_STATIONS, 1):
        stations.append({
            "station_id": f"@{short}",
            "station_name": name,
            "station_code": code,
            "station_pinyin": pinyin,
            "station_short": short,
            "station_index": str(i),
            "code": str(1000 + i),
            "city": city,
            "r1": "",
            "r2": "",
        })
    trains = {}
    for t in _DEFAULT_TRAINS:
        trains[t["train_no"]] = {
            "train_code": t["train_code"],
            "train_no": t["train_no"],
            "dw_flag": list(t["dw_flag"]),
            "seats": [
                {"seat_name": s[0], "short": s[1], "seat_type_code": s[2],
                 "price": s[3], "num": s[4]}
                for s in t["seats"]
            ],
            "stops": [
                {"station_name": st[0], "arrive_time": st[1],
                 "depart_time": st[2], "day": st[3]}
                for st in t["stops"]
            ],
        }
    return {
        "version": VERSION,
        "stations": stations,
        "trains": trains,
        # schedules keyed by "<train_no>:<date>" for per-date overrides;
        # missing key => use base train seats. Verifier doesn't depend
        # on per-date variance.
        "schedules": {},
        "calls": [],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _by_code(state: dict) -> dict:
    return {s["station_code"]: s for s in state["stations"]}


def _by_name(state: dict) -> dict:
    return {s["station_name"]: s for s in state["stations"]}


def _by_city(state: dict) -> dict:
    out: dict[str, list] = {}
    for s in state["stations"]:
        out.setdefault(s["city"], []).append(s)
    return out


def _check_date(date: str) -> bool:
    """True if `date` (yyyy-MM-dd) is today or later in Asia/Shanghai."""
    try:
        target = datetime.date.fromisoformat(date)
    except ValueError:
        return False
    today = _now_shanghai().date()
    return target >= today


def _train_filter_pass(train_code: str, dw_flag: list, flags: str) -> bool:
    if not flags:
        return True
    for f in flags:
        if f == "G" and (train_code.startswith("G") or train_code.startswith("C")):
            return True
        if f == "D" and train_code.startswith("D"):
            return True
        if f == "Z" and train_code.startswith("Z"):
            return True
        if f == "T" and train_code.startswith("T"):
            return True
        if f == "K" and train_code.startswith("K"):
            return True
        if f == "O" and not any(train_code.startswith(p)
                                for p in ("G", "C", "D", "Z", "T", "K")):
            return True
        if f == "F" and "复兴号" in dw_flag:
            return True
        if f == "S" and "智能动车组" in dw_flag:
            return True
    return False


def _segment_times(train: dict, from_name: str, to_name: str):
    """Return (depart_time, arrive_time, lishi) for a segment within
    `train`. None if either station is not on the route or order is
    wrong. `lishi` is "hh:mm" duration accounting for day rollover."""
    idx_from = idx_to = -1
    for i, st in enumerate(train["stops"]):
        if st["station_name"] == from_name and idx_from == -1:
            idx_from = i
        if st["station_name"] == to_name and idx_from != -1 and idx_to == -1:
            idx_to = i
    if idx_from == -1 or idx_to == -1 or idx_to <= idx_from:
        return None
    dep = train["stops"][idx_from]["depart_time"]
    arr = train["stops"][idx_to]["arrive_time"]
    if dep == "--" or arr == "--":
        return None
    dep_day = train["stops"][idx_from]["day"]
    arr_day = train["stops"][idx_to]["day"]
    dh, dm = (int(x) for x in dep.split(":"))
    ah, am = (int(x) for x in arr.split(":"))
    minutes = (arr_day - dep_day) * 24 * 60 + (ah * 60 + am) - (dh * 60 + dm)
    if minutes < 0:
        return None
    return dep, arr, f"{minutes // 60:02d}:{minutes % 60:02d}"


def _format_ticket_status(num: int) -> str:
    if num == 0:
        return "无票"
    return f"剩余{num}张票"


def _format_tickets(tickets: list[dict]) -> str:
    if not tickets:
        return "没有查询到相关车次信息"
    out = "车次 | 出发站 -> 到达站 | 出发时间 -> 到达时间 | 历时\n"
    for t in tickets:
        line = (f"{t['train_code']}(实际车次train_no: {t['train_no']}) "
                f"{t['from_station']}(telecode: {t['from_telecode']}) -> "
                f"{t['to_station']}(telecode: {t['to_telecode']}) "
                f"{t['start_time']} -> {t['arrive_time']} "
                f"历时：{t['lishi']}")
        for p in t["prices"]:
            line += (f"\n- {p['seat_name']}: "
                     f"{_format_ticket_status(p['num'])} {p['price']}元")
        out += line + "\n"
    return out


def _format_tickets_csv(tickets: list[dict]) -> str:
    if not tickets:
        return "没有查询到相关车次信息"
    out = ("车次,实际车次train_no,出发站,到达站,出发时间,到达时间,"
           "历时,票价,特色标签\n")
    for t in tickets:
        prices = ""
        for p in t["prices"]:
            prices += (f"{p['seat_name']}: "
                       f"{_format_ticket_status(p['num'])}{p['price']}元,")
        flags = "/" if not t["dw_flag"] else "&".join(t["dw_flag"])
        out += (f"{t['train_code']},{t['train_no']},"
                f"{t['from_station']}(telecode:{t['from_telecode']}),"
                f"{t['to_station']}(telecode: {t['to_telecode']}),"
                f"{t['start_time']},{t['arrive_time']},{t['lishi']},"
                f"[{prices}],{flags}\n")
    return out


def _build_ticket(state: dict, train: dict, from_code: str, to_code: str,
                  date: str) -> dict | None:
    by_code = _by_code(state)
    if from_code not in by_code or to_code not in by_code:
        return None
    from_name = by_code[from_code]["station_name"]
    to_name = by_code[to_code]["station_name"]
    seg = _segment_times(train, from_name, to_name)
    if seg is None:
        return None
    dep, arr, lishi = seg
    sched = state.get("schedules", {}).get(f"{train['train_no']}:{date}")
    seats = train["seats"]
    if sched and "seats" in sched:
        seats = sched["seats"]
    prices = [{"seat_name": s["seat_name"], "short": s.get("short", ""),
               "seat_type_code": s.get("seat_type_code", ""),
               "num": s["num"], "price": s["price"]}
              for s in seats]
    return {
        "train_code": train["train_code"],
        "train_no": train["train_no"],
        "from_station": from_name, "from_telecode": from_code,
        "to_station": to_name, "to_telecode": to_code,
        "start_time": dep, "arrive_time": arr, "lishi": lishi,
        "dw_flag": list(train.get("dw_flag", [])),
        "prices": prices,
    }


def _sort_tickets(tickets: list[dict], sort_flag: str,
                  reverse: bool) -> list[dict]:
    def hm(s: str) -> int:
        h, m = s.split(":")
        return int(h) * 60 + int(m)

    if sort_flag == "startTime":
        tickets.sort(key=lambda t: hm(t["start_time"]))
    elif sort_flag == "arriveTime":
        tickets.sort(key=lambda t: hm(t["arrive_time"]))
    elif sort_flag == "duration":
        tickets.sort(key=lambda t: hm(t["lishi"]))
    if reverse and sort_flag:
        tickets.reverse()
    return tickets


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

mcp = FastMCP("rail-12306-mock")

# Fixture tools are for the harness, not the agent. `mock_debug_seed_*` writes
# rows "bypassing the allowlist" — including straight into a table a grader
# reads — and `mock_debug_state` dumps state the agent is supposed to discover
# through the API. Registered only when MOCK_DEBUG_TOOLS is set, so by default
# they are neither listed nor callable over MCP.
_DEBUG_TOOLS = os.environ.get("MOCK_DEBUG_TOOLS", "").lower() not in ("", "0", "false", "no")


def _debug_tool(*a, **kw):
    return mcp.tool(*a, **kw) if _DEBUG_TOOLS else (lambda fn: fn)



def _text(s: str) -> str:
    """Tool results are returned as JSON-encodable strings; FastMCP
    wraps them as text content automatically. The upstream wraps
    everything in `{content:[{type:'text', text}]}` — FastMCP does
    the same when a tool returns a str."""
    return s


@mcp.tool(name="get-current-date")
def get_current_date() -> str:
    """Get the current date (Asia/Shanghai, UTC+8) as yyyy-MM-dd.
    Used by the agent to resolve relative dates like 明天."""
    with _lock():
        s = _load_state()
        _record(s, "get_current_date")
        _save_state(s)
    return _text(_now_shanghai().strftime("%Y-%m-%d"))


@mcp.tool(name="get-stations-code-in-city")
def get_stations_code_in_city(city: str) -> str:
    """All stations in a Chinese city. Returns JSON list of
    {station_code, station_name}."""
    with _lock():
        s = _load_state()
        cities = _by_city(s)
        _record(s, "get_stations_code_in_city", city=city,
                hit=city in cities)
        _save_state(s)
    if city not in cities:
        return _text("Error: City not found. ")
    return _text(json.dumps(
        [{"station_code": st["station_code"],
          "station_name": st["station_name"]}
         for st in cities[city]],
        ensure_ascii=False))


@mcp.tool(name="get-station-code-of-citys")
def get_station_code_of_citys(citys: str) -> str:
    """City-name -> representative station_code (pipe-separated)."""
    with _lock():
        s = _load_state()
        cities = _by_city(s)
        _record(s, "get_station_code_of_citys", citys=citys)
        _save_state(s)
    out: dict[str, dict] = {}
    for city in citys.split("|"):
        if city not in cities:
            out[city] = {"error": "未检索到城市。"}
            continue
        # Pick the station whose name equals the city name; else the
        # first listed station for that city.
        hit = next((st for st in cities[city]
                    if st["station_name"] == city), cities[city][0])
        out[city] = {"station_code": hit["station_code"],
                     "station_name": hit["station_name"]}
    return _text(json.dumps(out, ensure_ascii=False))


@mcp.tool(name="get-station-code-by-names")
def get_station_code_by_names(stationNames: str) -> str:
    """Specific station-name -> station_code (pipe-separated). The
    upstream strips a trailing `站` if present."""
    with _lock():
        s = _load_state()
        names = _by_name(s)
        _record(s, "get_station_code_by_names",
                stationNames=stationNames)
        _save_state(s)
    out: dict[str, dict] = {}
    for raw in stationNames.split("|"):
        name = raw[:-1] if raw.endswith("站") else raw
        if name not in names:
            out[name] = {"error": "未检索到城市。"}
        else:
            out[name] = {"station_code": names[name]["station_code"],
                         "station_name": names[name]["station_name"]}
    return _text(json.dumps(out, ensure_ascii=False))


@mcp.tool(name="get-station-by-telecode")
def get_station_by_telecode(stationTelecode: str) -> str:
    """Full station record by 3-letter telecode."""
    with _lock():
        s = _load_state()
        by_code = _by_code(s)
        _record(s, "get_station_by_telecode",
                stationTelecode=stationTelecode,
                hit=stationTelecode in by_code)
        _save_state(s)
    if stationTelecode not in by_code:
        return _text("Error: Station not found. ")
    return _text(json.dumps(by_code[stationTelecode], ensure_ascii=False))


@mcp.tool(name="get-tickets")
def get_tickets(date: str,
                fromStation: str,
                toStation: str,
                trainFilterFlags: str = "",
                earliestStartTime: int = 0,
                latestStartTime: int = 24,
                sortFlag: str = "",
                sortReverse: bool = False,
                limitedNum: int = 0,
                csvFormat: bool = False) -> str:
    """Search remaining tickets. Returns the upstream's exact text
    format that the Toolathlon evaluator parses with regex.

    Parameters match the upstream zod schema verbatim:
      - `date` "yyyy-MM-dd"; must be today or later (Shanghai TZ)
      - `fromStation`/`toStation` are station telecodes
      - `trainFilterFlags` any of GDZTKOFS (multi-select)
      - `earliestStartTime`/`latestStartTime` 0..24 (hour-of-day)
      - `sortFlag` startTime|arriveTime|duration
      - `sortReverse` invert sort
      - `limitedNum` 0 = unlimited
      - `csvFormat` switch to CSV layout
    """
    with _lock():
        s = _load_state()
        if not _check_date(date):
            _record(s, "get_tickets", date=date, fromStation=fromStation,
                    toStation=toStation, result="date_in_past")
            _save_state(s)
            return _text("Error: The date cannot be earlier than today.")
        by_code = _by_code(s)
        if fromStation not in by_code or toStation not in by_code:
            _record(s, "get_tickets", date=date, fromStation=fromStation,
                    toStation=toStation, result="station_not_found")
            _save_state(s)
            return _text("Error: Station not found. ")
        tickets = []
        for train in s["trains"].values():
            if not _train_filter_pass(train["train_code"],
                                      train.get("dw_flag", []),
                                      trainFilterFlags):
                continue
            t = _build_ticket(s, train, fromStation, toStation, date)
            if t is None:
                continue
            hour = int(t["start_time"].split(":")[0])
            if not (earliestStartTime <= hour < latestStartTime):
                continue
            tickets.append(t)
        tickets = _sort_tickets(tickets, sortFlag, sortReverse)
        if limitedNum > 0:
            tickets = tickets[:limitedNum]
        _record(s, "get_tickets", date=date, fromStation=fromStation,
                toStation=toStation, count=len(tickets),
                trainFilterFlags=trainFilterFlags)
        _save_state(s)
    if csvFormat:
        return _text(_format_tickets_csv(tickets))
    return _text(_format_tickets(tickets))


@mcp.tool(name="get-interline-tickets")
def get_interline_tickets(date: str,
                          fromStation: str,
                          toStation: str,
                          middleStation: str = "",
                          showWZ: bool = False,
                          trainFilterFlags: str = "",
                          earliestStartTime: int = 0,
                          latestStartTime: int = 24,
                          sortFlag: str = "",
                          sortReverse: bool = False,
                          limitedNum: int = 10) -> str:
    """Interline (one-transfer) ticket search. Considers every pair of
    trains that share a transfer station (or the requested
    `middleStation`) with a 20..240 min wait. Returns the upstream's
    multi-line text format."""
    with _lock():
        s = _load_state()
        if not _check_date(date):
            _record(s, "get_interline_tickets", date=date,
                    result="date_in_past")
            _save_state(s)
            return _text("Error: The date cannot be earlier than today.")
        by_code = _by_code(s)
        if fromStation not in by_code or toStation not in by_code:
            _record(s, "get_interline_tickets", date=date,
                    result="station_not_found")
            _save_state(s)
            return _text("Error: Station not found. ")
        from_name = by_code[fromStation]["station_name"]
        to_name = by_code[toStation]["station_name"]
        middle_filter = (by_code[middleStation]["station_name"]
                         if middleStation and middleStation in by_code
                         else None)
        # legs from `from` (any train passing through it that has a
        # later stop) + legs from `to` (any train ending at it)
        first_legs = []  # list of (train, mid_name, dep, arr, lishi)
        for train in s["trains"].values():
            names = [st["station_name"] for st in train["stops"]]
            if from_name not in names:
                continue
            for mid in names:
                if mid == from_name or mid == to_name:
                    continue
                if middle_filter and mid != middle_filter:
                    continue
                seg = _segment_times(train, from_name, mid)
                if seg is None:
                    continue
                first_legs.append((train, mid, *seg))
        results = []
        for train1, mid, dep1, arr1, lishi1 in first_legs:
            mid_code = _by_name(s)[mid]["station_code"]
            for train2 in s["trains"].values():
                if train2["train_no"] == train1["train_no"]:
                    continue
                names2 = [st["station_name"] for st in train2["stops"]]
                if mid not in names2 or to_name not in names2:
                    continue
                seg2 = _segment_times(train2, mid, to_name)
                if seg2 is None:
                    continue
                dep2, arr2, lishi2 = seg2
                # wait = dep2 - arr1 (same day; mock keeps day=1)
                ah, am = (int(x) for x in arr1.split(":"))
                dh, dm = (int(x) for x in dep2.split(":"))
                wait = (dh * 60 + dm) - (ah * 60 + am)
                if not (20 <= wait <= 240):
                    continue
                # Build the two sub-tickets and the wrapper.
                t1 = _build_ticket(s, train1, fromStation, mid_code, date)
                t2 = _build_ticket(s, train2, mid_code, toStation, date)
                if t1 is None or t2 is None:
                    continue
                if not (_train_filter_pass(train1["train_code"],
                                           train1.get("dw_flag", []),
                                           trainFilterFlags)
                        and _train_filter_pass(train2["train_code"],
                                               train2.get("dw_flag", []),
                                               trainFilterFlags)):
                    continue
                start_hour = int(t1["start_time"].split(":")[0])
                if not (earliestStartTime <= start_hour < latestStartTime):
                    continue
                # total lishi from departure of leg1 to arrival of leg2
                sh, sm = (int(x) for x in t1["start_time"].split(":"))
                eh, em = (int(x) for x in t2["arrive_time"].split(":"))
                total = (eh * 60 + em) - (sh * 60 + sm)
                results.append({
                    "start_date": date,
                    "start_time": t1["start_time"],
                    "arrive_date": date,
                    "arrive_time": t2["arrive_time"],
                    "from_station_name": from_name,
                    "middle_station_name": mid,
                    "end_station_name": to_name,
                    "same_station": True,
                    "same_train": False,
                    "wait_time": f"{wait // 60:02d}:{wait % 60:02d}",
                    "lishi": f"{total // 60:02d}:{total % 60:02d}",
                    "train_code": t1["train_code"],
                    "tickets": [t1, t2],
                })
        # apply sort + limit (sort by start_time by default to be deterministic)
        def hm(s: str) -> int:
            h, m = s.split(":"); return int(h) * 60 + int(m)
        if sortFlag == "startTime":
            results.sort(key=lambda r: hm(r["start_time"]))
        elif sortFlag == "arriveTime":
            results.sort(key=lambda r: hm(r["arrive_time"]))
        elif sortFlag == "duration":
            results.sort(key=lambda r: hm(r["lishi"]))
        else:
            results.sort(key=lambda r: hm(r["start_time"]))
        if sortReverse and sortFlag:
            results.reverse()
        results = results[:max(1, limitedNum)]
        _record(s, "get_interline_tickets", date=date,
                fromStation=fromStation, toStation=toStation,
                middleStation=middleStation, count=len(results))
        _save_state(s)
    if not results:
        return _text("很抱歉，未查到相关的列车余票。")
    out = ("出发时间 -> 到达时间 | 出发车站 -> 中转车站 -> 到达车站 | "
           "换乘标志 |换乘等待时间| 总历时\n\n")
    for r in results:
        out += (f"{r['start_date']} {r['start_time']} -> "
                f"{r['arrive_date']} {r['arrive_time']} | "
                f"{r['from_station_name']} -> {r['middle_station_name']} -> "
                f"{r['end_station_name']} | "
                f"{'同站换乘' if r['same_station'] else '换站换乘'} | "
                f"{r['wait_time']} | {r['lishi']}\n\n")
        out += "\t" + _format_tickets(r["tickets"]).replace("\n", "\n\t") + "\n"
    return _text(out)


@mcp.tool(name="get-train-route-stations")
def get_train_route_stations(trainNo: str,
                             fromStationTelecode: str,
                             toStationTelecode: str,
                             departDate: str) -> str:
    """Full route (with stopover times) for a specific train_no over
    the requested segment. JSON list of {arrive_time, station_name,
    stopover_time, station_no}."""
    with _lock():
        s = _load_state()
        train = s["trains"].get(trainNo)
        by_code = _by_code(s)
        _record(s, "get_train_route_stations", trainNo=trainNo,
                fromStationTelecode=fromStationTelecode,
                toStationTelecode=toStationTelecode,
                departDate=departDate,
                hit=train is not None)
        _save_state(s)
    if not train:
        return _text("未查询到相关车次信息。")
    from_name = by_code.get(fromStationTelecode, {}).get("station_name")
    to_name = by_code.get(toStationTelecode, {}).get("station_name")
    names = [st["station_name"] for st in train["stops"]]
    if from_name not in names or to_name not in names:
        return _text("未查询到相关车次信息。")
    i = names.index(from_name)
    j = names.index(to_name)
    if j < i:
        return _text("未查询到相关车次信息。")
    out = []
    for idx, st in enumerate(train["stops"][i:j + 1], start=i + 1):
        if idx == 1 or st == train["stops"][i]:
            arrive = st["depart_time"] if st["arrive_time"] == "--" \
                else st["arrive_time"]
        else:
            arrive = st["arrive_time"]
        # stopover time = depart - arrive in minutes, formatted "M分钟"
        if st["arrive_time"] != "--" and st["depart_time"] != "--":
            ah, am = (int(x) for x in st["arrive_time"].split(":"))
            dh, dm = (int(x) for x in st["depart_time"].split(":"))
            mins = (dh * 60 + dm) - (ah * 60 + am)
            stopover = f"{mins}分钟"
        else:
            stopover = "----"
        out.append({"arrive_time": arrive,
                    "station_name": st["station_name"],
                    "stopover_time": stopover,
                    "station_no": idx})
    return _text(json.dumps(out, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Mock-only debug helpers
# ---------------------------------------------------------------------------

@_debug_tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Return the full persisted state (stations, trains, schedules,
    calls). Mock-only — not part of the upstream surface."""
    with _lock():
        return _load_state()


@_debug_tool(name="mock_debug_seed")
def mock_debug_seed(stations: list | None = None,
                    trains: dict | None = None,
                    schedules: dict | None = None,
                    reset: bool = False) -> dict:
    """Bulk-merge fixtures into state. Used by per-task setup to
    customise the train/station catalogue. If `reset=True`, drops
    everything (including calls) first."""
    with _lock():
        s = _default_state() if reset else _load_state()
        if stations:
            existing = {st["station_code"] for st in s["stations"]}
            for st in stations:
                if st.get("station_code") in existing:
                    s["stations"] = [x for x in s["stations"]
                                     if x["station_code"] != st["station_code"]]
                s["stations"].append(st)
        if trains:
            s["trains"].update(trains)
        if schedules:
            s["schedules"].update(schedules)
        _record(s, "mock_debug_seed",
                stations=len(stations or []),
                trains=len(trains or {}),
                schedules=len(schedules or {}),
                reset=reset)
        _save_state(s)
        return {"ok": True, "stations": len(s["stations"]),
                "trains": len(s["trains"])}


if __name__ == "__main__":
    mcp.run()
