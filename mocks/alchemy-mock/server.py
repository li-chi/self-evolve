"""Alchemy mock MCP server.

Mirrors the tool surface of `@alchemy/mcp-server`
(github.com/alchemyplatform/alchemy-mcp), the official Alchemy MCP
server registered by `mcp-atlas` (template entry `alchemy`,
`npx @alchemy/mcp-server`). That server proxies Alchemy's hosted
blockchain APIs (Prices, Multichain Token, Multichain Transaction
History, NFT V3, Transfers, JSON-RPC Token + Block + Tx Receipts,
Solana DAS, Beacon, Debug/Trace, Simulation) — so each tool here
returns the same JSON shape as the underlying Alchemy endpoint.

Response shapes:
  * JSON-RPC tools (`alchemy_getTokenBalances`, `alchemy_getTokenMetadata`,
    `alchemy_getAssetTransfers`, `eth_getBlockByNumber`, `eth_gasPrice`,
    `eth_blockNumber`, `alchemy_getTransactionReceipts`, ...) return the
    full JSON-RPC envelope `{"jsonrpc":"2.0","id":<int>,"result":<obj>}`
    or `{..., "error":{"code":<int>,"message":<str>}}` — matching what
    the upstream client surfaces to the agent.
  * NFT V3 + Multichain REST tools return the response body directly
    (no envelope), matching Alchemy's HTTPS REST APIs.

Implemented subset (~40 tools, covers the Tier A `alchemy` rollouts):

  Discovery        listSupportedNetworks
  Prices           fetchTokenPriceBySymbol, fetchTokenPriceByAddress,
                   fetchTokenPriceHistoryBySymbol,
                   fetchTokenPriceHistoryByTimeFrame
  Token            getTokenBalances, getTokenMetadata, getTokenAllowance
  Multichain       fetchTokensOwnedByMultichainAddresses,
                   fetchAddressTransactionHistory,
                   fetchNftsOwnedByMultichainAddresses,
                   fetchNftContractDataByMultichainAddress
  Transfers        fetchTransfers
  NFT V3           getNFTsForOwner, getNFTsForContract, getNFTMetadata,
                   getContractMetadata, getOwnersForNFT,
                   getOwnersForContract, getFloorPrice,
                   getContractsForOwner, getCollectionsForOwner,
                   isSpamContract, isAirdropNFT, isHolderOfContract,
                   getSpamContracts, summarizeNFTAttributes,
                   computeRarity, getNFTSales, searchContractMetadata,
                   getCollectionMetadata, invalidateNFTContractCache,
                   reportSpam
  Block / Tx       getTransactionReceipts, eth_blockNumber, eth_gasPrice,
                   eth_getBlockByNumber, eth_getTransactionByHash,
                   eth_getTransactionReceipt
  Solana DAS       solanaGetAsset, solanaGetAssetsByOwner,
                   solanaGetTokenAccounts

Plus mock-only helpers: `mock_debug_state`, `mock_debug_seed_state`,
`mock_debug_seed_address` (used by per-task setup/verification).

State (`$ALCHEMY_MOCK_STATE_DIR/state.json`, default
`~/.openclaw/alchemy_mock/state.json`):

  {
    "chains": ["eth-mainnet", "polygon-mainnet", ...],
    "blocks": {
      "<chain>": {
        "latest": <int>,
        "<num>": {"hash":"0x...","timestamp":"0x...","transactions":[...]}
      }
    },
    "gas":   {"<chain>": "<wei>"},
    "addresses": {
      "<chain>:<lower-0xaddr>": {
        "native_balance": "<wei str>",
        "token_balances": [{"contractAddress":"0x...","tokenBalance":"<hex>"}],
        "nfts":           [{...alchemy NFT V3 shape...}],
        "transactions":   [{...alchemy_getAssetTransfers shape...}]
      }
    },
    "tokens":   {"<chain>:<0xcontract>": {"name","symbol","decimals","logo"}},
    "prices":   {"<SYMBOL>": "<usd>", "<chain>:<0xaddr>": "<usd>"},
    "solana":   {"assets":{"<id>":{...}}, "tokens":{"<owner>":[...]}},
    "calls":    [{"op","ts",...}, ...]
  }

Errors mirror Alchemy + JSON-RPC shapes:
  * JSON-RPC tools: `{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"..."}}`
  * REST tools:     `{"error":{"code":<int>,"message":"..."}}`
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
from typing import Any

from mcp.server.fastmcp import FastMCP


# ----------------------------------------------------------------------
# Supported networks (verbatim from alchemy-mcp api/networks.ts)
# ----------------------------------------------------------------------

SUPPORTED_NETWORKS: dict[str, list[dict[str, Any]]] = {
    "evm": [
        {"chain": "Ethereum", "mainnet": "eth-mainnet",
         "testnets": ["eth-sepolia", "eth-holesky", "eth-hoodi"]},
        {"chain": "Arbitrum", "mainnet": "arb-mainnet",
         "testnets": ["arb-sepolia"]},
        {"chain": "Arbitrum Nova", "mainnet": "arbnova-mainnet",
         "testnets": []},
        {"chain": "Base", "mainnet": "base-mainnet",
         "testnets": ["base-sepolia"]},
        {"chain": "BNB Chain", "mainnet": "bnb-mainnet",
         "testnets": ["bnb-testnet"]},
        {"chain": "Avalanche", "mainnet": "avax-mainnet", "testnets": []},
        {"chain": "Polygon", "mainnet": "polygon-mainnet", "testnets": []},
        {"chain": "Polygon zkEVM", "mainnet": "polygonzkevm-mainnet",
         "testnets": []},
        {"chain": "Optimism", "mainnet": "opt-mainnet",
         "testnets": ["opt-sepolia"]},
        {"chain": "zkSync", "mainnet": "zksync-mainnet",
         "testnets": ["zksync-sepolia"]},
        {"chain": "Linea", "mainnet": "linea-mainnet",
         "testnets": ["linea-sepolia"]},
        {"chain": "Scroll", "mainnet": "scroll-mainnet",
         "testnets": ["scroll-sepolia"]},
        {"chain": "Blast", "mainnet": "blast-mainnet",
         "testnets": ["blast-sepolia"]},
        {"chain": "Gnosis", "mainnet": "gnosis-mainnet", "testnets": []},
        {"chain": "Celo", "mainnet": "celo-mainnet",
         "testnets": ["celo-sepolia"]},
        {"chain": "Mantle", "mainnet": "mantle-mainnet",
         "testnets": ["mantle-sepolia"]},
        {"chain": "Mode", "mainnet": "mode-mainnet",
         "testnets": ["mode-sepolia"]},
        {"chain": "Zora", "mainnet": "zora-mainnet",
         "testnets": ["zora-sepolia"]},
        {"chain": "Shape", "mainnet": "shape-mainnet",
         "testnets": ["shape-sepolia"]},
        {"chain": "Unichain", "mainnet": "unichain-mainnet",
         "testnets": ["unichain-sepolia"]},
        {"chain": "WorldChain", "mainnet": "worldchain-mainnet",
         "testnets": ["worldchain-sepolia"]},
    ],
    "solana": [
        {"chain": "Solana", "mainnet": "solana-mainnet",
         "testnets": ["solana-devnet", "solana-testnet"]},
    ],
}

# Flat set of all valid network ids, for chain validation.
_ALL_NETWORK_IDS: set[str] = set()
for _grp in SUPPORTED_NETWORKS.values():
    for _net in _grp:
        _ALL_NETWORK_IDS.add(_net["mainnet"])
        for _t in _net["testnets"]:
            _ALL_NETWORK_IDS.add(_t)

DEFAULT_CHAINS = [
    "eth-mainnet", "polygon-mainnet", "arb-mainnet",
    "base-mainnet", "opt-mainnet", "solana-mainnet",
]


# ----------------------------------------------------------------------
# State / locking
# ----------------------------------------------------------------------

def _state_path() -> str:
    state_dir = os.environ.get(
        "ALCHEMY_MOCK_STATE_DIR",
        os.path.expanduser("~/.openclaw/alchemy_mock"),
    )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, "state.json")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _empty_state() -> dict:
    return {
        "chains": list(DEFAULT_CHAINS),
        "blocks": {
            c: {"latest": 19_000_000 + i,
                str(19_000_000 + i): _default_block(19_000_000 + i)}
            for i, c in enumerate(DEFAULT_CHAINS) if not c.startswith("solana-")
        },
        "gas": {c: "20000000000" for c in DEFAULT_CHAINS
                if not c.startswith("solana-")},  # 20 gwei default
        "addresses": {},
        "tokens": {},
        "prices": {},
        "solana": {"assets": {}, "tokens": {}},
        "calls": [],
    }


def _default_block(num: int) -> dict:
    return {
        "hash": "0x" + f"{num:064x}",
        "parentHash": "0x" + f"{(num - 1):064x}",
        "timestamp": hex(1_700_000_000 + num),
        "number": hex(num),
        "transactions": [],
        "gasUsed": "0x0",
        "gasLimit": "0x1c9c380",
        "baseFeePerGas": "0x4a817c800",
        "miner": "0x0000000000000000000000000000000000000000",
    }


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        seed = os.environ.get("ALCHEMY_MOCK_SEED_PATH")
        if seed and os.path.exists(seed):
            with open(seed, "r", encoding="utf-8") as f:
                return json.load(f)
        return _empty_state()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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
    entry = {"op": op, "ts": _now()}
    entry.update(kwargs)
    state["calls"].append(entry)


# ----------------------------------------------------------------------
# Validation helpers
# ----------------------------------------------------------------------

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]+$")
_RPC_ID = [0]


def _next_rpc_id() -> int:
    _RPC_ID[0] += 1
    return _RPC_ID[0]


def _rpc_ok(result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": _next_rpc_id(), "result": result}


def _rpc_err(code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": _next_rpc_id(),
            "error": {"code": code, "message": message}}


def _rest_err(code: int, message: str) -> dict:
    """Shape used by Alchemy REST (Prices / Multichain / NFT V3) on 4xx."""
    return {"error": {"code": code, "message": message}}


def _is_valid_addr(addr: str | None) -> bool:
    return bool(addr and isinstance(addr, str) and _ADDR_RE.match(addr))


def _is_valid_chain(net: str | None) -> bool:
    return bool(net and net in _ALL_NETWORK_IDS)


def _norm_addr(addr: str) -> str:
    return addr.lower() if isinstance(addr, str) else addr


def _addr_key(chain: str, addr: str) -> str:
    return f"{chain}:{_norm_addr(addr)}"


def _addr_entry(state: dict, chain: str, addr: str) -> dict:
    """Get-or-default an address fixture. Read-only callers should
    NOT persist this — use `_save_state` only if mutating fields are
    written. We always return defaults so queries against
    unseeded-but-valid addresses produce realistic empty responses."""
    return state["addresses"].get(_addr_key(chain, addr), {
        "native_balance": "0",
        "token_balances": [],
        "nfts": [],
        "transactions": [],
    })


def _int_or_default(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _hex_to_int(h: str) -> int:
    if isinstance(h, int):
        return h
    s = str(h)
    if s.startswith("0x"):
        return int(s, 16)
    return int(s)


mcp = FastMCP("alchemy-mock")


# ======================================================================
# Discovery
# ======================================================================

@mcp.tool(name="listSupportedNetworks")
def list_supported_networks() -> dict:
    """List all blockchain networks supported by Alchemy, including
    EVM and Solana chains. Mirrors the upstream tool of the same name —
    returns the full `SUPPORTED_NETWORKS` map from `api/networks.ts`."""
    with _lock():
        s = _load_state()
        _record(s, "listSupportedNetworks")
        _save_state(s)
        return SUPPORTED_NETWORKS


# ======================================================================
# Prices API
# ======================================================================

@mcp.tool(name="fetchTokenPriceBySymbol")
def fetch_token_price_by_symbol(symbols: list[str]) -> dict:
    """Alchemy Prices API: GET /prices/v1/by-symbol — return current USD
    prices for a list of token tickers. Unknown symbols default to a
    deterministic stub price."""
    with _lock():
        s = _load_state()
        out = []
        for sym in symbols or []:
            key = sym.upper()
            price = s["prices"].get(key)
            if price is None:
                price = _stub_price_for_symbol(key)
            out.append({
                "symbol": key,
                "prices": [{
                    "currency": "USD",
                    "value": str(price),
                    "lastUpdatedAt": _now(),
                }],
            })
        _record(s, "fetchTokenPriceBySymbol", symbols=symbols)
        _save_state(s)
        return {"data": out}


def _stub_price_for_symbol(sym: str) -> str:
    defaults = {"BTC": "65000.00", "ETH": "3500.00", "SOL": "150.00",
                "MATIC": "0.75", "USDC": "1.00", "USDT": "1.00",
                "DAI": "1.00", "LINK": "15.00", "UNI": "8.00"}
    return defaults.get(sym, "1.00")


@mcp.tool(name="fetchTokenPriceByAddress")
def fetch_token_price_by_address(addresses: list[dict]) -> dict:
    """Alchemy Prices API: POST /prices/v1/by-address — return current
    USD prices for tokens identified by (network, contract). Unknown
    pairs return a deterministic stub price."""
    with _lock():
        s = _load_state()
        out = []
        for pair in addresses or []:
            net = pair.get("network", "")
            addr = pair.get("address", "")
            if not _is_valid_chain(net):
                out.append({"network": net, "address": addr,
                            "error": f"Unsupported network: {net!r}"})
                continue
            if not _is_valid_addr(addr):
                out.append({"network": net, "address": addr,
                            "error": f"Malformed address: {addr!r}"})
                continue
            key = f"{net}:{_norm_addr(addr)}"
            price = s["prices"].get(key, "1.00")
            out.append({
                "network": net,
                "address": addr,
                "prices": [{"currency": "USD", "value": str(price),
                            "lastUpdatedAt": _now()}],
            })
        _record(s, "fetchTokenPriceByAddress", count=len(addresses or []))
        _save_state(s)
        return {"data": out}


@mcp.tool(name="fetchTokenPriceHistoryBySymbol")
def fetch_token_price_history_by_symbol(symbol: str, startTime: str,
                                        endTime: str,
                                        interval: str = "1d") -> dict:
    """Alchemy Prices API: POST /prices/v1/historical — return historical
    USD prices for a symbol between `startTime` and `endTime`. Mock
    generates a flat series at the current stub price."""
    with _lock():
        s = _load_state()
        base = s["prices"].get(symbol.upper(),
                               _stub_price_for_symbol(symbol.upper()))
        points = [
            {"value": str(base), "timestamp": startTime},
            {"value": str(base), "timestamp": endTime},
        ]
        _record(s, "fetchTokenPriceHistoryBySymbol",
                symbol=symbol, interval=interval)
        _save_state(s)
        return {"symbol": symbol.upper(), "currency": "USD",
                "data": points}


@mcp.tool(name="fetchTokenPriceHistoryByTimeFrame")
def fetch_token_price_history_by_time_frame(symbol: str, timeFrame: str,
                                            interval: str = "1d") -> dict:
    """Alchemy Prices API helper: same as
    `fetchTokenPriceHistoryBySymbol` but takes a natural-language /
    shorthand `timeFrame` (e.g. `"past-7d"`, `"last-week"`, `"ytd"`)."""
    with _lock():
        s = _load_state()
        base = s["prices"].get(symbol.upper(),
                               _stub_price_for_symbol(symbol.upper()))
        _record(s, "fetchTokenPriceHistoryByTimeFrame",
                symbol=symbol, timeFrame=timeFrame)
        _save_state(s)
        return {
            "symbol": symbol.upper(), "currency": "USD",
            "timeFrame": timeFrame, "interval": interval,
            "data": [{"value": str(base), "timestamp": _now()}],
        }


# ======================================================================
# Token API — JSON-RPC (alchemy_getTokenBalances / getTokenMetadata /
# getTokenAllowance)
# ======================================================================

@mcp.tool(name="getTokenBalances")
def get_token_balances(network: str = "eth-mainnet",
                       address: str = "",
                       tokenSpec: Any = None,
                       pageKey: str | None = None,
                       maxCount: int | None = None) -> dict:
    """Alchemy JSON-RPC `alchemy_getTokenBalances` — return ERC-20
    token balances for `address` on `network`. `tokenSpec` may be:
    `"erc20"`, `"DEFAULT_TOKENS"`, `"NATIVE_TOKEN"`, or an explicit list
    of contract addresses. Returns the raw JSON-RPC envelope; balances
    are hex strings (Alchemy's wire format)."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            _record(s, "getTokenBalances", network=network,
                    result="unsupported_chain")
            _save_state(s)
            return _rpc_err(-32602, f"Unsupported network: {network!r}")
        if not _is_valid_addr(address):
            _record(s, "getTokenBalances", network=network,
                    address=address, result="bad_address")
            _save_state(s)
            return _rpc_err(-32602,
                            f"invalid argument 0: must be a valid "
                            f"hex-encoded address, got {address!r}")
        entry = _addr_entry(s, network, address)
        balances = list(entry.get("token_balances") or [])
        if isinstance(tokenSpec, list):
            wanted = {_norm_addr(a) for a in tokenSpec}
            balances = [b for b in balances
                        if _norm_addr(b.get("contractAddress", ""))
                        in wanted]
            # If the caller passed explicit contracts not in storage,
            # surface them with a zero balance (matches Alchemy).
            present = {_norm_addr(b["contractAddress"]) for b in balances}
            for a in wanted - present:
                balances.append({"contractAddress": a,
                                 "tokenBalance": "0x" + "0" * 64,
                                 "error": None})
        elif tokenSpec == "NATIVE_TOKEN":
            balances = []  # native balance is via eth_getBalance instead
        cap = maxCount if isinstance(maxCount, int) and maxCount > 0 \
            else len(balances)
        page = balances[:cap]
        _record(s, "getTokenBalances", network=network,
                address=address, count=len(page))
        _save_state(s)
        return _rpc_ok({
            "address": address,
            "tokenBalances": page,
            "pageKey": None,
        })


