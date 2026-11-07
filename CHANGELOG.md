# Changelog

Newest first. A release's section is its tag's notes. "Paralon API" in
each section names the responses that release's tests were written
against: the tool reads `GET /nodes` and writes `PATCH /nodes/{id}`
(`available`) and `POST /nodes/{id}/reclaim`, on
`https://paraloncloud.com/api/v1`.

## 0.9.0 (unreleased)

Web UI: one way to show values, clearer states, a phone layout for
the table, core vs advanced settings and a tiered change flow. No
change to the config format, the HTTP API or what the service does.

- **One cell formatter** on the web page: a missing value (null, NaN)
  reads "—" everywhere, true / false read yes / no, and a value spelled
  `None`, `True`, `False`, `nan` reads the same; the settings diff shows an
  absent key as "— (not set)". The bare "-" placeholders are gone.
- **Offer ends** on each card and in the table, worded "11-21 19:12 ·
  14 d left · rolling 7 d": the date, what is left, then the term
  (rolling N d, or fixed end), from the listing Vast already reports.
  Red under 7 days left on a fixed end, or under 1 day on a rolling one;
  an unlisted machine reads "unlisted".

## 0.8.0

Public-readiness release.

- **`doctor`**: checks the install, one PASS / WARN / FAIL row each with a
  one-line fix: Python, `vastai` (found, version, logged in), the Paralon
  key and one `GET /nodes`, the config, every machine id and node id, Docker
  on each `paralon_agent`, a relist price per machine, the state file and
  its lock, and the systemd unit. Exit 1 on any FAIL. `run` makes the same
  checks at start-up and refuses to start (exit 2) on a FAIL in the first
  five groups; `run --skip-doctor` bypasses it. `init` prints the table at
  its end.
- **Armed switch** (`"armed"` in the config). Unarmed, `run` makes every
  decision and logs it as `NOT ARMED: would ...`, but makes none of the
  writes (Vast list/unlist, Paralon available/paused and reclaim, agent and
  idle-job start/stop, the Jupyter pin); `status` and the web UI say so (an
  amber banner). `init` writes `"armed": false`. Arm with `arm` or the
  Settings tab, both of which run `doctor`.
- **`notify_command`**: a shell command run for warnings and confirmed side
  changes, the message on stdin and `VPDUAL_MACHINE` / `VPDUAL_LEVEL` /
  `VPDUAL_EVENT` in its environment; rate-limited per machine and event.
  README examples: ntfy, Telegram, mail.
- **Packaging and CI**: `pyproject.toml` (no runtime dependencies,
  `requires-python >= 3.8`, console script `vast-paralon-dual`), so
  `pipx install git+https://github.com/matthew351-ctrl/vast-paralon-dual.git`
  works; `python -m vpdual`; `service` writes a unit for a pipx install too.
  GitHub Actions: pytest on Python 3.8, 3.10 and 3.12, `ruff check`
  (E, F, W; line length 100). The code is ruff-clean.
- **`status --json`**, the bug-report attachment: versions, the `doctor`
  summary, the last 10 decisions per machine; ssh targets and the key
  redacted. README: "Reporting a problem" (community-maintained, not
  supported by Paralon or Vast).
- **`agent_control`** per machine: `docker` (default, as before),
  `systemd` (`systemctl --user stop|start` of `paralon_agent_unit` over the
  same local / ssh path) or `none` (the agent is never touched). The engine
  reaches the agent through one AgentControl interface; the decisions are
  unchanged.
- README: a risk statement at the top, pipx install first, the staged quick start (install,
  key, init, doctor, run unarmed, check, arm), more known limits; the
  "What's new" sections moved here.

Paralon API: unchanged since 0.6.0 -- the `GET /nodes` shape in
`tests/fixture_paralon_nodes.json` (captured 2026-11-04); not re-checked
against the live API for this release.

**Migrating from 0.7.x:** nothing to do. A config without `"armed"` is
armed, so an existing install keeps acting exactly as before. Only a config
written by the new `init` starts unarmed. The service now runs `doctor` at
start: if it refuses to start, `journalctl -u vast-paralon-dual` shows the
FAIL rows and their fixes (or add `--skip-doctor` to the unit's `run`).

## 0.7.0

- **The page no longer reads both sides itself.** The `run` service writes
  a status snapshot after every pass (`status.json` beside the state file)
  and `ui` serves it: an open page costs no `vastai`, Paralon or SSH call.
  Only when the service has not written one lately does the page read live,
  and it says which ("from the service, N s ago" or "live read").
- **An optional login for the web UI.** Set `VPDUAL_UI_PASSWORD` and the
  page asks for it. `ui --host` on a non-loopback address now refuses to
  start without it, unless you pass `--no-login`.
- **Relist backoff.** A machine Vast will not relist (unverified, de-listed)
  is tried 5 times, then every 10 minutes, with one warning, instead of
  every pass forever.
- The web UI keeps its connection open between refreshes (HTTP/1.1).

Paralon API: as 0.6.0 (no new call).

## 0.6.1

- **`init` suggests the pairs.** Where a Paralon node's name matches a Vast
  machine's hostname (ignoring case, spaces and dashes; exact, then prefix,
  then a close match), that node is offered as the default: Enter takes it,
  `-` skips, a number picks another. Nothing is paired without your answer,
  and `--pair` works as before.
- **Add a machine from a list.** The Settings tab's "Add a machine" offers
  your Vast machines and Paralon nodes that the config does not have yet,
  from the read the page already makes (no extra calls), with the node
  pre-selected by the same name match. You still review the change and
  save it; "Type the ids instead" adds one by hand.

Paralon API: as 0.6.0.

## 0.6.0

- **A web page: `ui`.** `python3 vast_paralon_dual.py ui` serves a local
  page (standard library only, nothing loaded from the internet) with each
  machine's Vast and Paralon side, what the next pass would do and the last
  decisions, as a grid of cards or a table. It is the same data as `status`.
  See [Web UI](#web-ui).
- **Machine names.** An optional `name` per machine in the config; without
  one, the hostname Vast reports for your machine is shown, else its id.
  `status` shows them too.
- **Mode switch from the page.** vast / paralon / dual per machine, after a
  confirm. It is saved to the state file only; the running service applies
  it on its next pass, so it stays the only process acting on Vast and
  Paralon.
- **Settings tab.** Edit the config from the page: every setting with its
  unit and default, and your machines. Changes are checked by the same
  validator the tool uses, shown as a diff before saving, backed up, and
  written atomically with mode 600. It refuses to remove a machine the tool
  still owes a relist. The API key is never shown.
- **Light and dark.** Follows your system, or an Auto / Light / Dark switch.
- **Help on every term.** An (i) beside each label explains it, on hover,
  keyboard focus or tap, and always opens fully on screen.
- **Safe by default.** It listens on 127.0.0.1 only; a LAN address prints a
  warning. Every change needs a per-run token from the page and is refused
  from other web sites and from unexpected host names.

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

Earlier releases (0.1 to 0.5.x): see the git history.

Paralon API: `GET /nodes` as in `tests/fixture_paralon_nodes.json`,
as committed with this release (2026-11-04).

