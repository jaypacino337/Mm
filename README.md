# mmbot — custom multi-venue market-making bot

One configurable market maker, four venues:

| Venue | What it is | Integration | Status |
|-------|------------|-------------|--------|
| [Perpl](https://perpl.xyz) | Perps CLOB on Monad | Native WS protocol ([api-docs](https://github.com/PerplFoundation/api-docs)), Ed25519-signed | Full |
| [Phoenix](https://www.ellipsislabs.xyz/) | Spot CLOB on Solana | Official [`phoenix-trade`](https://github.com/Ellipsis-Labs/phoenix-sdk) SDK | Full |
| [Variational](https://www.variational.io) | RFQ-based perps | Official [`variational`](https://pypi.org/project/variational/) SDK | Full (RFQ-maker mode) |
| [Arcus](https://arcus.xyz) | Perps + stock tokens on Robinhood Chain | Ed25519-signed REST | Best-effort — verify endpoints against [docs.arcus.xyz](https://docs.arcus.xyz) (see header of `mmbot/venues/arcus.py`) |

A shared, unit-tested strategy core computes quote ladders in human units
(spread / levels / inventory skew / position caps, all in bps); each venue
adapter handles its own wire protocol, scaling, and order semantics:

- **Perpl / Arcus / Phoenix** rest a post-only ladder and re-quote on drift.
- **Variational** has no maker order book — the adapter responds to incoming
  RFQs with two-sided quotes around the venue's indicative price instead.
- Perps venues map reduce-only quotes to close-type orders; spot (Phoenix)
  treats inventory as base-token balance vs. a configured target.

Safety throughout: **dry-run is the default** (log quotes, send nothing),
orders carry TTLs where the venue supports them, everything is cancelled on
shutdown, and WebSocket sequence gaps force clean reconnects.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# per-venue SDKs, only for the venues you run:
pip install phoenix-trade    # Phoenix
pip install variational      # Variational

cp config.example.yaml config.yaml   # keep only the venues you want
cp .env.example .env                 # secrets for live trading
```

Secrets per venue (see `.env.example`): Perpl and Arcus use Ed25519 API
keys created in their apps; Variational uses an API key/secret pair;
Phoenix signs with your Solana keypair file (`keypair_path` in config).

## Run

```bash
# Dry run — no credentials needed, quotes are logged, nothing is sent
python -m mmbot --config config.yaml --dry-run

# One venue at a time
python -m mmbot --dry-run --venue perpl

# Live (start on testnets / tiny sizes!)
python -m mmbot --config config.yaml --live
```

### Volume-focused preset

`config.volume.yaml` is a ready-made preset for maximizing genuine maker
turnover: single-level tight quotes, fast re-quotes, small position caps
with strong skew so inventory recycles. Check each venue's maker fee and
keep `spread_bps` above twice it. The bot only ever rests post-only
quotes filled by real counterparties — it does not and will not
self-match (wash trading is banned on all of these venues).

```bash
python -m mmbot --config config.volume.yaml --dry-run
```

## Strategy parameters (per market)

| Key | Meaning |
|-----|---------|
| `spread_bps` | Full quoted spread; each side sits at ±half around fair |
| `levels` / `level_step_bps` | Ladder depth and spacing per side |
| `order_size` | Size per level in base units |
| `max_position` | Hard inventory cap; the growing side stops at the cap |
| `inventory_skew_bps` | Quote shift per 100% of cap held |
| `requote_bps` | Fair-price drift that triggers a re-quote |
| `min_requote_interval_s` | Floor between re-quotes (mind venue rate limits) |
| `fair_source` | `book_mid`, `mark`, `last`, `state_mid` (venue-dependent) |
| `post_only` | Maker-only quoting |
| `order_ttl` | Order lifetime (blocks on Perpl, seconds elsewhere) |
| `venue:` | Venue-specific extras (market ids/pubkeys, target inventory, ...) |

## Tests

```bash
pip install pytest && pytest
```

## Layout

```
mmbot/
  core/
    strategy.py   # venue-neutral quote math (pure, unit-tested)
    venue.py      # Venue interface + shared types, default ladder cycle
    engine.py     # runs all venues concurrently, error isolation
    config.py     # multi-venue YAML config
  venues/
    perpl/        # native protocol client (auth, REST, market-data & trading WS)
    phoenix.py    # phoenix-trade SDK adapter (spot)
    variational.py# RFQ-maker adapter (overrides the quote cycle)
    arcus.py      # signed-REST adapter (endpoints configurable)
```

## Risk warning

`--live` places real (leveraged, where applicable) orders. Start in
dry-run, then testnet, with small sizes. Market making can lose money —
adverse selection is real. You are responsible for your keys, funds, and
parameters. The Arcus adapter's endpoint paths and signing layout must be
verified against the official docs before live use.