@mcp.tool(name="getTokenMetadata")
def get_token_metadata(network: str = "eth-mainnet",
                       contractAddress: str = "") -> dict:
    """Alchemy JSON-RPC `alchemy_getTokenMetadata` — return name, symbol,
    decimals, logo for an ERC-20 contract. Unknown contracts return all
    fields null (Alchemy's behavior for contracts without metadata)."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            _record(s, "getTokenMetadata", network=network,
                    result="unsupported_chain")
            _save_state(s)
            return _rpc_err(-32602, f"Unsupported network: {network!r}")
        if not _is_valid_addr(contractAddress):
            _record(s, "getTokenMetadata", network=network,
                    contractAddress=contractAddress,
                    result="bad_address")
            _save_state(s)
            return _rpc_err(-32602,
                            f"invalid argument 0: must be a valid "
                            f"hex-encoded address, got {contractAddress!r}")
        meta = s["tokens"].get(
            f"{network}:{_norm_addr(contractAddress)}",
            {"name": None, "symbol": None, "decimals": None, "logo": None},
        )
        _record(s, "getTokenMetadata", network=network,
                contractAddress=contractAddress)
        _save_state(s)
        return _rpc_ok(meta)


@mcp.tool(name="getTokenAllowance")
def get_token_allowance(network: str = "eth-mainnet",
                        contract: str = "",
                        owner: str = "",
                        spender: str = "") -> dict:
    """Alchemy JSON-RPC `alchemy_getTokenAllowance` — return the ERC-20
    allowance the `spender` has been granted by `owner` on `contract`.
    Mock always returns "0" unless seeded in `state["tokens"]["allow"]`."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rpc_err(-32602, f"Unsupported network: {network!r}")
        for arg, val in (("contract", contract), ("owner", owner),
                         ("spender", spender)):
            if not _is_valid_addr(val):
                return _rpc_err(-32602,
                                f"invalid argument {arg}: {val!r}")
        key = f"{network}:{_norm_addr(contract)}:" \
              f"{_norm_addr(owner)}:{_norm_addr(spender)}"
        amt = (s.get("allowances") or {}).get(key, "0")
        _record(s, "getTokenAllowance", network=network,
                contract=contract, owner=owner, spender=spender)
        _save_state(s)
        return _rpc_ok(amt)


