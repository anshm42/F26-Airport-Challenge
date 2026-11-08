# Local demo verification

Verified on November 8, 2026 with the checked-in scenarios, licensed Julia/Gurobi,
and the native matcher. Fixtures, traffic rules, assignment scoring, and remote
stand policy were not changed.

| Replay | Source events accepted | Reassessments | Elapsed | Response p95 / maximum | Maximum pacing delay | Waiting at completion |
| --- | --- | --- | --- | --- | --- | --- |
| 400 flights, 480× | 159 / 159 | 334 | 173.63 s | 0.053 / 0.675 s | 0.190 s | 0 |
| 1,200 flights, unpaced | 464 / 464 | 703 | 146.98 s | 0.071 / 8.697 s | 0 (unpaced) | 0 |

Both runs completed with zero retries, skipped requests, rejected decisions, or
occupied-gate conflicts. The maximum response in the larger run was initialization;
all requests completed within the 10-second socket timeout. Response durations are
HTTP round trips, including synchronous matching. The unpaced verification also
compared the operator's predicted occupancy and waiting list with the updater's
accepted state after every decision. Its elapsed time includes those checks.

The ordinary suite passed 161 tests (six licensed tests skipped); the separate
licensed suite passed all six. Regression coverage includes mandatory relocatable
occupants, initial traffic-rule swaps, failed placement rollback, movement-driven
reassessment, outage reopening, delayed timers, timer replacement, invalid timers,
HTTP sequencing, dashboard WebSocket snapshots, and launcher shutdown. Both
frontends passed `npm run build`. The completed 400-flight launcher was interrupted
with SIGINT and released ports 8080/9000 and its Julia worker.

Run the interactive demo from the repository root:

```sh
python3 run_demo.py
```

This starts 1,200 flights paced at 60×, both backend services, Julia, and the two
Vite frontends. Open `http://127.0.0.1:5173/` for the operator and
`http://127.0.0.1:5174/board.html` for passengers. Ctrl-C stops the owned services;
rerun the command for a fresh day. See the README for prerequisites.

The full 60× day was not replayed in real time during this check (about 24 minutes).
The same 1,200-flight fixture completed unpaced; anchored pacing was exercised by
the full 480× replay and clock regressions. Browser visual inspection was not part
of this backend verification. Hosting, access controls, durable recovery, production
operations, and non-instantaneous aircraft moves remain outside this milestone.
