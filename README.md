# vast-paralon-dual

Rent one GPU machine on **Vast.ai** and **Paralon Cloud** at the same time, or
switch it fully to one of them.

- `mode dual`: listed on both. A Vast renter always wins the GPU: Paralon is
  paused and a Paralon **spot** rental is reclaimed.
- `mode vast`: Paralon paused, Vast listed.
- `mode paralon`: Vast unlisted, Paralon available.

Python 3 standard library only, plus Vast's official `vastai` CLI. It never
touches the Vast daemon, its packages or its containers.

## How it decides

Each pass (every 60 s by default) reads both sides first. If either read
fails (API down, CLI error), it does nothing and logs `hold`.

1. **Vast renter running** (any mode): Paralon is set `available: false`. If
   Paralon also has a running rental on a spot node, it is reclaimed (the
   Paralon renter gets 2 minutes' notice). An on-demand rental cannot be
   reclaimed: the tool warns.
2. **Paralon rented, no Vast renter** (dual mode): what Vast does is your
   choice, set in the config as `on_paralon_rental` (there is no default):
   - `keep_vast_listed`: Vast stays listed. Paralon is interruptible fill; a
     Vast booking triggers rule 1.
   - `unlist_vast`: Vast is unlisted until the Paralon rental ends.
3. **Rental ended**: the tool relists only what it took off itself. These
   flags live in a JSON state file, saved before each unlist/pause and
   cleared only after the relist succeeds, so a restart never loses one. If
   you unlist or pause a side by hand, the tool leaves it alone in dual mode.

In `mode paralon`, Paralon is made available only once no Vast renter is
running (unlisting Vast never evicts a renter). Switching modes does not end
a rental that is already running on either side.

Relists use **your** prices from `vast_listing` in the config, because an
unlisted Vast machine reports no live price.

The node should be **spot** in dual mode. The tool warns loudly if it is
on-demand.

## Install

On the GPU machine (or any always-on Linux box that can reach both APIs):

```sh
sudo useradd -m -r vpdual
sudo -u vpdual pip install --user vastai
sudo -u vpdual ~vpdual/.local/bin/vastai set api-key YOUR_VAST_API_KEY

sudo git clone <this repo> /opt/vast-paralon-dual
sudo mkdir -p /etc/vast-paralon-dual
sudo cp /opt/vast-paralon-dual/config.example.json /etc/vast-paralon-dual/config.json
sudo editor /etc/vast-paralon-dual/config.json   # machine id, node id, policy, prices
echo 'PARALON_API_KEY=prlc_...' | sudo tee /etc/vast-paralon-dual/env
sudo chmod 600 /etc/vast-paralon-dual/env
sudo install -d -o vpdual /var/lib/vast-paralon-dual
```

The Paralon key needs the **"My nodes"** permission. Find your Vast machine id
with `vastai show machines` and your Paralon node id in the Paralon dashboard.
If `vastai` is not on the service's PATH, set `vastai_bin` to its full path.

Try it without writing anything, then pick a mode and start the service:

```sh
cd /opt/vast-paralon-dual
export PARALON_API_KEY=prlc_...
python3 vast_paralon_dual.py -c /etc/vast-paralon-dual/config.json --dry-run mode dual
sudo -u vpdual -E python3 vast_paralon_dual.py -c /etc/vast-paralon-dual/config.json mode dual

sudo cp systemd/vast-paralon-dual.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vast-paralon-dual
journalctl -u vast-paralon-dual -f
```

## Commands

```
vast_paralon_dual.py [-c CONFIG] [--dry-run] [-v] run
vast_paralon_dual.py [-c CONFIG] [--dry-run] mode vast|paralon|dual
vast_paralon_dual.py [-c CONFIG] status
```

- `run`: the watcher (what the systemd unit runs).
- `mode`: saves the mode and applies it once. The running watcher picks it up
  on its next pass.
- `status`: both sides, the owed relists and the last decisions.
- `--dry-run`: logs what it would do; no API write, no state-file write.

## Config

See `config.example.json`. Main keys:

| key | meaning |
| --- | --- |
| `vast_machine_id` | your Vast machine id |
| `paralon_node_id` | your Paralon node id |
| `on_paralon_rental` | `keep_vast_listed` or `unlist_vast` (required) |
| `vast_listing` | prices used on relist; keys map to `vastai list machine` flags (`price_gpu` and `price_min_bid` required; optional `price_disk`, `price_inetu`, `price_inetd`, `discount_rate`, `min_chunk`, `end_date` or `duration`, `vol_size`, `vol_price`) |
| `poll_interval_s` | seconds between passes (default 60) |
| `paralon_min_get_interval_s` | minimum seconds between `GET /nodes` (default 60) |
| `paralon_max_calls_per_min` | hard cap on Paralon calls (default 4) |
| `reclaim_retry_s` | do not repeat a reclaim sooner than this (default 300) |
| `docker_events` | `true` to also check immediately on any container start (needs Docker access on the GPU machine; polling works without it) |

## API use

- Paralon (`https://paraloncloud.com/api/v1`, `Authorization: Bearer prlc_...`):
  `GET /nodes`, `PATCH /nodes/{id}` with `{"available": true|false}`,
  `POST /nodes/{id}/reclaim` (spot only). At most one `GET /nodes` a minute,
  at most 4 calls a minute, exponential back-off on 429, 409, 5xx and network
  errors. The key is read from `PARALON_API_KEY` and never logged.
- Vast (`vastai` CLI, checked against version 1.3.0):
  `vastai show machines --raw`, `vastai list machine <id> --price_gpu ...
  --price_min_bid ...`, `vastai unlist machine <id>`.

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

Not in v1: a price floor (pause Paralon below a set price), options for
paused/stopped instances, and keeping the Paralon image present
automatically.

## Tests

```sh
python3 -m unittest discover -s tests -t tests
```

## License

MIT, see `LICENSE`.