# ======================================================================
# Block & native balance (JSON-RPC)
# ======================================================================

@mcp.tool(name="eth_blockNumber")
def eth_block_number(network: str = "eth-mainnet") -> dict:
    """Standard JSON-RPC `eth_blockNumber` — return the latest block
    number on `network` as a hex string."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rpc_err(-32602, f"Unsupported network: {network!r}")
        latest = s["blocks"].get(network, {}).get("latest", 0)
        _record(s, "eth_blockNumber", network=network)
        _save_state(s)
        return _rpc_ok(hex(latest))


@mcp.tool(name="eth_gasPrice")
def eth_gas_price(network: str = "eth-mainnet") -> dict:
    """Standard JSON-RPC `eth_gasPrice` — return the current gas price
    (wei, hex). Mock returns `state["gas"][network]`, default 20 gwei."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rpc_err(-32602, f"Unsupported network: {network!r}")
        wei = int(s["gas"].get(network, "20000000000"))
        _record(s, "eth_gasPrice", network=network)
        _save_state(s)
        return _rpc_ok(hex(wei))


@mcp.tool(name="eth_getBlockByNumber")
def eth_get_block_by_number(network: str = "eth-mainnet",
                            blockNumberOrTag: str = "latest",
                            fullTransactions: bool = False) -> dict:
    """Standard JSON-RPC `eth_getBlockByNumber` — fetch a block by
    number, "latest", or "earliest". Returns `result: null` if the
    block does not exist."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rpc_err(-32602, f"Unsupported network: {network!r}")
        chain_blocks = s["blocks"].get(network, {})
        if blockNumberOrTag in ("latest", "pending", "safe", "finalized"):
            num = chain_blocks.get("latest", 0)
        elif blockNumberOrTag == "earliest":
            nums = [int(k) for k in chain_blocks.keys()
                    if k != "latest" and k.isdigit()]
            num = min(nums) if nums else 0
        else:
            try:
                num = _hex_to_int(blockNumberOrTag)
            except (TypeError, ValueError):
                return _rpc_err(-32602,
                                f"invalid block id: {blockNumberOrTag!r}")
        block = chain_blocks.get(str(num))
        if not block:
            # Surface a synthesised block for unseeded numbers (Alchemy
            # never returns null for mainnet historic blocks).
            block = _default_block(num)
        if not fullTransactions:
            block = dict(block)
            block["transactions"] = [
                t if isinstance(t, str) else t.get("hash")
                for t in block.get("transactions", [])
            ]
        _record(s, "eth_getBlockByNumber", network=network,
                blockNumberOrTag=blockNumberOrTag)
        _save_state(s)
        return _rpc_ok(block)


@mcp.tool(name="eth_getTransactionByHash")
def eth_get_transaction_by_hash(network: str = "eth-mainnet",
                                hash: str = "") -> dict:
    """Standard JSON-RPC `eth_getTransactionByHash`. Mock looks up the
    tx in `state["addresses"][*]["transactions"]`; returns `result:null`
    when not found."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rpc_err(-32602, f"Unsupported network: {network!r}")
        if not (isinstance(hash, str) and _TX_RE.match(hash)):
            return _rpc_err(-32602, f"invalid tx hash: {hash!r}")
        found = None
        for k, entry in s["addresses"].items():
            if not k.startswith(network + ":"):
                continue
            for tx in entry.get("transactions") or []:
                if tx.get("hash") == hash:
                    found = tx
                    break
            if found:
                break
        _record(s, "eth_getTransactionByHash", network=network, hash=hash,
                result="ok" if found else "not_found")
        _save_state(s)
        return _rpc_ok(found)


