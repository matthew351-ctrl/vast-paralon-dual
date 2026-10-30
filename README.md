# vast-paralon-dual

Rent your GPU machines on **Vast.ai** and **Paralon Cloud** at the same time,
or switch each one fully to one of them. A Vast renter always wins the GPU.

Python 3 standard library only, plus Vast's official `vastai` CLI. It never
touches the Vast daemon, its packages or its containers. On each GPU machine
it stops and starts only Paralon's agent, the containers that agent started,
and your own idle job if you name one (`idle_job_images`).

**Set `paralon_agent` for every machine.** It is how the tool runs `docker`
on the machine (`local`, or `user@host` over ssh), and the protections need
it: seeing a renter from the machine's own containers, acting on
`docker events` within seconds, stopping Paralon's agent and its leftovers
during a Vast rental, stopping your idle job during a Paralon rental, and
waiting for the GPU to be free before handing it to the other side. Without
it the tool has only Vast's running count and Paralon's API, read on a 60 s
pass.

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
3. **A way for this tool to run `docker` on that machine** (`paralon_agent`
   in the config; **required for the protections**): run the tool on the
   machine itself (your user in the `docker` group), or give it an ssh key
   login there to a user in the `docker` group. Without it the tool still
   switches listings, but only from Vast's running count and Paralon's API
   on a 60 s pass: no renter seen from the machine's containers, no
   `docker events`, the Paralon agent and its leftovers keep running beside
   Vast renters, your idle job keeps running beside Paralon renters, and
   nothing waits for the GPU to be free before the other side is listed.
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
  Paralon agent off it runs `docker` there over ssh (`user@host`, key login),
  and it follows each machine's `docker events` over the same ssh link, so a
  Vast renter's container starting pauses Paralon within seconds.
- **On the GPU machine itself:** one install, that machine in the config
  with `"paralon_agent": "local"`. Good for a single machine with nothing else
  always on.

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
python3 vast_paralon_dual.py init --policy unlist_vast \
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

**The machine decides, Vast's count is a hint.** For every machine with
`paralon_agent`, each pass also reads the machine itself in one command:
`docker ps` (which containers run) and `nvidia-smi` (GPU compute processes and
memory in use). A Vast renter is running when Vast's running count says so
**or** a Vast rental container (`C.<id>`) runs on the machine -- the count can
lag a container that already runs, and a new renter's container may still be
pulling its image. Paralon is made available again only when the machine shows
no Vast container, the Paralon agent running and the node online.

**Containers starting act at once** (`docker_events`, on by default): the
tool follows `docker events` on each machine (locally or over the ssh link).
When a non-Paralon container starts -- anything but Paralon's own containers,
this tool's pin and your `idle_job_images` -- every available Paralon node on
that machine is paused immediately, and a full pass follows within seconds
(it stops the agent and reads Vast). When a **Paralon rental** container
starts (Paralon labels a tenant's container `paralon.type=rental`; its agent,
benchmarks and inference workers are not rentals), the tool at once unlists
Vast (dual mode with `unlist_vast`, unless a Vast renter already runs) and
stops your idle job, instead of waiting up to a minute for the API to show
the rental.

Each pass (every 60 s by default, every `fast_poll_s` = 5 s while a machine
is recovering, see rule 3) reads both sides first: one
`vastai show machines --raw` and one `GET /nodes`, shared by all machines. If
either read fails, it does nothing and logs `hold`. A machine missing from a
read holds on its own.

1. **Vast renter running** (any mode): Paralon is set `available: false`. If
   Paralon also has a running rental on a spot node, it is reclaimed (the
   Paralon renter gets 2 minutes' notice). An on-demand rental cannot be
   reclaimed: the tool warns.
   Then the machine's **Paralon agent is stopped**, and after it any
   container it started that is still running (inference, model download,
   benchmark) is killed. The agent goes first (2 s grace), so it is gone
   before the containers it watches die; the leftovers are killed outright
   because Paralon's inference worker ignores a polite stop and would hold
   the renter's GPU memory for Docker's whole stop timeout. While a Paralon rental is being reclaimed the agent stays
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
   **A stored Vast instance keeps Paralon paused**, spot or on-demand (since
   v0.5.0; before, on-demand only). A renter's stopped instance (Vast's
   resident count above its running count) can resume at any moment, and
   unlisting Vast does not stop a resume: beside a spot rental the two would
   share the GPU for the 2-minute reclaim notice, beside an on-demand rental
   until one ends. The node stays registered (`available: false`, never
   deleted) and is made available again once nothing is stored. If Paralon
   is already rented when an instance is stored, the rental cannot be
   paused out: the tool warns, keeps the node paused so no new rental
   follows, and a resume is handled as above (reclaim). Vast does not count
   your own default job in either count.
