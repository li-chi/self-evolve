# alchemy-mock

Mock MCP server that mirrors
[`@alchemy/mcp-server`](https://github.com/alchemyplatform/alchemy-mcp)
(the official Alchemy MCP server registered by `mcp-atlas` as the
`alchemy` entry — `npx @alchemy/mcp-server`). Alchemy is a Tier A
dependency because real keys cost money and answers are time-varying
(latest block, gas price, prices, balances, transaction history) — the
mock substitutes deterministic, file-backed fixtures.

## Tool surface

Tool names match the upstream verbatim (extracted from
`api/registerTools.ts`); parameters and response shapes match the
underlying Alchemy endpoints.

| group        | tools |
|--------------|-------|
| Discovery    | `listSupportedNetworks` |
| Prices       | `fetchTokenPriceBySymbol`, `fetchTokenPriceByAddress`, `fetchTokenPriceHistoryBySymbol`, `fetchTokenPriceHistoryByTimeFrame` |
| Token (RPC)  | `getTokenBalances`, `getTokenMetadata`, `getTokenAllowance` |
| Block / Tx   | `eth_blockNumber`, `eth_gasPrice`, `eth_getBlockByNumber`, `eth_getTransactionByHash`, `eth_getTransactionReceipt`, `getTransactionReceipts` |
| Transfers    | `fetchTransfers` |
| Multichain   | `fetchTokensOwnedByMultichainAddresses`, `fetchAddressTransactionHistory`, `fetchNftsOwnedByMultichainAddresses`, `fetchNftContractDataByMultichainAddress` |
| NFT V3       | `getNFTsForOwner`, `getNFTsForContract`, `getNFTMetadata`, `getContractMetadata`, `getCollectionMetadata`, `invalidateNFTContractCache`, `getOwnersForNFT`, `getOwnersForContract`, `getFloorPrice`, `getSpamContracts`, `isSpamContract`, `isAirdropNFT`, `isHolderOfContract`, `summarizeNFTAttributes`, `computeRarity`, `getNFTSales`, `getContractsForOwner`, `getCollectionsForOwner`, `searchContractMetadata`, `reportSpam` |
| Solana DAS   | `solanaGetAsset`, `solanaGetAssetsByOwner`, `solanaGetTokenAccounts` |
| Mock-only    | `mock_debug_state`, `mock_debug_seed_state`, `mock_debug_seed_address` |

The Beacon (`getBeacon*`), Debug (`debug*`), Trace (`trace*`),
Simulation (`simulate*`), Bundler (`getUserOperation*`,
`estimateUserOperationGas`, `getSupportedEntryPoints`,
`getMaxPriorityFeePerGas`), Wallet (`sendTransaction`, `swap`), and
extended Solana surfaces (`solanaGet{AssetProof,Assets,AssetSignatures,
NftEditions,AssetsByAuthority,AssetsByCreator,AssetsByGroup,
SearchAssets}`) from `@alchemy/mcp-server` are not implemented — none
of the current Toolathlon `alchemy` rollouts call them. Add by
following the existing `mcp.tool(...)` pattern in `server.py`.

## Response shapes

- **JSON-RPC tools** (`getTokenBalances`, `getTokenMetadata`,
  `getTokenAllowance`, `eth_*`, `getTransactionReceipts`,
  `fetchTransfers`, `solana*`) return the **full JSON-RPC envelope**
  `{"jsonrpc": "2.0", "id": <int>, "result": <obj>}` (or `"error":
  {"code": <int>, "message": "..."}`), matching exactly what the
  upstream `jsonRpcProvider` surfaces to the agent.
- **REST tools** (`fetchTokenPrice*`, `fetch*ByMultichainAddresses`,
  all `getNFT*` / `getOwners*` / `getFloor*` / `isSpamContract` / ...
  NFT V3 endpoints) return the response body directly, matching the
  Alchemy HTTPS REST APIs.

Validation: malformed addresses (`!= /^0x[0-9a-fA-F]{40}$/`) and
unsupported networks return:
- `_rpc_err(-32602, "...")` for JSON-RPC tools;
- `{"error": {"code": 400, "message": "..."}}` for REST tools.

## Supported networks

Verbatim copy of `SUPPORTED_NETWORKS` from
[`api/networks.ts`](https://github.com/alchemyplatform/alchemy-mcp/blob/main/api/networks.ts)
— 21 EVM mainnets (Ethereum, Arbitrum, Arbitrum Nova, Base, BNB,
Avalanche, Polygon, Polygon zkEVM, Optimism, zkSync, Linea, Scroll,
Blast, Gnosis, Celo, Mantle, Mode, Zora, Shape, Unichain, WorldChain)
plus their testnets, plus Solana (mainnet/devnet/testnet).
`listSupportedNetworks` returns the full structure exposed by the real
upstream tool.

State seeds default fixtures for `eth-mainnet`, `polygon-mainnet`,
`arb-mainnet`, `base-mainnet`, `opt-mainnet`, and `solana-mainnet`
(latest block + 20 gwei gas) — other supported chains validate as
known networks but return empty fixtures until seeded via
`mock_debug_seed_state` / `mock_debug_seed_address`.

## State

State lives in `$ALCHEMY_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/alchemy/state.json` inside the container;
`~/.openclaw/alchemy_mock/state.json` outside).

```jsonc
{
  "chains": ["eth-mainnet", "polygon-mainnet", "arb-mainnet",
             "base-mainnet", "opt-mainnet", "solana-mainnet"],
  "blocks": {
    "<chain>": {
      "latest": <int>,
      "<num>": {"hash":"0x...","parentHash":"0x...","timestamp":"0x...",
                "number":"0x...","transactions":[...],"gasUsed":"0x...",
                "gasLimit":"0x...","baseFeePerGas":"0x...","miner":"0x..."}
    }
  },
  "gas":   {"<chain>": "<wei str, decimal>"},
  "addresses": {
    "<chain>:<lower-0xaddr>": {
      "native_balance": "<wei str, decimal>",
      "token_balances": [{"contractAddress":"0x...",
                          "tokenBalance":"<hex str>"}],
      "nfts":           [{"contract":{"address":"0x...","name":"...",
                                       "symbol":"...","tokenType":"ERC721"},
                          "id":{"tokenId":"...","tokenMetadata":{...}},
                          "title":"...","tokenUri":{"raw":"...","gateway":"..."},
                          "metadata":{...}}],
      "transactions":   [{"hash":"0x...","from":"...","to":"...",
                          "value":"<hex>","blockNum":"0x...","asset":"ETH",
                          "category":"external","blockTimestamp":"...",
                          "rawContract":{"address":"0x...","value":"0x...",
                                         "decimal":"0x..."}}]
    }
  },
  "tokens":   {"<chain>:<0xcontract>": {"name","symbol","decimals","logo"}},
  "prices":   {"BTC":"65000.00", "ETH":"3500.00",
               "<chain>:<0xaddr>":"<usd>", ...},
  "allowances":   {"<chain>:<contract>:<owner>:<spender>": "<wei str>"},
  "spam":         {"<chain>": ["0x...", ...]},
  "floor_prices": {"<chain>:<contract-or-slug>":
                   {"openSea": <float>, "looksRare": <float>}},
  "collections":  {"<chain>:<slug>": {...}},
  "nft_sales":    {"<chain>": [{...sale row...}]},
  "solana": {
    "assets":          {"<asset_id>": {...DAS asset shape...}},
    "assets_by_owner": {"<owner>": ["<asset_id>", ...]},
    "tokens":          {"<owner>": [{"mint":"...","amount":"...",
                                     "decimals":<int>}]}
  },
  "calls": [{"op":"...","ts":"...",...}, ...]
}
```

The `calls` log is what the verifier consumes — every tool appends an
entry (including failed validation). File-locking via `fcntl.flock`
makes concurrent calls safe; per-rollout isolation should reset the
state dir between rollouts.

Seed a starting state by setting `ALCHEMY_MOCK_SEED_PATH` to a JSON
file in the same shape — it is loaded once if no `state.json` exists.
For incremental seeding from a task setup script, prefer the
`mock_debug_seed_state` / `mock_debug_seed_address` tools (they
shallow-merge over the live state).

## Run

```bash
# local
ALCHEMY_MOCK_STATE_DIR=$PWD/state python server.py

# docker (per-task compose snippet)
services:
  alchemy-mock:
    build:
      context: ../../mcp_servers/alchemy-mock
      dockerfile: Dockerfile
    image: mcp-env/alchemy-mock:0.1.0
    stdin_open: true
    tty: false
    environment:
      ALCHEMY_MOCK_STATE_DIR: /workspace/output/end_state/alchemy
      ALCHEMY_MOCK_SEED_PATH: /workspace/input/alchemy_seed.json
    volumes:
      - ${AGENT_WORKSPACE:-./workspace}:/workspace
```
