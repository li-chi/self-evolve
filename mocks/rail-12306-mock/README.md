# rail-12306-mock

Mock MCP server that mirrors the npm package
[`12306-mcp`](https://github.com/lockon-n/12306-mcp) (Toolathlon's
fork of [Joooook/12306-mcp](https://github.com/Joooook/12306-mcp)).
The real server hits `kyfw.12306.cn` and can place actual China-Rail
bookings — **RL training MUST use this mock**.

## Tool surface

Names, parameters, and return shape come straight from upstream
`src/index.ts` (the `server.tool(...)` zod schemas):

| tool                          | parameters                                                                                                                                              |
|-------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------|
| `get-current-date`            | —                                                                                                                                                       |
| `get-stations-code-in-city`   | `city`                                                                                                                                                  |
| `get-station-code-of-citys`   | `citys` (pipe-separated)                                                                                                                                |
| `get-station-code-by-names`   | `stationNames` (pipe-separated; trailing 站 stripped)                                                                                                   |
| `get-station-by-telecode`     | `stationTelecode`                                                                                                                                       |
| `get-tickets`                 | `date`, `fromStation`, `toStation`, `trainFilterFlags=''`, `earliestStartTime=0`, `latestStartTime=24`, `sortFlag=''`, `sortReverse=False`, `limitedNum=0`, `csvFormat=False` |
| `get-interline-tickets`       | `date`, `fromStation`, `toStation`, `middleStation=''`, `showWZ=False`, `trainFilterFlags=''`, `earliestStartTime=0`, `latestStartTime=24`, `sortFlag=''`, `sortReverse=False`, `limitedNum=10` |
| `get-train-route-stations`    | `trainNo`, `fromStationTelecode`, `toStationTelecode`, `departDate`                                                                                     |

Plus two mock-only debug tools for per-task setup / inspection:

- `mock_debug_state` — return the full persisted state dict.
- `mock_debug_seed(stations?, trains?, schedules?, reset=False)` —
  merge/overwrite stations + trains; `reset=True` drops everything
  back to the default fixture first.

### Output formats

`get-tickets` returns the upstream's **exact text format**, because
the Toolathlon `train-ticket-plan` evaluator parses it with regex.
Example:

```
车次 | 出发站 -> 到达站 | 出发时间 -> 到达时间 | 历时
G1(实际车次train_no: 240000G10001) 北京南(telecode: VNP) -> 上海虹桥(telecode: AOH) 06:00 -> 10:30 历时：04:30
- 商务座: 剩余12张票 1748元
- 一等座: 剩余89张票 933元
- 二等座: 剩余250张票 553元
```

Empty result → `没有查询到相关车次信息`. Station/date errors are
plain text matching upstream (`Error: Station not found.`,
`Error: The date cannot be earlier than today.`).

`get-train-route-stations` and the station-lookup tools return JSON.

## State

State lives in `$RAIL12306_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/rail12306/state.json` in-container,
`~/.openclaw/rail12306_mock/state.json` outside):

```jsonc
{
  "version": "0.3.5-mock",
  "stations": [
    {"station_name":"北京南","station_code":"VNP","station_pinyin":"beijingnan",
     "station_short":"bjn","city":"北京", ...}
  ],
  "trains": {
    "240000G10001": {
      "train_code":"G1","train_no":"240000G10001",
      "dw_flag":["复兴号"],
      "seats":[{"seat_name":"商务座","short":"swz","price":1748,"num":12}, ...],
      "stops":[{"station_name":"北京南","arrive_time":"--","depart_time":"06:00","day":1}, ...]
    }
  },
  "schedules": {
    "<train_no>:<yyyy-MM-dd>": {"seats":[...]}    // optional per-date override
  },
  "calls": [{"op":"get_tickets","ts":"...","date":"...","count":3}, ...]
}
```

`calls` is what the verifier consumes — every tool call appends one
entry. File-locking via `fcntl.flock` makes concurrent invocations
safe; per-rollout isolation should clear the state dir between
rollouts.

### Default fixture

Ships with a built-in fixture covering the **Beijing / Shanghai /
Nanjing / Qufu** corridor that `train-ticket-plan` exercises:

- Stations: 北京 (BJP), 北京南 (VNP), 北京西 (BXP), 上海 (SHH),
  上海虹桥 (AOH), 南京 (NJH), 南京南 (NKH), 曲阜 (QFK),
  曲阜东 (QAK), 曲阜南 (QFN), 济南 (JNK), 济南西 (JGK),
  天津 (TJP), 天津南 (TIP).
- 9 G-trains: BJP/VNP ↔ AOH/SHH direct (G1, G3, G103), VNP ↔ QAK
  evening (G155, G163), AOH ↔ QAK evening (G220, G224), QAK ↔ VNP
  Sunday afternoon (G164), QAK ↔ AOH Sunday afternoon (G221).

Override by `mock_debug_seed(...)` or by pre-writing `state.json`.
Set `$RAIL12306_MOCK_SEED_PATH` to a JSON file in the same shape to
load custom fixtures on first start.

## Run

```bash
# local
RAIL12306_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  rail-12306-mock:
    build:
      context: ../../mcp_servers/rail-12306-mock
      dockerfile: Dockerfile
    image: mcp-env/rail-12306-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      RAIL12306_MOCK_STATE_DIR: /workspace/output/end_state/rail12306
      RAIL12306_MOCK_SEED_PATH: /workspace/input/rail12306_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```
