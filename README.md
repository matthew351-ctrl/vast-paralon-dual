# vast-paralon-dual

Rent your GPU machines on **Vast.ai** and **Paralon Cloud** at the same time,
or switch each one fully to one of them. A Vast renter always wins the GPU.

Python 3 standard library only, plus Vast's official `vastai` CLI. It never
touches the Vast daemon, its packages or its containers. On each GPU machine
it stops and starts only Paralon's agent and the containers that agent
started.

**While Vast rents a machine, its Paralon agent is switched off.** A paused
Paralon node's agent keeps starting inference workers, model downloads and
GPU benchmarks by itself. Beside a Vast renter they take the renter's GPU
memory, and when one is killed Paralon re-benchmarks the node, which can mark
it failed. With the agent stopped nothing can start them; when the Vast
renter leaves, the agent is started again.

**Paralon's Jupyter image is kept on the machine.** Paralon rents a node only
while `ghcr.io/paraloncloud/jupyter` is present ("not ready: no jupyter image
present" otherwise), and Vast's daemon deletes images that no container uses
within minutes once images pass 10% of the disk. Every pass, the tool keeps a
created-but-never-started container on that image, which makes the delete
refuse.

## Before you start

This tool switches listings; it does not install Paralon. Each GPU machine
needs, first:

1. **A Vast host that works today** (the Vast daemon running, the machine
   verified on Vast).
2. **A Paralon node with its agent running on that machine.** Create the node
   in the Paralon dashboard, set it to **spot**, and run the agent install
   command it gives you on the machine. Check the dashboard shows the node
   online and verified.
3. **A way for this tool to run `docker` on that machine**, to switch the
   Paralon agent off during Vast rentals: run the tool on the machine itself
   (your user in the `docker` group), or give it an ssh key login there to a
   user in the `docker` group. Without it everything else works, but the
   agent keeps running beside Vast renters.
4. **Docker 29.2 or newer** on the machine. On a Vast host the NVIDIA runtime is
   in CDI mode, and older Docker cannot give the Paralon agent the GPU. Upgrade
   Docker following Vast's own host instructions.
5. **An NVIDIA CDI spec that matches the GPUs actually installed.** If the
   machine's GPUs ever change (a card added, removed or powered off), the old
   spec still lists them and the Paralon agent fails to start with
   `invoking the NVIDIA Container Runtime Hook directly ... is not supported`.
   Regenerate it:

   ```sh
   sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
   docker restart paralon-agent
   ```

## Where to run it

Either works; the setup below is the same.

- **On a control plane** (any always-on Linux box: a server, a Raspberry Pi):
  one install manages many machines. List each machine's Vast id and Paralon
  node id in one config (`init` pairs them for you). It talks to Vast through
  the `vastai` CLI and to Paralon through its API; to switch each machine's
  Paralon agent off it runs `docker` there over ssh (`user@host`, key login).
  Reacts within about a minute (one check per minute).
- **On the GPU machine itself:** one install, that machine in the config
  with `"paralon_agent": "local"`. Good for a single machine with nothing else
  always on. Here you can also turn on
  `"docker_events": true`, which wakes the watcher the moment a container
  starts, so it reacts in seconds instead of up to a minute.

Either way, the Paralon agent runs on each GPU machine whenever Vast is not
renting it: that is Paralon's software doing the rental. This tool decides
where each machine is listed, and switches the agent off while Vast rents.

## Quick start

Run these as your normal user on the control plane or the GPU machine (see
above):

```sh
# 1. Install (Python 3.8+; pipx keeps the Vast CLI out of the system Python,
#    which recent Ubuntu/Debian lock down)
git clone https://github.com/matthew351-ctrl/vast-paralon-dual.git ~/vast-paralon-dual && cd ~/vast-paralon-dual
sudo apt install -y pipx && pipx install vastai && pipx ensurepath
export PATH="$HOME/.local/bin:$PATH"      # until you log in again
vastai set api-key YOUR_VAST_API_KEY

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
  (Enter skips a machine), then how to reach Docker on each paired machine
  (`local` or `user@host`), and checks once that it can see the Paralon agent
  there. It only reads; nothing is stopped.
- Writes `~/.config/vast-paralon-dual/config.json`, the key file `env` next
  to it (mode 600, read by the service and by hand-run commands), and sets
  mode `dual` for each paired machine. Nothing changes on Vast or Paralon
  until the service runs.

For scripted installs, pass everything and it asks nothing:

```sh
python3 vast_paralon_dual.py init --policy keep_vast_listed \
    --pair 12345=NODE_ID --pair 12346=OTHER_NODE_ID \
    --agent 12345=local --agent 12346=you@gpu-machine-2 [--force]
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

- `status`: per machine, both sides, owed relists, whether the Paralon agent
  is running, the listing a relist would use, what the next pass would do and
  the last decisions. Writes nothing.
- `mode`: saves the mode for one machine (or all) and applies it once. The
  running service re-reads the state file at the start of every pass, so it
  follows the change from its next pass; a lock file next to the state file
  stops the two acting on a machine at the same moment.
  - `dual`: listed on both (the rules below).
  - `vast`: Paralon paused, Vast listed.
  - `paralon`: Vast unlisted, Paralon available once no Vast renter is
    running (unlisting never evicts a renter).
- `run`: the watcher (what the service runs). Machines with no mode are
  skipped until one is set.
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
   Then the machine's **Paralon agent is stopped**, and after it any
   container it started that is still running (inference, model download,
   benchmark). The agent goes first, so it is gone before the containers it
   watches stop. While a Paralon rental is being reclaimed the agent stays
   up, because it runs the reclaim; it is stopped once the rental has ended.
   A Paralon rental's container and Vast's containers are never touched. If
   Docker restarts and brings the agent back during the rental, the next pass
   stops it again.
   Every pass (any mode, rented or not) it also keeps Paralon's Jupyter image
   pinned: one `docker create`d container named `vpdual-pin-jupyter-<image id>`,
   never started. When Paralon publishes a new image, the next pass pins the
   new one and removes the old pin. It never pulls: if the image is not on the
   machine yet (the Paralon agent downloads it when it starts), nothing happens
   until it is.
2. **Paralon rented, no Vast renter** (dual mode): per `on_paralon_rental`
   (required, no default):
   - `keep_vast_listed`: Vast stays listed. Paralon is interruptible fill; a
     Vast booking triggers rule 1.
   - `unlist_vast`: Vast is unlisted until the Paralon rental ends.
3. **Rental ended**: the tool relists only what it took off itself, and
   starts the Paralon agent only if it stopped it. These flags live in the
   state file, saved before each unlist/pause/stop and cleared only when a
   later read shows the side back, so a restart never loses one. A side you
   unlist or pause by hand, or an agent you stop by hand, is left alone. The
   node shows offline on Paralon while its agent is stopped, so it is made
   available again on the pass after the agent reconnects.

Switching modes does not end a rental already running on either side.

### Relist prices

Just before it unlists a Vast machine, the tool saves that machine's current
listing from the same read: GPU price, min bid, disk price, upload and
download bandwidth prices, minimum GPU chunk, reserved discount rate, volume
offer (or `--vol_size 0` when there is none, so Vast does not create one),
and the offer end. The relist passes all of them back (`vastai list machine`
resets any term it is not given). A rolling offer (`--duration`, extended
daily by Vast) is relisted as rolling; a fixed end date is relisted as that
date, or, if it has passed by then, with the config's `duration`.

A relist or unlist is checked two ways: the CLI is run with `--raw` and its
JSON reply must say `success`, because `vastai` exits 0 and prints only a
message when the server refuses; and the "owed" flag is cleared only when
the next read shows the side back. If the Paralon node disappears from your
account while a Vast relist is owed, the relist still happens.

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
    {"vast_machine_id": 12345, "paralon_node_id": "node-id",
     "paralon_agent": "local"},
    {"vast_machine_id": 12346, "paralon_node_id": "other-node-id",
     "paralon_agent": "you@gpu-machine-2",
     "vast_listing": {"price_gpu": 0.60, "price_min_bid": 0.45}}
  ],
  "vast_listing": {"price_gpu": 0.40, "price_min_bid": 0.30, "duration": "14 days"}
}
```

| key | meaning |
| --- | --- |
| `on_paralon_rental` | `keep_vast_listed` or `unlist_vast` (required) |
| `machines` | list of `{vast_machine_id, paralon_node_id}`, each with an optional `paralon_agent` and an optional `vast_listing` that overrides the top-level one |
| `machines[].paralon_agent` | how to run `docker` on that machine to stop the Paralon agent during Vast rentals: `local`, or `user@host` for ssh (key login, `BatchMode`; the user must be in the `docker` group). Leave it out to not manage the agent |
| `vast_listing` | optional fallback listing terms (see Relist prices); keys map to `vastai list machine` flags: `price_gpu`, `price_min_bid`, `price_disk`, `price_inetu`, `price_inetd`, `discount_rate`, `min_chunk`, `end_date` or `duration` (not both), `vol_size`, `vol_price` |
| `state_file` | default `~/.local/state/vast-paralon-dual/state.json` |
| `poll_interval_s` | seconds between passes (default 60) |
| `paralon_min_get_interval_s` | minimum seconds between `GET /nodes` (default 60) |
| `paralon_max_calls_per_min` | hard cap on Paralon calls (default 4) |
| `reclaim_retry_s` | do not repeat a reclaim sooner than this (default 300) |
| `vastai_bin` | path to `vastai` (`init` writes the full path) |
| `docker_events` | `true` to also check immediately on any container start (needs Docker access; polling works without it) |

The old single-machine format (`vast_machine_id` and `paralon_node_id` at the
top level) still loads. An old (v0.1) state file is carried over only when
the config still has exactly one machine; with more, its mode is dropped
(logged) and you run `mode ...` again.

Notes: `current_rentals_running` is Vast's count, so an interruptible (bid)
renter counts as a Vast renter, as it should. Hand-run `status` or `mode`
commands make their own Paralon calls outside the service's per-minute
budget; `status` is one `GET /nodes`.

The key comes from `PARALON_API_KEY`, or else from the `env` file next to
the config. It is never logged.

## Service

The service runs as the user who installed it, so it uses that user's
`vastai` login and the files under `~/.config` and `~/.local/state`.
`service` fills in the user and paths; to write the unit by hand, copy
`systemd/vast-paralon-dual.service` and replace the `@...@` placeholders as
its header explains. If `docker_events` is on, that user must be in the
`docker` group.

systemd starts the service with a minimal `PATH`, so `vastai_bin` must be a
full path (`init` writes one; `run` warns if it cannot resolve the name). A
service with no mode set keeps running and warns until you run `mode ...`;
every pass re-reads the state file, so it picks the mode up without a
restart.

## API use

- Paralon (`https://paraloncloud.com/api/v1`, `Authorization: Bearer prlc_...`):
  `GET /nodes`, `PATCH /nodes/{id}` with `{"available": true|false}`,
  `POST /nodes/{id}/reclaim` (spot only). At most one `GET /nodes` a minute
  (shared by all machines), at most 4 calls a minute, exponential back-off on
  429, 409, 5xx and network errors. With many machines changing in the same
  minute, writes beyond the limit wait for a later pass.
