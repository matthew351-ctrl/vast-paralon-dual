# vast-paralon-dual

Rent your GPU machines on **Vast.ai** and **Paralon Cloud** at the same time,
or switch each one fully to one of them. A Vast renter always wins the GPU.

Python 3 standard library only, plus Vast's official `vastai` CLI. It never
touches the Vast daemon, its packages or its containers.

## Quick start

Run these as your normal user on the GPU machine (or any always-on Linux box
that can reach both APIs):

```sh
# 1. Install
git clone https://github.com/matthew351-ctrl/vast-paralon-dual.git ~/vast-paralon-dual && cd ~/vast-paralon-dual
pip install --user vastai && vastai set api-key YOUR_VAST_API_KEY

# 2. Set the Paralon key (create it in the Paralon dashboard with the
#    "My nodes" permission)
export PARALON_API_KEY=prlc_...

# 3. Pair your Vast machines with Paralon nodes and choose the policy
python3 vast_paralon_dual.py init

# 4. Dry run: both sides, and what the tool would do. Changes nothing.
python3 vast_paralon_dual.py status

# 5. Start the service (runs as you, starts on boot)
python3 vast_paralon_dual.py service | sudo tee /etc/systemd/system/vast-paralon-dual.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now vast-paralon-dual
journalctl -u vast-paralon-dual -f
```

`init` prints the exact commands for steps 4 and 5 when it finishes. Set each
Paralon node to **spot** in the Paralon dashboard: only a spot rental can be
reclaimed when a Vast renter arrives.

## What `init` does

- Checks that `PARALON_API_KEY` is set and that `vastai` is installed and
  logged in. If not, it says what to run.
- Reads your machines (`vastai show machines --raw`) and nodes
  (`GET /nodes`), once each.
- Asks for the policy, then which Paralon node goes with each Vast machine
  (Enter skips a machine).
- Writes `~/.config/vast-paralon-dual/config.json`, the key file `env` next
  to it (mode 600, read by the service and by hand-run commands), and sets
  mode `dual` for each paired machine. Nothing changes on Vast or Paralon
  until the service runs.

For scripted installs, pass everything and it asks nothing:

```sh
python3 vast_paralon_dual.py init --policy keep_vast_listed \
    --pair 12345=NODE_ID --pair 12346=OTHER_NODE_ID [--force]
```

Other options: `--price-gpu`, `--price-min-bid`, `--duration` (fallback
listing terms), `--mode vast|paralon|dual`, `--state-file`, `--vastai-bin`.
Use `-c PATH` before `init` for a different config path; `init` then prints
commands that carry it.

## Commands

```
vast_paralon_dual.py [-c CONFIG] init [options]
vast_paralon_dual.py [-c CONFIG] status [VAST_MACHINE_ID]
vast_paralon_dual.py [-c CONFIG] [--dry-run] mode vast|paralon|dual [VAST_MACHINE_ID]
vast_paralon_dual.py [-c CONFIG] [--dry-run] [-v] run
vast_paralon_dual.py [-c CONFIG] service
```

- `status`: per machine, both sides, owed relists, the listing a relist would
  use, what the next pass would do and the last decisions. Writes nothing.
- `mode`: saves the mode for one machine (or all) and applies it once. The
  running service picks it up on its next pass.
  - `dual`: listed on both (the rules below).
  - `vast`: Paralon paused, Vast listed.
  - `paralon`: Vast unlisted, Paralon available once no Vast renter is
    running (unlisting never evicts a renter).
- `run`: the watcher (what the service runs). Machines with no mode are
  skipped.
- `service`: prints the systemd unit filled in for your account, Python and
  paths (template: `systemd/vast-paralon-dual.service`).
- `--dry-run`: logs what it would do; no API write, no state-file write.

## How it decides

Each pass (every 60 s by default) reads both sides first: one
`vastai show machines --raw` and one `GET /nodes`, shared by all machines. If
either read fails, it does nothing and logs `hold`. A machine missing from a
read holds on its own.