@mcp.tool(name="eth_getTransactionReceipt")
def eth_get_transaction_receipt(network: str = "eth-mainnet",
                                hash: str = "") -> dict:
    """Standard JSON-RPC `eth_getTransactionReceipt`. Synthesises a
    minimal receipt from a stored tx, or returns `result:null`."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rpc_err(-32602, f"Unsupported network: {network!r}")
        if not (isinstance(hash, str) and _TX_RE.match(hash)):
            return _rpc_err(-32602, f"invalid tx hash: {hash!r}")
        for k, entry in s["addresses"].items():
            if not k.startswith(network + ":"):
                continue
            for tx in entry.get("transactions") or []:
                if tx.get("hash") == hash:
                    _record(s, "eth_getTransactionReceipt",
                            network=network, hash=hash)
                    _save_state(s)
                    return _rpc_ok({
                        "transactionHash": hash,
                        "blockHash": tx.get("blockHash", "0x" + "0" * 64),
                        "blockNumber": tx.get("blockNum", "0x0"),
                        "from": tx.get("from", ""),
                        "to": tx.get("to", ""),
                        "cumulativeGasUsed": "0x5208",
                        "gasUsed": "0x5208",
                        "effectiveGasPrice": "0x4a817c800",
                        "status": "0x1",
                        "logs": [],
                        "logsBloom": "0x" + "0" * 512,
                        "type": "0x2",
                    })
        _record(s, "eth_getTransactionReceipt", network=network, hash=hash,
                result="not_found")
        _save_state(s)
        return _rpc_ok(None)


@mcp.tool(name="getTransactionReceipts")
def get_transaction_receipts(network: str = "eth-mainnet",
                             blockNumber: str | None = None,
                             blockHash: str | None = None) -> dict:
    """Alchemy JSON-RPC `alchemy_getTransactionReceipts` — return all
    transaction receipts for a single block (identified by `blockNumber`
    or `blockHash`)."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rpc_err(-32602, f"Unsupported network: {network!r}")
        if not blockNumber and not blockHash:
            return _rpc_err(-32602,
                            "one of blockNumber or blockHash is required")
        chain_blocks = s["blocks"].get(network, {})
        block = None
        if blockNumber:
            num = _hex_to_int(blockNumber) \
                if blockNumber not in ("latest", "pending") \
                else chain_blocks.get("latest", 0)
            block = chain_blocks.get(str(num))
        elif blockHash:
            for k, b in chain_blocks.items():
                if k == "latest":
                    continue
                if isinstance(b, dict) and b.get("hash") == blockHash:
                    block = b
                    break
        if not block:
            return _rpc_ok({"receipts": []})
        receipts = []
        for tx in block.get("transactions") or []:
            h = tx if isinstance(tx, str) else tx.get("hash")
            if not h:
                continue
            receipts.append({
                "transactionHash": h,
                "blockHash": block.get("hash"),
                "blockNumber": block.get("number", "0x0"),
                "from": (tx.get("from") if isinstance(tx, dict) else ""),
                "to": (tx.get("to") if isinstance(tx, dict) else ""),
                "gasUsed": "0x5208",
                "status": "0x1",
                "logs": [],
            })
        _record(s, "getTransactionReceipts", network=network,
                blockNumber=blockNumber, blockHash=blockHash,
                count=len(receipts))
        _save_state(s)
        return _rpc_ok({"receipts": receipts})


# ======================================================================
# Asset transfers (alchemy_getAssetTransfers)
# ======================================================================