2. **Paralon rented, no Vast renter** (dual mode): per `on_paralon_rental`.
   "Rented" is the API's `rented` **or** a Paralon rental container running
   on the machine (the API can lag it by a minute):
   - `unlist_vast` (the default): Vast is unlisted until the Paralon rental
     ends, so Vast cannot start a renter on the same GPU. With
     `docker_events` the unlist is sent the moment the rental's container
     starts, through the same path as a pass (flag and listing terms saved
     first). If the next passes show no rental after all (the container
     exited and the API never showed one), Vast is relisted as below.
   - `keep_vast_listed`: Vast stays listed. Paralon is interruptible fill; a
     Vast booking triggers rule 1, and the Vast renter waits for the spot
     reclaim (Paralon gives its renter 2 minutes' notice).
3. **Rental ended**: the tool relists only what it took off itself, and
   starts the Paralon agent only if it stopped it. After a Vast rental the
   machine is **recovering**: passes run every `fast_poll_s` (5 s) until the
   GPU is actually free (no compute process, at most `gpu_free_mib` in use),
   then the agent is started, then it waits until the node shows online on
   Paralon (a fresh `GET /nodes` once a minute, as Paralon's docs advise),
   and only then sets it available. These flags live in the
   state file, saved before each unlist/pause/stop and cleared only when a
   later read shows the side back, so a restart never loses one. A side you
   unlist or pause by hand, or an agent you stop by hand, is left alone. The
   node shows offline on Paralon while its agent is stopped, so it is made
   available again on the pass after the agent reconnects.
   After a **Paralon** rental (for a machine with `paralon_agent`), Vast is
   relisted only once no Paralon rental container runs on the machine and
   the GPU is free (no compute process, at most `gpu_free_mib` in use); until
   then the pass logs `hold: not relisting Vast yet -- ...` and passes run
   every `fast_poll_s`. An unreadable machine holds too. The agent's own
   benchmark or inference worker does not hold it (it may run as long as the
   agent does, and rule 1 kills it the moment a Vast renter arrives). A
   machine without `paralon_agent` is relisted as soon as the API shows the
   rental ended.

### Your idle job

`idle_job_images` names your own idle job (a miner, say): never a renter.
On a machine with `paralon_agent` it also **never runs under a Paralon
rental** (since v0.5.0). While a Paralon rental runs (the API, or a rental
container on the machine) and no Vast renter does, the tool stops it, and
starts it again once the rental is over -- only what it stopped itself (the
`idle_job` list in the state file, saved before the stop and cleared only
when a later read shows the job back).

- **A Vast default job** (a running `C.<id>` container whose image matches):
  stopped and started through Vast, because Vast's daemon restarts a default
  job it believes should run. The tool reads `vastai show instances --raw`,
  takes the instances whose `machine_id` is this machine and whose
  `image_uuid` starts with an `idle_job_images` prefix, and runs `vastai stop
  instance <id>` for each whose `intended_status` is `running`. To start it
  again: `vastai start instance <id>` while its `intended_status` is not
  `running`; it is back once it reads `running`.
- **A plain Docker container** you run yourself (any other name):
  `docker stop` / `docker start` on the machine.

The order after a Paralon rental: once the rental's container is gone and
the GPU is free, the pass relists Vast first and then starts the idle job.
The idle job can never hold the relist back: it starts after the relist, and
a running idle job counts as a free GPU (a Vast renter's arrival stops a
default job anyway). A relist that is held for another reason (no saved
price, a failed write) does not hold the idle job.

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
  "on_paralon_rental": "unlist_vast",
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
| `on_paralon_rental` | `unlist_vast` (default) or `keep_vast_listed` |
| `machines` | list of `{vast_machine_id, paralon_node_id}`, each with an optional `paralon_agent` and an optional `vast_listing` that overrides the top-level one |
| `machines[].paralon_agent` | how to run `docker` on that machine: `local`, or `user@host` for ssh (key login, `BatchMode`; the user must be in the `docker` group). **Required for the protections**: renter detection from the machine's own containers, `docker_events`, stopping Paralon's agent and its leftovers during Vast rentals, stopping your idle job during Paralon rentals, and the GPU-free waits before the agent starts and before Vast is relisted. Left out, the tool has only Vast's running count and Paralon's API on a 60 s pass |
| `vast_listing` | optional fallback listing terms (see Relist prices); keys map to `vastai list machine` flags: `price_gpu`, `price_min_bid`, `price_disk`, `price_inetu`, `price_inetd`, `discount_rate`, `min_chunk`, `end_date` or `duration` (not both), `vol_size`, `vol_price` |
| `state_file` | default `~/.local/state/vast-paralon-dual/state.json` |
| `poll_interval_s` | seconds between passes (default 60) |
| `paralon_min_get_interval_s` | minimum seconds between `GET /nodes` while nothing is happening (default 120; once a minute while recovering or rented) |
| `paralon_max_calls_per_min` | hard cap on Paralon calls (default 6; 4 before v0.4.7, when a 30 s burst read could fill the minute and hold a write back by a minute or more) |
| `reclaim_retry_s` | do not repeat a reclaim sooner than this (default 300) |
| `vastai_bin` | path to `vastai` (`init` writes the full path) |
| `docker_events` | follow `docker events` on each machine with `paralon_agent`: pause Paralon the moment a non-Paralon container starts, and unlist Vast (`unlist_vast`) and stop your idle job the moment a Paralon rental container starts (default `true`) |
| `gpu_free_mib` | the GPU counts as free with no compute process and at most this much memory in use (default 500); used before the agent starts after a Vast rental and before Vast is relisted after a Paralon rental |
| `fast_poll_s` | seconds between passes while a machine recovers after a Vast rental, or waits for the GPU before a Vast relist (default 5) |
| `idle_job_images` | image name prefixes of your own idle job (a miner, say) on the machine: never a renter, its GPU use does not block the agent's start, and (with `paralon_agent`) it is stopped while a Paralon rental runs and started again after -- a Vast default job through `vastai stop/start instance`, a plain container through Docker (default none) |

The old single-machine format (`vast_machine_id` and `paralon_node_id` at the
top level) still loads. An old (v0.1) state file is carried over only when
the config still has exactly one machine; with more, its mode is dropped
(logged) and you run `mode ...` again.

Notes: `current_rentals_running` is Vast's count -- a hint next to the
machine's own reading -- so an interruptible (bid) renter counts as a Vast
renter, as it should. Hand-run `status` or `mode`
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

### Stopping it for good, or removing it

First run `mode vast` for each machine (or once with no machine id, for
all). It relists on Vast what the tool unlisted, pauses Paralon, and starts
your idle job if the tool stopped it and no Paralon rental is running. A
machine the tool unlisted on Vast **stays unlisted while the service is
stopped**: the unit's `Restart=always` covers a crash, not a deliberate
`systemctl stop`. Check with `status` that nothing is owed, then:

```sh
python3 vast_paralon_dual.py mode vast
python3 vast_paralon_dual.py status        # "took off (owed)" all false
sudo systemctl disable --now vast-paralon-dual
sudo rm /etc/systemd/system/vast-paralon-dual.service && sudo systemctl daemon-reload
rm -r ~/.config/vast-paralon-dual ~/.local/state/vast-paralon-dual ~/vast-paralon-dual
```

If a Paralon rental is still running, `mode vast` relists Vast at once (a
Vast booking then reclaims a spot rental) but your idle job stays stopped
until the rental ends; and in the seconds after a rental, while its
container tears down, the relist waits for the GPU. Leave the service
running until `status` shows nothing owed. Removing the tool leaves
the Paralon agent and Vast's software as they are; the `vpdual-pin-jupyter-*`
container can be removed with `docker rm`.

## API use

- Paralon (`https://paraloncloud.com/api/v1`, `Authorization: Bearer prlc_...`):
  `GET /nodes`, `PATCH /nodes/{id}` with `{"available": true|false}`,
  `POST /nodes/{id}/reclaim` (spot only). One `GET /nodes` every 2 minutes
  (shared by all machines); once a minute while one of its nodes is rented or
  an ON-DEMAND node waits available (a late rental container is seen only
  through the API, and on-demand cannot be reclaimed);
  every 30 s for 4 minutes at a critical moment (a write that landed, a
  reclaim, a Vast renter or a Paralon container starting on a machine) and
  while a machine recovers after a Vast rental; at most 6 calls a minute; exponential
  back-off on every HTTP error and on network errors. No pause write is sent
  for a node that reads OFFLINE (it cannot be rented). With many machines changing in the same
  minute, writes beyond the limit wait for a later pass.
- Docker on each GPU machine (only with `paralon_agent`; directly, or one
  `ssh -o BatchMode=yes user@host docker ...` per command):
  each pass, one read of the machine (`docker ps -a --no-trunc --format ...`
  and `nvidia-smi`); on a Vast arrival `docker stop -t 2 <id>` for the agent,
  then `docker kill <id>` for the containers it started; `docker start <id>`
  for the agent; `docker stop -t 2 <id>` / `docker start <id>` for your
  idle job when it is a plain container; and `docker events` (one
  long-lived stream per machine; it prints each started container's name,
  image and `paralon.type` label). The agent is found by its image (`ghcr.io/paraloncloud/agent`),
  because it renames its own container when it updates. Each pass also runs
  one short shell command for the Jupyter pin: `docker image inspect`, then
  `docker create` only if the pin is missing, and `docker rm` of older pins.
  While the agent is stopped, once every `jupyter_refresh_s` (default 3600) a
  low-priority `docker pull -q ghcr.io/paraloncloud/jupyter:latest` on a
  background thread keeps the image current, so a node is not left "digest not
  approved" when Paralon publishes a new image during a Vast rental.
- Vast (`vastai` CLI, checked against version 1.3.0):
  `vastai show machines --raw`, `vastai list machine <id> --price_gpu ...
  --price_min_bid ... --price_disk ... --price_inetu ... --price_inetd ...
  --end_date ...`, `vastai unlist machine <id>`. With `idle_job_images`
  and a Vast default job only: `vastai show instances --raw` (while a
  Paralon rental runs beside your job, and when its start is owed), `vastai
  stop instance <id>` and `vastai start instance <id>` for that job --
  never a renter's instance. These two exit 0 whatever the server says, so
  the tool checks for their "stopping/starting instance <id>." reply.

## Known limits

- On a Vast host, the Paralon agent needs **Docker 29.2 or newer** to see the
  GPU. Upgrade using Vast's own host instructions; this tool does not.
- Since 2026-10-26 Paralon runs no inference or render jobs on **spot**
  nodes; an on-demand node can still get them, and either can get a
  verification benchmark (e.g. after a rental). The agent is still stopped
  during Vast rentals, so nothing of Paralon's can start beside the renter; in the seconds before the pass that stops
  it, a Paralon inference worker can still hold GPU memory beside it.
- **Container DNS.** Paralon's containers use Docker's default DNS. If your
  `daemon.json` names a LAN resolver that a firewall blocks for containers,
  every lookup inside a Paralon container waits for the timeout and the
  rental's Jupyter URL stays unreachable for about a minute (seen on our
  machines). Check: `docker run --rm --entrypoint getent
  ghcr.io/paraloncloud/jupyter:latest hosts paraloncloud.com` should answer
  in milliseconds.
- A node whose benchmark was killed beside a Vast renter may show
  verification "failed". Paralon retries about every 31 minutes; it passes
  once a retry runs while no Vast renter is on the machine.
- The Jupyter pin needs `paralon_agent` (it uses the same Docker access).
  Without it, Vast's image cleanup can delete Paralon's Jupyter image and the
  node shows "not ready" until the Paralon agent downloads it again, which it
  does only when it starts.
- **Without `paralon_agent` the gaps are a minute wide.** Vast is unlisted
  only once a pass sees `rented` on Paralon's API (a Vast booking in that
  minute shares the GPU until the spot reclaim ends the Paralon rental, 2
  minutes later), your idle job is not stopped at all, and Vast is relisted
  without waiting for the GPU.