1. **Vast renter running** (any mode): Paralon is set `available: false`. If
   Paralon also has a running rental on a spot node, it is reclaimed (the
   Paralon renter gets 2 minutes' notice). An on-demand rental cannot be
   reclaimed: the tool warns.
2. **Paralon rented, no Vast renter** (dual mode): per `on_paralon_rental`
   (required, no default):
   - `keep_vast_listed`: Vast stays listed. Paralon is interruptible fill; a
     Vast booking triggers rule 1.
   - `unlist_vast`: Vast is unlisted until the Paralon rental ends.
3. **Rental ended**: the tool relists only what it took off itself. These
   flags live in the state file, saved before each unlist/pause and cleared
   only after the relist succeeds, so a restart never loses one. A side you
   unlist or pause by hand is left alone in dual mode.

Switching modes does not end a rental already running on either side.

### Relist prices

Just before it unlists a Vast machine, the tool saves that machine's current
listing from the same read: GPU price, min bid, disk price, upload and
download bandwidth prices, and the offer end date. The relist passes all of
them back (`vastai list machine` resets any term it is not given). If the
saved offer end has passed by then, the config's `duration` is used instead.

Config prices are only a fallback, for terms nothing was saved for, for
example a machine you unlisted by hand before installing and then switch to
`mode vast`. If there is neither a saved nor a config GPU price and min bid,
the tool does not list the machine and logs `hold: cannot relist Vast: ...`.

## Config

`init` writes it. Format (see `config.example.json`):

```json
{
  "on_paralon_rental": "keep_vast_listed",
  "machines": [
    {"vast_machine_id": 12345, "paralon_node_id": "node-id"},
    {"vast_machine_id": 12346, "paralon_node_id": "other-node-id",
     "vast_listing": {"price_gpu": 0.60, "price_min_bid": 0.45}}
  ],
  "vast_listing": {"price_gpu": 0.40, "price_min_bid": 0.30, "duration": "14 days"}
}
```

| key | meaning |
| --- | --- |
| `on_paralon_rental` | `keep_vast_listed` or `unlist_vast` (required) |
| `machines` | list of `{vast_machine_id, paralon_node_id}`, each with an optional `vast_listing` that overrides the top-level one |
| `vast_listing` | optional fallback listing terms (see Relist prices); keys map to `vastai list machine` flags: `price_gpu`, `price_min_bid`, `price_disk`, `price_inetu`, `price_inetd`, `discount_rate`, `min_chunk`, `end_date` or `duration`, `vol_size`, `vol_price` |
| `state_file` | default `~/.local/state/vast-paralon-dual/state.json` |
| `poll_interval_s` | seconds between passes (default 60) |
| `paralon_min_get_interval_s` | minimum seconds between `GET /nodes` (default 60) |
| `paralon_max_calls_per_min` | hard cap on Paralon calls (default 4) |
| `reclaim_retry_s` | do not repeat a reclaim sooner than this (default 300) |
| `vastai_bin` | path to `vastai` (`init` writes the full path) |
| `docker_events` | `true` to also check immediately on any container start (needs Docker access; polling works without it) |

The old single-machine format (`vast_machine_id` and `paralon_node_id` at the
top level) still loads, and an old state file is carried over for that one
machine.

The key comes from `PARALON_API_KEY`, or else from the `env` file next to
the config. It is never logged.

## Service

The service runs as the user who installed it, so it uses that user's
`vastai` login and the files under `~/.config` and `~/.local/state`.
`service` fills in the user and paths; to write the unit by hand, copy
`systemd/vast-paralon-dual.service` and replace the `@...@` placeholders as
its header explains. If `docker_events` is on, that user must be in the
`docker` group.

## API use

- Paralon (`https://paraloncloud.com/api/v1`, `Authorization: Bearer prlc_...`):
  `GET /nodes`, `PATCH /nodes/{id}` with `{"available": true|false}`,
  `POST /nodes/{id}/reclaim` (spot only). At most one `GET /nodes` a minute
  (shared by all machines), at most 4 calls a minute, exponential back-off on
  429, 409, 5xx and network errors. With many machines changing in the same
  minute, writes beyond the limit wait for a later pass.
- Vast (`vastai` CLI, checked against version 1.3.0):
  `vastai show machines --raw`, `vastai list machine <id> --price_gpu ...
  --price_min_bid ... --price_disk ... --price_inetu ... --price_inetd ...
  --end_date ...`, `vastai unlist machine <id>`.

## Known limits

- When Vast rents during a Paralon spot rental, a Vast **Docker** renter can
  share the GPU for up to about 2 minutes, until the reclaim lands.
- A Vast **VM** start is refused while the GPU is busy and retried by Vast's
  daemon; we observed it starting about 2.5 minutes late. If detection were
  slower than Vast's retry window, the VM start could fail. Keep
  `poll_interval_s` at 60 or below, and consider `docker_events`.
- On a Vast host, the Paralon agent needs **Docker 29.2 or newer** to see the
  GPU. Upgrade using Vast's own host instructions; this tool does not.
- Paralon turns its inference workload on by itself when a node goes
  available. There is no API switch for it.
- For fast Paralon starts, keep the Paralon Jupyter image on the machine. A
  stopped container that references the image stops Docker's image cleanup
  from removing it. This is not automated.

## Compliance

Dual-listing is allowed: Vast's hosting agreement is non-exclusive. Do **not**
contact or solicit Vast customers to move to Paralon (non-circumvention). Do
not stop, mask or modify Vast's software, and do not bypass its apt pins.

## Possible future work

A price floor (pause Paralon below a set price), options for paused/stopped
instances, and keeping the Paralon image present automatically.

## Tests

```sh
python3 -m unittest discover -s tests -t tests
```

## License

MIT, see `LICENSE`.
