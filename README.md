# vast-paralon-dual

List one GPU machine on **Vast.ai** and **Paralon Cloud** at the same time. When either
platform rents it, the other side is paused; when the rental ends, it is relisted on the
same terms. Pure Python standard library, one service, one web page.

> **This tool unlists and relists your Vast machine and pauses/resumes your Paralon node.**
> A wrong move costs rental income. It installs **unarmed**: it logs what it would do and
> changes nothing until you run `arm`. Run it unarmed for a day first.

## Before you start (the platforms' setup, not this tool's)

- A Vast machine that is listed, verified and has rented at least once
- A Paralon node that shows **available** in your Paralon dashboard
- The `vastai` CLI (1.3.0+) logged in on the box that will run this
- A Paralon API key with the nodes permission
- Docker running on each machine (the Paralon agent is a container)

If `doctor` fails on one of these, fix it on the platform — [Vast host docs](https://docs.vast.ai/host/hosting-overview) · [Paralon host docs](https://paraloncloud.com/docs/add-a-node).

## Quick start

    pipx install git+https://github.com/matthew351-ctrl/vast-paralon-dual.git
    export PARALON_API_KEY=...              # init saves it to ~/.config/vast-paralon-dual/env, mode 600
    vast-paralon-dual init                  # pairs Vast machines with Paralon nodes, prints the command that installs the service
    vast-paralon-dual doctor                # one table: pass / warn / fail, with the fix for each
    sudo systemctl start vast-paralon-dual  # runs UNARMED: "NOT ARMED: would ..." in the log
    vast-paralon-dual status                # read it for a day
    vast-paralon-dual arm                   # now it acts

Web page: `vast-paralon-dual ui` → http://127.0.0.1:8787 (set `VPDUAL_UI_PASSWORD` and pass `--host 0.0.0.0` to open it to your LAN).

## What it does, in five rules

1. A Vast renter pauses the Paralon node; a Paralon renter unlists the Vast machine.
2. When the rental ends, the side this tool paused is restored on its saved terms. It never touches a side it didn't pause.
3. A stopped Vast instance ("stored") keeps Paralon paused: the renter can resume onto that GPU.
4. Vast's own count is a hint; Docker on the machine is the truth.
5. Every write is flagged on disk before it is sent and confirmed on the next read. Nothing is done twice.

Modes per machine: `dual` (both), `vast`, `paralon`. Change them in the UI or with `mode`.

## Get help

`vast-paralon-dual status --json` is the bug report — attach it with the last 50 journal lines.
[Open an issue](../../issues/new/choose). Community-maintained; not supported by Vast or Paralon.

Full reference — every setting, the decision rules in detail, the HTTP API, removal:
**[docs/REFERENCE.md](docs/REFERENCE.md)**. Changes: [CHANGELOG.md](CHANGELOG.md).