- A Paralon rental is told apart from Paralon's other containers by the
  `paralon.type=rental` label Paralon puts on it. If Paralon stops labelling
  rentals, the immediate unlist and idle-job stop fall back to the next pass
  that sees `rented` on the API.
- A stored Vast instance keeps the node paused for as long as it is stored;
  a renter can leave a stopped instance for days. That is Paralon time given
  up so a resume never lands on a Paralon renter.
- With `keep_vast_listed`, Vast stays listed during a Paralon rental by
  design: a Vast booking reclaims the spot rental, and the two share the GPU
  for the 2-minute notice.
- Stopping a Vast default job takes Vast a few seconds; the immediate stop on
  a rental container starting narrows that window but cannot close it.

## Compliance

Dual-listing is allowed: Vast's hosting agreement is non-exclusive. Do **not**
contact or solicit Vast customers to move to Paralon (non-circumvention). Do
not stop, mask or modify Vast's software, and do not bypass its apt pins.
This tool stops only Paralon's agent and its containers, never Vast's.

## Possible future work

A price floor (pause Paralon below a set price). If Paralon
adds an inference/benchmark switch for paused nodes, stopping the agent will
no longer be needed.

## Tests

```sh
python3 -m unittest discover -s tests -t tests
```

## License

MIT, see `LICENSE`.
