---
name: Report a problem
about: Something vast-paralon-dual did (or did not do) that you did not expect
title: ""
labels: ""
assignees: ""
---

<!--
This tool is community-maintained. It is NOT made, endorsed or supported by
Paralon or by Vast.ai: report its problems here, not to them.

NEVER paste your config's `env` file, your Paralon key (prlc_...) or your
Vast API key. Read every attachment before posting it.
-->

**Version:** <!-- `vast-paralon-dual --version` -->

### What I expected

### What happened instead

### status --json

<!--
`vast-paralon-dual status --json > status.json` (from a clone:
`python3 vast_paralon_dual.py status --json`), attached or pasted below.
It holds the versions, the `doctor` summary, both sides as the tool reads
them, what the next pass would do and the last 10 decisions per machine.
The Paralon key is never in it and every ssh `paralon_agent` reads
`user@host`; your machine ids, node ids and machine names are in it --
edit them out if you prefer.
-->

```json

```

### Log around the problem

<!--
`journalctl -u vast-paralon-dual --since "1 hour ago"` -- read it first: it
has your machine ids and, for an ssh error, your `user@host`.
-->

```

```
