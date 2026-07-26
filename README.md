# perpl-mm — custom market-making bot for Perpl

A configurable market maker for [Perpl](https://perpl.xyz), the fully on-chain
perpetual futures exchange on Monad. Built in Python directly against the
official [Perpl API](https://github.com/PerplFoundation/api-docs) (Ed25519-signed
REST + WebSocket trading protocol) — no third-party trading libraries.

## What it does

- Streams the live L2 order book and market state over the market-data WebSocket.
- Computes a quote ladder around a configurable fair price (book mid, mark,
  last, or state mid): N levels per side, spread and level spacing in bps.
- Skews quotes against inventory and enforces a hard position cap. The
  reducing side uses Close orders first (Perpl orders are open/close-typed).
- Places post-only limit orders through the trading WebSocket with idempotent,
  strictly increasing request IDs seeded from the account's `lfr`, and re-quotes
  when the fair price drifts past a threshold.
- Safety: on-chain order expiry (`lb` = head block + TTL) so quotes die even if
  the bot crashes; cancel-all on shutdown; heartbeat sequence-gap detection with
  automatic reconnect and re-authentication.
- **Dry-run mode by default** — logs the quotes it would place without sending
  anything.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml   # edit strategy parameters
cp .env.example .env                 # add your API key (live trading only)
```

Create an API key with the `trade` scope at
[app.perpl.xyz/apikeys](https://app.perpl.xyz/apikeys) (or
[testnet.perpl.xyz/apikeys](https://testnet.perpl.xyz/apikeys)) and export:

```bash
export PERPL_API_KEY=...            # opaque key token
export PERPL_API_PRIVATE_KEY=...    # 32-byte Ed25519 seed, hex or base64
```

> **Note:** an API key alone cannot trade. You must also have a Perpl exchange
> account with collateral deposited (created via the web app). API keys can
> never withdraw funds.

## Run

```bash
# Dry run (no credentials needed): watch the quotes it would place
python -m perpl_mm --config config.yaml --dry-run

# Live on testnet first
python -m perpl_mm --config config.yaml --live

# Then flip `network: mainnet` in config.yaml when you're happy
```

## Strategy parameters (per market)

| Key | Meaning |
|-----|---------|
| `spread_bps` | Full quoted spread; each side sits at ±half of it around fair |
| `levels` / `level_step_bps` | Ladder depth and spacing per side |
| `order_size` | Size per level in base units |
| `max_position` | Hard inventory cap; the growing side stops quoting at the cap |
| `inventory_skew_bps` | Quote shift per 100% of cap held (long → quotes move down) |
| `requote_bps` | Fair-price drift that triggers a re-quote |
| `min_requote_interval_s` | Floor between re-quotes (respect WS rate limits) |
| `fair_source` | `book_mid`, `mark`, `last`, or `state_mid` |
| `leverage` | Leverage on opening orders |
| `post_only` | Maker-only quoting (rejects instead of crossing) |
| `order_ttl_blocks` | On-chain expiry for resting orders |

Multiple markets can be quoted at once — add more entries under `markets:`.

## Tests

```bash
pip install pytest
pytest
```

## Layout

```
perpl_mm/
  protocol.py     # message types, enums, price/size scaling
  auth.py         # Ed25519 request signing (REST + WS sign-in)
  rest.py         # /v1/pub/context, fills, order history
  market_data.py  # order book + market state stream, reconnect logic
  trading.py      # trading WS: auth, snapshots, orders, positions, rq idempotency
  strategy.py     # pure quote-ladder math (unit-tested)
  bot.py          # orchestrator: fair price, inventory, re-quote loop
  __main__.py     # CLI
```

## Risk warning

This bot places real leveraged orders when run with `--live` on mainnet. Start
in dry-run, then testnet, with small sizes. Market making can lose money —
especially in fast markets (adverse selection). You are responsible for your
own keys, funds, and parameters.
