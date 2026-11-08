# Test harness

The harness measures how quickly a matching algorithm turns each dynamic update
into a gate decision, from initialization through the day's updates, and
compares it with a greedy baseline. Requires Python 3.11+ with no third-party
dependencies. Run from the repository root:

```sh
python3 -m smartgate_harness smartgate_harness/examples/sizes.json
```

Use `--algorithms naive` to run only some configured algorithms (for example
while the matcher is down) and `--runs flights-200,flights-400` to run a subset.

## How it connects

```text
stream server (one per algorithm) --webhook--> harness proxy --forward--> algorithm  POST /updates
                                                harness  <--WebSocket---  algorithm  /passenger/ws
```

For each algorithm, the harness holds the stream's webhook (through its proxy)
and a WebSocket to that algorithm's passenger feed. The proxy forwards every
request unchanged and relays the response, so the harness knows exactly when an
update was heard without replicating the stream. Each feed message is
attributed to the update it answers. Messages are matched by ordering, and the
feed's `generatedAt` simulated time breaks ties.

Each algorithm gets its own stream run. Every request carries a `gateState` built
from that algorithm's earlier decisions, so a single run can't be shared between
algorithms. A stream server allows only one active run, so the harness starts one
server per algorithm (or uses `stream_url` if you run them yourself).

Runs execute in parallel by default, which matches a live side-by-side demo.
Both algorithms then compete for CPU, so set `"parallel": false` for numbers
you plan to publish.

## Configuration

```json
{
  "output_dir": "harness-results",
  "parallel": true,
  "run_timeout_seconds": 1800,
  "algorithms": {
    "matcher": {
      "webhook_url": "http://127.0.0.1:9000/updates",
      "websocket_url": "ws://127.0.0.1:9000/passenger/ws",
      "health_url": "http://127.0.0.1:9000/health",
      "stream_port": 8701
    },
    "naive": {
      "command": ["{python}", "-m", "smartgate_naive", "--port", "9100"],
      "webhook_url": "http://127.0.0.1:9100/updates",
      "websocket_url": "ws://127.0.0.1:9100/passenger/ws",
      "health_url": "http://127.0.0.1:9100/health",
      "stream_port": 8702
    }
  },
  "runs": [
    {"name": "flights-400", "scenario": "fixtures/pearson-400-flights-seed42.json", "mode": "unpaced"},
    {"name": "flights-800", "generate": {"flights": 800, "seed": 42, "date": "2026-11-07"}, "mode": "paced", "speed": 960}
  ]
}
```

| Field | Meaning |
| --- | --- |
| `algorithms.*.command` | Optional process the harness starts and stops; `{python}` is this interpreter. Omit it for an algorithm you run yourself, such as `python matcher_server.py --port 9000`. |
| `algorithms.*.health_url` | Must return `200` within `startup_timeout_seconds` (default 30). Otherwise that algorithm's runs are recorded as `unavailable` and the rest continue. |
| `algorithms.*.stream_port` / `stream_url` | Port for a stream server the harness starts, or the control URL of one already running. |
| `runs[].scenario` | Fixture path, relative to the current directory. |
| `runs[].generate` | `flights`, `seed`, `date`, `rate` for `smartgate_stream generate`. Generated fixtures are cached under `output_dir/fixtures`. |
| `runs[].mode`, `runs[].speed` | Passed to the stream's `POST /runs`. Omitting `speed` uses the fixture's rate. |
| `runs[].algorithms`, `runs[].timeout_seconds` | Optional per-run subset and time limit. A run past its limit is stopped. |
| `harness_port` | Proxy port; default is any free port. |

Generation fails above roughly 1,200–1,400 flights because the fleet and gate
pools are full. Increase speed rather than size beyond that point.

## Results

Each invocation writes `output_dir/<UTC timestamp>/`:

- `summary.csv` / `summary.json`: one row per run and algorithm, rewritten after
  every run so partial sweeps are kept. This is the table to plot from.
- `runs/<run>/<algorithm>/events.csv`: one row per update.
- `runs/<run>/<algorithm>/waitlist.csv`: one row per flight that waited without a gate.
- `runs/<run>/<algorithm>/stream-status.json`: the stream's final run status, including lateness.
- `logs/`: output from the processes the harness started. The naive matcher logs
  its compute time per event.

| Metric | Definition |
| --- | --- |
| `response_seconds` | Update heard by the proxy → algorithm's HTTP response received. |
| `feed_latency_seconds` | Update heard by the proxy → first passenger-feed message for that update. The primary "time to assign". |
| `init_*` | The same for `InitializeAirport`, which carries the whole day. |
| `updates_without_feed` | Updates that changed nothing on the board. Feeds send only when the flight list changes. |
| `waitlist_*` | Per flight, simulated time spent on the feed with no gate (and not cancelled), until it gets one or leaves the board. |
| `max_lateness_seconds` | The stream's own measure of falling behind the paced schedule. |

Waitlist time is read from the feed, so it's the same measurement for every
algorithm. If a feed includes `waitlistedSince` / `assignedAt` per flight, those
values are copied into `waitlist.csv`. The naive matcher emits both. The
matcher's field names aren't settled yet. A gate the matcher reserves for later
entry shows as assigned on the board and so counts as no wait.

## Naive baseline

`python3 -m smartgate_naive --port 9100` serves the same contracts as the
matcher: `POST /updates`, `/passenger/ws` (full-list feed, no `gates` filter),
and `/health`. It ports the challenge's scoring-aware greedy reference
(`solution_scored.py`):

1. Aircraft on the ground keep their position. Planned visits that an update made
   invalid (timing, equipment, outage, or overlap) are repaired to the cheapest
   free gate, or waitlisted.
2. Waitlisted visits are placed in this order: turnbacks and other priority
   diversions first, then the most constrained aircraft, then the longest
   waiting. A visit is placed only on a gate that is free for its whole
   remaining stay, so it is retried after every later update. Aircraft that
   land while waitlisted are moved in once a gate frees up.
3. Cost terms: avoid remote stands except for emergencies that allow them, keep
   carriers in their terminal, and prefer the tightest size fit.

It tracks flight state with the stream's `GateState` and takes physical
occupancy from each request's `gateState`. In tests, every decision passes the
stream's own validation.