@mcp.tool(name="fetchTransfers")
def fetch_transfers(fromBlock: str = "0x0",
                    toBlock: str = "latest",
                    fromAddress: str | None = None,
                    toAddress: str | None = None,
                    contractAddresses: list[str] | None = None,
                    category: list[str] | None = None,
                    order: str = "asc",
                    withMetadata: bool = False,
                    excludeZeroValue: bool = True,
                    maxCount: str = "0xA",
                    pageKey: str | None = None,
                    network: str = "eth-mainnet") -> dict:
    """Alchemy JSON-RPC `alchemy_getAssetTransfers` — list token / ETH
    transfers filtered by address, block range, contract, or category.
    Returns the JSON-RPC envelope with `result: {transfers, pageKey}`."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rpc_err(-32602, f"Unsupported network: {network!r}")
        if fromAddress and not _is_valid_addr(fromAddress):
            return _rpc_err(-32602,
                            f"fromAddress invalid: {fromAddress!r}")
        if toAddress and not _is_valid_addr(toAddress):
            return _rpc_err(-32602, f"toAddress invalid: {toAddress!r}")
        cats = set(category or ["external", "erc20"])
        max_n = _hex_to_int(maxCount) if maxCount else 10
        candidates = []
        for k, entry in s["addresses"].items():
            if not k.startswith(network + ":"):
                continue
            for tx in entry.get("transactions") or []:
                if fromAddress and _norm_addr(tx.get("from", "")) \
                        != _norm_addr(fromAddress):
                    continue
                if toAddress and _norm_addr(tx.get("to", "")) \
                        != _norm_addr(toAddress):
                    continue
                if tx.get("category") and tx["category"] not in cats:
                    continue
                if contractAddresses:
                    wanted = {_norm_addr(a) for a in contractAddresses}
                    rawc = tx.get("rawContract", {})
                    c = rawc.get("address") if isinstance(rawc, dict) \
                        else None
                    if _norm_addr(c or "") not in wanted:
                        continue
                if excludeZeroValue and (tx.get("value") in (0, "0", None)):
                    continue
                candidates.append(tx)
        # de-dup by hash (transactions stored on both sender + receiver)
        seen, out = set(), []
        for tx in candidates:
            h = tx.get("hash")
            if h and h in seen:
                continue
            if h:
                seen.add(h)
            out.append(tx)
        out.sort(key=lambda t: _hex_to_int(t.get("blockNum", "0x0")),
                 reverse=(order == "desc"))
        page = out[:max_n]
        _record(s, "fetchTransfers", network=network,
                fromAddress=fromAddress, toAddress=toAddress,
                count=len(page))
        _save_state(s)
        return _rpc_ok({"transfers": page, "pageKey": None})


# ======================================================================
# Multichain Token / Tx / NFT (Alchemy Data API)
# ======================================================================

@mcp.tool(name="fetchTokensOwnedByMultichainAddresses")
def fetch_tokens_owned_by_multichain_addresses(
        addresses: list[dict]) -> dict:
    """Alchemy Data API: POST /data/v1/tokens/by-address — return
    ERC-20 token balances + metadata for `(address, [networks])` pairs.
    Hex balances are decoded to decimal strings (matching the upstream
    `convertHexBalanceToDecimal` post-processor)."""
    with _lock():
        s = _load_state()
        tokens_out = []
        for pair in addresses or []:
            addr = pair.get("address", "")
            nets = pair.get("networks") or []
            if not _is_valid_addr(addr):
                continue
            for net in nets:
                if not _is_valid_chain(net):
                    continue
                entry = _addr_entry(s, net, addr)
                for tb in entry.get("token_balances") or []:
                    contract = tb.get("contractAddress")
                    meta = s["tokens"].get(
                        f"{net}:{_norm_addr(contract or '')}", {})
                    raw = tb.get("tokenBalance", "0x0")
                    try:
                        dec = str(_hex_to_int(raw))
                    except Exception:
                        dec = "0"
                    tokens_out.append({
                        "address": addr,
                        "network": net,
                        "tokenAddress": contract,
                        "tokenBalance": dec,
                        "tokenMetadata": {
                            "name": meta.get("name"),
                            "symbol": meta.get("symbol"),
                            "decimals": meta.get("decimals"),
                            "logo": meta.get("logo"),
                        },
                    })
                # Include native balance row
                native = entry.get("native_balance", "0")
                tokens_out.append({
                    "address": addr,
                    "network": net,
                    "tokenAddress": None,
                    "tokenBalance": str(_int_or_default(native, 0)),
                    "tokenMetadata": _native_metadata(net),
                })
        _record(s, "fetchTokensOwnedByMultichainAddresses",
                count=len(tokens_out))
        _save_state(s)
        return {"data": {"tokens": tokens_out, "pageKey": None}}


def _native_metadata(network: str) -> dict:
    sym = {
        "polygon-mainnet": "MATIC",
        "avax-mainnet": "AVAX",
        "bnb-mainnet": "BNB",
        "solana-mainnet": "SOL",
    }.get(network, "ETH")
    return {"name": sym, "symbol": sym, "decimals": 18, "logo": None}


@mcp.tool(name="fetchAddressTransactionHistory")
def fetch_address_transaction_history(addresses: list[dict],
                                      before: str | None = None,
                                      after: str | None = None,
                                      limit: int = 25) -> dict:
    """Alchemy Data API: POST /data/v1/transactions/history/by-address
    — return human-readable transaction history for one or more
    `(address, [networks])` pairs across chains. Each row is enriched
    with `date` (ISO 8601) and `ethValue` (decimal ETH) by the upstream
    client; mock pre-populates both fields."""
    with _lock():
        s = _load_state()
        txs = []
        for pair in addresses or []:
            addr = pair.get("address", "")
            nets = pair.get("networks") or []
            if not _is_valid_addr(addr):
                continue
            for net in nets:
                if not _is_valid_chain(net):
                    continue
                entry = _addr_entry(s, net, addr)
                for tx in entry.get("transactions") or []:
                    enriched = dict(tx)
                    enriched.setdefault("network", net)
                    enriched.setdefault("blockTimestamp", _now())
                    enriched.setdefault("date",
                                        enriched["blockTimestamp"])
                    enriched.setdefault("ethValue",
                                        str(_hex_to_int(
                                            enriched.get("value", "0x0"))
                                            / 1e18))
                    txs.append(enriched)
        page = txs[: max(1, min(limit or 25, 100))]
        _record(s, "fetchAddressTransactionHistory", count=len(page))
        _save_state(s)
        return {"transactions": page, "after": None, "before": None}


@mcp.tool(name="fetchNftsOwnedByMultichainAddresses")
def fetch_nfts_owned_by_multichain_addresses(
        addresses: list[dict],
        withMetadata: bool = True,
        pageKey: str | None = None,
        pageSize: int = 10) -> dict:
    """Alchemy Data API: POST /data/v1/nfts/by-address — return NFTs
    owned by one or more `(address, [networks], excludeFilters,
    includeFilters, spamConfidenceLevel)` triples across chains."""
    with _lock():
        s = _load_state()
        out = []
        for pair in addresses or []:
            addr = pair.get("address", "")
            nets = pair.get("networks") or ["eth-mainnet"]
            if not _is_valid_addr(addr):
                continue
            for net in nets:
                if not _is_valid_chain(net):
                    continue
                entry = _addr_entry(s, net, addr)
                for nft in entry.get("nfts") or []:
                    row = dict(nft) if withMetadata \
                        else {k: nft[k] for k in
                              ("contract", "id") if k in nft}
                    row["network"] = net
                    row["ownerAddress"] = addr
                    out.append(row)
        page = out[: max(1, min(pageSize, 100))]
        _record(s, "fetchNftsOwnedByMultichainAddresses",
                count=len(page))
        _save_state(s)
        return {"ownedNfts": page, "totalCount": len(out),
                "pageKey": None, "validAt": {"blockNumber": None}}


@mcp.tool(name="fetchNftContractDataByMultichainAddress")
def fetch_nft_contract_data_by_multichain_address(
        addresses: list[dict],
        withMetadata: bool = True) -> dict:
    """Alchemy Data API: POST /data/v1/nfts/contracts/by-address —
    return distinct NFT contracts an address owns across chains."""
    with _lock():
        s = _load_state()
        out = []
        for pair in addresses or []:
            addr = pair.get("address", "")
            nets = pair.get("networks") or ["eth-mainnet"]
            if not _is_valid_addr(addr):
                continue
            for net in nets:
                if not _is_valid_chain(net):
                    continue
                entry = _addr_entry(s, net, addr)
                seen = set()
                for nft in entry.get("nfts") or []:
                    c = (nft.get("contract") or {}).get("address")
                    if not c or c in seen:
                        continue
                    seen.add(c)
                    out.append({
                        "address": addr,
                        "network": net,
                        "contract": nft.get("contract", {}),
                        "totalBalance": "1",
                        "numDistinctTokensOwned": "1",
                        "isSpam": False,
                    })
        _record(s, "fetchNftContractDataByMultichainAddress",
                count=len(out))
        _save_state(s)
        return {"contracts": out, "pageKey": None}


# ======================================================================
# NFT V3 REST (single-chain GET endpoints)
# ======================================================================

@mcp.tool(name="getNFTsForOwner")
def get_nfts_for_owner(network: str = "eth-mainnet",
                       owner: str = "",
                       contractAddresses: list[str] | None = None,
                       withMetadata: bool = True,
                       orderBy: str | None = None,
                       excludeFilters: list[str] | None = None,
                       includeFilters: list[str] | None = None,
                       spamConfidenceLevel: str | None = None,
                       tokenUriTimeoutInMs: int | None = None,
                       pageKey: str | None = None,
                       pageSize: int | None = None) -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/getNFTsForOwner — return
    NFTs owned by `owner` on a single network, with optional metadata
    and contract / spam filters."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        if not _is_valid_addr(owner):
            return _rest_err(400, f"Malformed owner address: {owner!r}")
        entry = _addr_entry(s, network, owner)
        nfts = list(entry.get("nfts") or [])
        if contractAddresses:
            wanted = {_norm_addr(a) for a in contractAddresses}
            nfts = [n for n in nfts
                    if _norm_addr((n.get("contract") or {}).get("address",
                                                                ""))
                    in wanted]
        if not withMetadata:
            nfts = [{"contract": n.get("contract", {}),
                     "id": n.get("id", {})} for n in nfts]
        ps = pageSize or 100
        page = nfts[:ps]
        _record(s, "getNFTsForOwner", network=network, owner=owner,
                count=len(page))
        _save_state(s)
        return {"ownedNfts": page, "totalCount": len(nfts),
                "pageKey": None, "validAt": {"blockNumber": None,
                                             "blockHash": None,
                                             "blockTimestamp": _now()}}


@mcp.tool(name="getNFTsForContract")
def get_nfts_for_contract(network: str = "eth-mainnet",
                          contractAddress: str = "",
                          withMetadata: bool = True,
                          startToken: str | None = None,
                          limit: int | None = None,
                          tokenUriTimeoutInMs: int | None = None) -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/getNFTsForContract —
    return all NFTs in a contract."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        if not _is_valid_addr(contractAddress):
            return _rest_err(
                400, f"Malformed contract: {contractAddress!r}")
        out = []
        for k, entry in s["addresses"].items():
            if not k.startswith(network + ":"):
                continue
            for nft in entry.get("nfts") or []:
                if _norm_addr((nft.get("contract") or {}).get(
                        "address", "")) == _norm_addr(contractAddress):
                    out.append(nft if withMetadata
                               else {"contract": nft.get("contract"),
                                     "id": nft.get("id")})
        lim = limit or 100
        _record(s, "getNFTsForContract", network=network,
                contractAddress=contractAddress, count=len(out[:lim]))
        _save_state(s)
        return {"nfts": out[:lim], "pageKey": None}


@mcp.tool(name="getNFTMetadata")
def get_nft_metadata(network: str = "eth-mainnet",
                     contractAddress: str = "",
                     tokenId: str = "",
                     tokenType: str | None = None,
                     tokenUriTimeoutInMs: int | None = None,
                     refreshCache: bool = False) -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/getNFTMetadata."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        if not _is_valid_addr(contractAddress):
            return _rest_err(400,
                             f"Malformed contract: {contractAddress!r}")
        for k, entry in s["addresses"].items():
            if not k.startswith(network + ":"):
                continue
            for nft in entry.get("nfts") or []:
                c = (nft.get("contract") or {}).get("address", "")
                tid = (nft.get("id") or {}).get("tokenId")
                if (_norm_addr(c) == _norm_addr(contractAddress)
                        and str(tid) == str(tokenId)):
                    _record(s, "getNFTMetadata", network=network,
                            contractAddress=contractAddress,
                            tokenId=tokenId)
                    _save_state(s)
                    return dict(nft)
        # Synthesise an empty NFT shape for unseeded tokens, like Alchemy
        _record(s, "getNFTMetadata", network=network,
                contractAddress=contractAddress, tokenId=tokenId,
                result="not_found")
        _save_state(s)
        return {
            "contract": {"address": contractAddress},
            "id": {"tokenId": str(tokenId),
                   "tokenMetadata": {"tokenType": tokenType or "UNKNOWN"}},
            "title": "",
            "description": "",
            "tokenUri": {"raw": "", "gateway": ""},
            "media": [],
            "metadata": {},
            "timeLastUpdated": _now(),
        }


@mcp.tool(name="getContractMetadata")
def get_contract_metadata(network: str = "eth-mainnet",
                          contractAddress: str = "") -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/getContractMetadata."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        if not _is_valid_addr(contractAddress):
            return _rest_err(400,
                             f"Malformed contract: {contractAddress!r}")
        # Try to derive from any seeded NFT in this contract
        for k, entry in s["addresses"].items():
            if not k.startswith(network + ":"):
                continue
            for nft in entry.get("nfts") or []:
                c = (nft.get("contract") or {})
                if _norm_addr(c.get("address", "")) \
                        == _norm_addr(contractAddress):
                    _record(s, "getContractMetadata", network=network,
                            contractAddress=contractAddress)
                    _save_state(s)
                    return {
                        "address": contractAddress,
                        "contractMetadata": {
                            "name": c.get("name", ""),
                            "symbol": c.get("symbol", ""),
                            "totalSupply": c.get("totalSupply", "0"),
                            "tokenType": c.get("tokenType", "ERC721"),
                        },
                    }
        _record(s, "getContractMetadata", network=network,
                contractAddress=contractAddress, result="not_seeded")
        _save_state(s)
        return {"address": contractAddress,
                "contractMetadata": {"name": "", "symbol": "",
                                     "totalSupply": "0",
                                     "tokenType": "UNKNOWN"}}


@mcp.tool(name="getCollectionMetadata")
def get_collection_metadata(network: str = "eth-mainnet",
                            collectionSlug: str = "") -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/getCollectionMetadata
    — by OpenSea slug. Mock returns a stub when the slug is unseeded."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        col = ((s.get("collections") or {}).get(
            f"{network}:{collectionSlug}", {}))
        _record(s, "getCollectionMetadata", network=network,
                collectionSlug=collectionSlug)
        _save_state(s)
        return {
            "name": col.get("name", collectionSlug),
            "slug": collectionSlug,
            "description": col.get("description", ""),
            "externalUrl": col.get("externalUrl", ""),
            "bannerImageUrl": col.get("bannerImageUrl", ""),
            "twitterUsername": col.get("twitterUsername", ""),
            "discordUrl": col.get("discordUrl", ""),
        }


@mcp.tool(name="invalidateNFTContractCache")
def invalidate_nft_contract_cache(network: str = "eth-mainnet",
                                  contractAddress: str = "") -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/invalidateContract —
    no-op in the mock; returns success acknowledgment."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        _record(s, "invalidateNFTContractCache",
                network=network, contractAddress=contractAddress)
        _save_state(s)
        return {"success": True}


@mcp.tool(name="getOwnersForNFT")
def get_owners_for_nft(network: str = "eth-mainnet",
                       contractAddress: str = "",
                       tokenId: str = "") -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/getOwnersForNFT — return
    a list of owner addresses for a specific NFT."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        if not _is_valid_addr(contractAddress):
            return _rest_err(400,
                             f"Malformed contract: {contractAddress!r}")
        owners = []
        for k, entry in s["addresses"].items():
            if not k.startswith(network + ":"):
                continue
            for nft in entry.get("nfts") or []:
                c = (nft.get("contract") or {}).get("address", "")
                tid = (nft.get("id") or {}).get("tokenId")
                if (_norm_addr(c) == _norm_addr(contractAddress)
                        and str(tid) == str(tokenId)):
                    owners.append(k.split(":", 1)[1])
        _record(s, "getOwnersForNFT", network=network,
                contractAddress=contractAddress, tokenId=tokenId,
                count=len(owners))
        _save_state(s)
        return {"owners": owners}


@mcp.tool(name="getOwnersForContract")
def get_owners_for_contract(network: str = "eth-mainnet",
                            contractAddress: str = "",
                            withTokenBalances: bool = False,
                            pageKey: str | None = None) -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/getOwnersForContract."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        if not _is_valid_addr(contractAddress):
            return _rest_err(400,
                             f"Malformed contract: {contractAddress!r}")
        agg: dict[str, list[dict]] = {}
        for k, entry in s["addresses"].items():
            if not k.startswith(network + ":"):
                continue
            owner = k.split(":", 1)[1]
            for nft in entry.get("nfts") or []:
                c = (nft.get("contract") or {}).get("address", "")
                if _norm_addr(c) != _norm_addr(contractAddress):
                    continue
                agg.setdefault(owner, []).append({
                    "tokenId": (nft.get("id") or {}).get("tokenId"),
                    "balance": "1",
                })
        if withTokenBalances:
            owners = [{"ownerAddress": o, "tokenBalances": tbs}
                      for o, tbs in agg.items()]
        else:
            owners = list(agg.keys())
        _record(s, "getOwnersForContract", network=network,
                contractAddress=contractAddress, count=len(owners))
        _save_state(s)
        return {"owners": owners, "pageKey": None}


@mcp.tool(name="getFloorPrice")
def get_floor_price(network: str = "eth-mainnet",
                    contractAddress: str | None = None,
                    collectionSlug: str | None = None) -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/getFloorPrice — return
    marketplace floor prices (OpenSea, LooksRare)."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        key = f"{network}:{_norm_addr(contractAddress or '')}" \
              if contractAddress else f"{network}:{collectionSlug or ''}"
        floors = (s.get("floor_prices") or {}).get(key, {})
        _record(s, "getFloorPrice", network=network,
                contractAddress=contractAddress,
                collectionSlug=collectionSlug)
        _save_state(s)
        return {
            "openSea": {
                "floorPrice": floors.get("openSea", 0.0),
                "priceCurrency": "ETH",
                "retrievedAt": _now(),
                "collectionUrl": "",
            },
            "looksRare": {
                "floorPrice": floors.get("looksRare", 0.0),
                "priceCurrency": "ETH",
                "retrievedAt": _now(),
                "collectionUrl": "",
            },
        }


@mcp.tool(name="getSpamContracts")
def get_spam_contracts(network: str = "eth-mainnet") -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/getSpamContracts — list
    flagged-spam contracts on `network`."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        spam = (s.get("spam") or {}).get(network, [])
        _record(s, "getSpamContracts", network=network)
        _save_state(s)
        return {"contractAddresses": spam}


@mcp.tool(name="isSpamContract")
def is_spam_contract(network: str = "eth-mainnet",
                     contractAddress: str = "") -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/isSpamContract."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        spam = (s.get("spam") or {}).get(network, [])
        is_spam = _norm_addr(contractAddress) in {_norm_addr(a)
                                                  for a in spam}
        _record(s, "isSpamContract", network=network,
                contractAddress=contractAddress)
        _save_state(s)
        return is_spam


@mcp.tool(name="isAirdropNFT")
def is_airdrop_nft(network: str = "eth-mainnet",
                   contractAddress: str = "",
                   tokenId: str = "") -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/isAirdropNFT."""
    with _lock():
        s = _load_state()
        _record(s, "isAirdropNFT", network=network,
                contractAddress=contractAddress, tokenId=tokenId)
        _save_state(s)
        return False