- Docker on each GPU machine (only with `paralon_agent`; directly, or one
  `ssh -o BatchMode=yes user@host docker ...` per command):
  `docker ps -a --no-trunc --format ...` each pass while a Vast renter is on
  (or an agent start is owed), `docker stop -t 10 <id>`
  for the agent and then the containers it started, `docker start <id>` for
  the agent. The agent is found by its image (`ghcr.io/paraloncloud/agent`),
  because it renames its own container when it updates. Each pass also runs
  one short shell command for the Jupyter pin: `docker image inspect`, then
  `docker create` only if the pin is missing, and `docker rm` of older pins.
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
- Paralon's agent starts inference, downloads and benchmarks even on a
  paused node, and there is no API switch for them. That is why the agent is
  stopped during Vast rentals. Until a pass sees the Vast renter (up to
  `poll_interval_s`, or seconds with `docker_events` on the machine), a
  Paralon inference worker can still hold GPU memory beside it.
- A node whose benchmark was killed beside a Vast renter may show
  verification "failed". Paralon retries about every 31 minutes; it passes
  once a retry runs while no Vast renter is on the machine.
- The Jupyter pin needs `paralon_agent` (it uses the same Docker access).
  Without it, Vast's image cleanup can delete Paralon's Jupyter image and the
  node shows "not ready" until the Paralon agent downloads it again, which it
  does only when it starts.

## Compliance

Dual-listing is allowed: Vast's hosting agreement is non-exclusive. Do **not**
contact or solicit Vast customers to move to Paralon (non-circumvention). Do
not stop, mask or modify Vast's software, and do not bypass its apt pins.
This tool stops only Paralon's agent and its containers, never Vast's.

## Possible future work

A price floor (pause Paralon below a set price), options for paused/stopped
instances. If Paralon
adds an inference/benchmark switch for paused nodes, stopping the agent will
no longer be needed.

## Tests

```sh
python3 -m unittest discover -s tests -t tests
```

## License

MIT, see `LICENSE`.