@mcp.tool(name="isHolderOfContract")
def is_holder_of_contract(network: str = "eth-mainnet",
                          wallet: str = "",
                          contractAddress: str = "") -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/isHolderOfContract."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        if not _is_valid_addr(wallet):
            return _rest_err(400, f"Malformed wallet: {wallet!r}")
        entry = _addr_entry(s, network, wallet)
        for nft in entry.get("nfts") or []:
            c = (nft.get("contract") or {}).get("address", "")
            if _norm_addr(c) == _norm_addr(contractAddress):
                _record(s, "isHolderOfContract", network=network,
                        wallet=wallet)
                _save_state(s)
                return {"isHolderOfContract": True}
        _record(s, "isHolderOfContract", network=network, wallet=wallet)
        _save_state(s)
        return {"isHolderOfContract": False}


@mcp.tool(name="summarizeNFTAttributes")
def summarize_nft_attributes(network: str = "eth-mainnet",
                             contractAddress: str = "") -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/summarizeNFTAttributes."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        summary: dict[str, dict[str, int]] = {}
        total = 0
        for k, entry in s["addresses"].items():
            if not k.startswith(network + ":"):
                continue
            for nft in entry.get("nfts") or []:
                c = (nft.get("contract") or {}).get("address", "")
                if _norm_addr(c) != _norm_addr(contractAddress):
                    continue
                total += 1
                for trait in (nft.get("metadata") or {}).get(
                        "attributes", []) or []:
                    if not isinstance(trait, dict):
                        continue
                    t = trait.get("trait_type", "")
                    v = str(trait.get("value", ""))
                    summary.setdefault(t, {}).setdefault(v, 0)
                    summary[t][v] += 1
        _record(s, "summarizeNFTAttributes", network=network,
                contractAddress=contractAddress)
        _save_state(s)
        return {"contractAddress": contractAddress,
                "totalSupply": total, "summary": summary}


@mcp.tool(name="computeRarity")
def compute_rarity(network: str = "eth-mainnet",
                   contractAddress: str = "",
                   tokenId: str = "") -> list:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/computeRarity — return
    per-trait rarity scores. Mock returns the stored attributes with
    uniform 1.0 prevalence."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return [{"error": f"Unsupported network: {network!r}"}]
        for k, entry in s["addresses"].items():
            if not k.startswith(network + ":"):
                continue
            for nft in entry.get("nfts") or []:
                c = (nft.get("contract") or {}).get("address", "")
                tid = (nft.get("id") or {}).get("tokenId")
                if (_norm_addr(c) == _norm_addr(contractAddress)
                        and str(tid) == str(tokenId)):
                    _record(s, "computeRarity", network=network,
                            contractAddress=contractAddress,
                            tokenId=tokenId)
                    _save_state(s)
                    return [
                        {"value": str(t.get("value", "")),
                         "traitType": t.get("trait_type", ""),
                         "prevalence": 1.0}
                        for t in (nft.get("metadata") or {}).get(
                            "attributes", []) or []
                    ]
        _record(s, "computeRarity", result="not_found")
        _save_state(s)
        return []


@mcp.tool(name="getNFTSales")
def get_nft_sales(network: str = "eth-mainnet",
                  fromBlock: str | None = None,
                  toBlock: str | None = None,
                  order: str | None = None,
                  marketplace: str | None = None,
                  contractAddress: str | None = None,
                  tokenId: str | None = None,
                  buyerAddress: str | None = None,
                  sellerAddress: str | None = None,
                  taker: str | None = None,
                  limit: int | None = None) -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/getNFTSales — return
    historical NFT sales. Mock returns whatever is seeded under
    `state["nft_sales"][network]` filtered by `contractAddress`/`tokenId`."""
    with _lock():
        s = _load_state()
        sales = (s.get("nft_sales") or {}).get(network, [])
        if contractAddress:
            sales = [x for x in sales
                     if _norm_addr(x.get("contractAddress", ""))
                     == _norm_addr(contractAddress)]
        if tokenId is not None:
            sales = [x for x in sales if str(x.get("tokenId")) == str(tokenId)]
        if buyerAddress:
            sales = [x for x in sales
                     if _norm_addr(x.get("buyerAddress", ""))
                     == _norm_addr(buyerAddress)]
        if sellerAddress:
            sales = [x for x in sales
                     if _norm_addr(x.get("sellerAddress", ""))
                     == _norm_addr(sellerAddress)]
        if marketplace:
            sales = [x for x in sales
                     if x.get("marketplace") == marketplace]
        lim = limit or 100
        _record(s, "getNFTSales", network=network, count=len(sales[:lim]))
        _save_state(s)
        return {"nftSales": sales[:lim], "pageKey": None}


@mcp.tool(name="getContractsForOwner")
def get_contracts_for_owner(network: str = "eth-mainnet",
                            owner: str = "",
                            pageKey: str | None = None,
                            pageSize: int | None = None) -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/getContractsForOwner —
    distinct NFT contracts an owner holds tokens in."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        if not _is_valid_addr(owner):
            return _rest_err(400, f"Malformed owner: {owner!r}")
        entry = _addr_entry(s, network, owner)
        seen: dict[str, dict] = {}
        for nft in entry.get("nfts") or []:
            c = nft.get("contract") or {}
            a = c.get("address")
            if a and a not in seen:
                seen[a] = {
                    "address": a,
                    "totalBalance": "1",
                    "numDistinctTokensOwned": "1",
                    "isSpam": False,
                    "name": c.get("name"),
                    "symbol": c.get("symbol"),
                }
        _record(s, "getContractsForOwner", network=network, owner=owner,
                count=len(seen))
        _save_state(s)
        return {"contracts": list(seen.values()), "pageKey": None,
                "totalCount": len(seen)}


@mcp.tool(name="getCollectionsForOwner")
def get_collections_for_owner(network: str = "eth-mainnet",
                              owner: str = "",
                              pageKey: str | None = None,
                              pageSize: int | None = None) -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/getCollectionsForOwner."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        if not _is_valid_addr(owner):
            return _rest_err(400, f"Malformed owner: {owner!r}")
        entry = _addr_entry(s, network, owner)
        seen: dict[str, dict] = {}
        for nft in entry.get("nfts") or []:
            c = nft.get("contract") or {}
            slug = c.get("openSeaMetadata", {}).get("collectionSlug") \
                or c.get("name") or c.get("address")
            if slug and slug not in seen:
                seen[slug] = {
                    "collectionSlug": slug,
                    "name": c.get("name"),
                    "totalBalance": "1",
                }
        _record(s, "getCollectionsForOwner", network=network,
                owner=owner)
        _save_state(s)
        return {"collections": list(seen.values()), "pageKey": None}


@mcp.tool(name="searchContractMetadata")
def search_contract_metadata(network: str = "eth-mainnet",
                             query: str = "") -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/searchContractMetadata
    — keyword search across known NFT contracts. Mock substring-matches
    against the name/symbol of every seeded contract on `network`."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        q = (query or "").lower()
        contracts: dict[str, dict] = {}
        for k, entry in s["addresses"].items():
            if not k.startswith(network + ":"):
                continue
            for nft in entry.get("nfts") or []:
                c = nft.get("contract") or {}
                a = c.get("address")
                if not a or a in contracts:
                    continue
                if (q in (c.get("name") or "").lower()
                        or q in (c.get("symbol") or "").lower()):
                    contracts[a] = {
                        "address": a,
                        "name": c.get("name"),
                        "symbol": c.get("symbol"),
                        "tokenType": c.get("tokenType", "ERC721"),
                    }
        _record(s, "searchContractMetadata", network=network, query=query,
                count=len(contracts))
        _save_state(s)
        return {"contracts": list(contracts.values())}


@mcp.tool(name="reportSpam")
def report_spam(network: str = "eth-mainnet",
                address: str = "",
                isSpam: bool = True) -> dict:
    """Alchemy NFT V3 REST: GET /nft/v3/{key}/reportSpam — flag a
    contract as (not) spam. Mutating mock: persists to
    `state["spam"][network]`."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rest_err(400, f"Unsupported network: {network!r}")
        if not _is_valid_addr(address):
            return _rest_err(400, f"Malformed address: {address!r}")
        spam = s.setdefault("spam", {}).setdefault(network, [])
        cur = {_norm_addr(a) for a in spam}
        addr = _norm_addr(address)
        if isSpam and addr not in cur:
            spam.append(addr)
        elif not isSpam and addr in cur:
            spam[:] = [a for a in spam if _norm_addr(a) != addr]
        _record(s, "reportSpam", network=network, address=address,
                isSpam=isSpam)
        _save_state(s)
        return {"success": True}


# ======================================================================
# Solana DAS (minimal)
# ======================================================================

@mcp.tool(name="solanaGetAsset")
def solana_get_asset(network: str = "solana-mainnet",
                     id: str = "") -> dict:
    """Solana DAS RPC `getAsset` — return metadata for a single asset
    (NFT / compressed NFT / fungible mint) by its asset id (mint)."""
    with _lock():
        s = _load_state()
        if not _is_valid_chain(network):
            return _rpc_err(-32602, f"Unsupported network: {network!r}")
        asset = (s.get("solana") or {}).get("assets", {}).get(id)
        _record(s, "solanaGetAsset", id=id,
                result="ok" if asset else "not_found")
        _save_state(s)
        if not asset:
            return _rpc_err(-32602, f"Asset not found: {id!r}")
        return _rpc_ok(asset)


@mcp.tool(name="solanaGetAssetsByOwner")
def solana_get_assets_by_owner(network: str = "solana-mainnet",
                               ownerAddress: str = "",
                               page: int = 1,
                               limit: int = 100) -> dict:
    """Solana DAS RPC `getAssetsByOwner`."""
    with _lock():
        s = _load_state()
        owner_map = (s.get("solana") or {}).get("assets_by_owner", {})
        asset_ids = owner_map.get(ownerAddress, [])
        assets = [s["solana"]["assets"].get(a) for a in asset_ids
                  if s["solana"]["assets"].get(a)]
        start = max(0, (page - 1) * limit)
        page_items = assets[start: start + limit]
        _record(s, "solanaGetAssetsByOwner", ownerAddress=ownerAddress,
                count=len(page_items))
        _save_state(s)
        return _rpc_ok({"items": page_items, "total": len(assets),
                        "page": page, "limit": limit})


@mcp.tool(name="solanaGetTokenAccounts")
def solana_get_token_accounts(network: str = "solana-mainnet",
                              owner: str | None = None,
                              mint: str | None = None,
                              page: int = 1,
                              limit: int = 100) -> dict:
    """Solana DAS RPC `getTokenAccounts` — return SPL token accounts
    for an owner and/or mint."""
    with _lock():
        s = _load_state()
        ta = (s.get("solana") or {}).get("tokens", {})
        rows = []
        for key, accts in ta.items():
            if owner and key != owner:
                continue
            for a in accts:
                if mint and a.get("mint") != mint:
                    continue
                rows.append(a)
        start = max(0, (page - 1) * limit)
        page_items = rows[start: start + limit]
        _record(s, "solanaGetTokenAccounts", owner=owner, mint=mint,
                count=len(page_items))
        _save_state(s)
        return _rpc_ok({"token_accounts": page_items,
                        "total": len(rows),
                        "page": page, "limit": limit})


# ======================================================================
# Mock-only debug helpers
# ======================================================================

@mcp.tool(name="mock_debug_state")
def mock_debug_state() -> dict:
    """Mock-only: return the persisted state dict. Not exposed by the
    real Alchemy MCP server; used for verification."""
    with _lock():
        return _load_state()


@mcp.tool(name="mock_debug_seed_state")
def mock_debug_seed_state(state: dict, merge: bool = True) -> dict:
    """Mock-only: overwrite or shallow-merge the full state dict.

    `merge=True` (default) merges top-level keys into the current state
    (with deep-merge for `addresses`, `tokens`, `prices`, `blocks`,
    `gas`, `solana`). `merge=False` replaces the state entirely."""
    with _lock():
        if merge:
            cur = _load_state()
            for k, v in (state or {}).items():
                if isinstance(v, dict) and isinstance(cur.get(k), dict):
                    cur[k].update(v)
                elif isinstance(v, list) and isinstance(cur.get(k), list):
                    cur[k] = v
                else:
                    cur[k] = v
            _record(cur, "debug_seed_state", merge=True)
            _save_state(cur)
            return cur
        _record(state, "debug_seed_state", merge=False)
        _save_state(state)
        return state


@mcp.tool(name="mock_debug_seed_address")
def mock_debug_seed_address(network: str, address: str,
                            native_balance: str | None = None,
                            token_balances: list[dict] | None = None,
                            nfts: list[dict] | None = None,
                            transactions: list[dict] | None = None) -> dict:
    """Mock-only: seed (or merge into) a single
    `state["addresses"]["<network>:<address>"]` fixture. Useful from
    per-task setup scripts."""
    with _lock():
        s = _load_state()
        key = _addr_key(network, address)
        cur = s["addresses"].get(key, {
            "native_balance": "0", "token_balances": [],
            "nfts": [], "transactions": [],
        })
        if native_balance is not None:
            cur["native_balance"] = str(native_balance)
        if token_balances is not None:
            cur["token_balances"] = list(token_balances)
        if nfts is not None:
            cur["nfts"] = list(nfts)
        if transactions is not None:
            cur["transactions"] = list(transactions)
        s["addresses"][key] = cur
        _record(s, "debug_seed_address", network=network, address=address)
        _save_state(s)
        return cur


if __name__ == "__main__":
    mcp.run()
