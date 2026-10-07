# Reading capacity gate

## Workload and targets fixed before profiling

Supported boundary: one sequential reading job, 1000 or 10000 originals in one
user chat; 96 ASCII text characters/message (96000/960000 total, within the public
100000/1000000 character limits), canonical IDs, source dates and links. The last
100-message batch checkpoints 900/9900 already retained originals. Terminal history
grows through 0, 10, 100 completed 1000-message archives. Both revisions use exactly
these fixtures, real temporary SQLite/WAL and the same fake Telegram, with no
network, Jev, owner credentials or live changes.

Gate targets, chosen before measurement:

| Boundary | Target | Reason |
| --- | --- | --- |
| Final local queue tick, including cleanup and checkpoint | <= 1 second | At most four existing 250 ms owner-loop intervals of local work; meaningful pause/control responsiveness |
| MCP selected-job status and first 100-original snapshot response | <= 2 seconds each | Interactive progress/evidence, comfortably within the existing 30-second read timeout |
| Incremental traced Python peak during each measured operation | <= 256 MiB | Bounded single-owner local workload on ordinary 8 GiB desktops; excludes interpreter/imports and fixture preparation |

Record checkpoint writes/bytes, chosen durable job bytes, frozen snapshot bytes,
Python traced peak, process memory, response bytes and individual elapsed times.
Bytes describe JSON/storage cost, never tokens. Retention/whole-job JSON overhead
is measured, not assumed to justify a migration. The timing boundary excludes
selection preflight, external network time and the deliberate 250 ms idle sleep;
it includes queue cleanup, local batch processing, atomic SQLite writes and MCP
serialization/transport for response operations.

One bounded baseline/after invocation per revision uses identical fixtures and
trace boundaries, one sample per size/history/boundary. Instrumented operations
report their overhead explicitly; no percentile or throughput claim from a single
sample. Run on the same Windows host/Python/dependencies; parent reports seven
concurrent independent chats, 16 logical CPUs and approximately 32 GiB RAM.
Capture actual environment and load during each run. A noisy target breach gets
its trace retained and a quiet integrated confirmation recommendation. Conditional
conditional storage tasks remain outside this task and require separate acceptance under ADR 0011.

## Reproduction and results

Measured 2026-10-07 in the dedicated detached worktree. Baseline runtime is
`457b9f1b96e306db38b386dac247e111ff3d4abe` (pre-public baseline); after uses the pending
active-queue changes against that revision. Each JSON report records normalized
runtime-file SHA-256 values to bind the actual tested source independently of HEAD.
Windows 11 build 26200, CPython 3.12.14 x64, SQLite 3.53.1, 16 logical CPUs,
the same frozen dependencies. Seven concurrent chats/~32 GiB RAM are parent-reported;
no CPU isolation or dynamic utilization sample. Evidence clock/records are fixed;
real perf_counter measures local latency.

The harness starts through HTTP MCP, seeds a deterministic valid late checkpoint
and terminal fixtures outside the measurement boundary, then runs the real owner
tick and public status/results. Each boundary has one uninstrumented timing sample
and one identical cProfile/tracemalloc replay for allocation/byte evidence. Each
replay resets only its fixture job or temporary snapshots. This is a late-checkpoint
capacity sample, not end-to-end collection throughput, network latency or a live SLO.

| Messages | Archives | Baseline tick s | After tick s | After status s | After snapshot s | Baseline/after tick Python peak MiB |
| --- | --- | --- | --- | --- | --- | --- |
| 1000 | 0 | 0.0176 | 0.0255 | 0.0098 | 0.0285 | 4.16 / 4.16 |
| 1000 | 10 | 0.1419 | 0.0261 | 0.0136 | 0.0438 | 18.07 / 4.14 |
| 1000 | 100 | 2.1744 | 0.0283 | 0.0098 | 0.0624 | 143.38 / 4.14 |
| 10000 | 0 | 0.3438 | 0.3165 | 0.0494 | 0.2793 | 43.51 / 43.51 |
| 10000 | 10 | 0.3876 | 0.4644 | 0.0429 | 0.2505 | 57.44 / 43.51 |
| 10000 | 100 | 1.9755 | 0.2837 | 0.0368 | 0.1612 | 182.75 / 43.52 |

Two atomic job writes remain per final batch: the attempt is persisted before
the external read, then originals/progress are committed together. For 1000
messages they write approximately 1,597,657 bytes, with an 840,675-byte durable
job and 839,828-byte frozen snapshot. For 10000 they write 16,735,685 bytes, with
an 8,409,689-byte durable job and approximately 8,408,837-byte snapshot. IDs/elapsed
timestamps produce the few-byte differences retained in raw reports. Status
responses are about 4 KiB; 100-original MCP responses about 165 KiB, including
the two MCP JSON representations.

At 100 archives, baseline ticks decode 405 job records (340.01/377.86 MB of
JSON for the 1000/10000 workloads); after ticks decode only the chosen job twice
(1.51/16.65 MB), independent of archive depth. Baseline cumulative profile attributes
17.23 of 24.95 instrumented seconds to the 24 whole-queue decodes; JSON raw decoding
alone costs 17.91 seconds. The after profile has no whole-queue decodes: remaining
cost is active evidence decode/encode/checkpoint/snapshot work.

Instrumentation materially changes latency: baseline tick replay adds 0.10-7.41 s;
after adds 0.07-0.97 s. Targets use uninstrumented timing samples, allocation targets
use traced replay peaks. Python peak after is at most 43.52 MiB for ticks and
30.01 MiB for responses, below 256 MiB. The original Windows process-memory probe
returned unavailable; its zero counters were discarded. These runs establish
Python allocation peaks, not process RSS peaks. The reproduction probe now binds
the 64-bit Win32 handle correctly; its isolated functional check returned 239,972,352
bytes RSS after imports, outside the capacity boundary and not a gate measurement.

**Gate outcome:** current JSON job/snapshot storage is adequate for this declared
workload after active-only scheduling/cleanup. Baseline exceeded the 1-second tick
target at 100 archives; that archive-decode bottleneck is removed. All after timing
and Python-memory targets pass. Whole-job write amplification is measurable but
does not breach this gate, so ADR 0011 remains conditional and conditional storage tasks are not
activated. No target failed only in a noisy after segment; a quiet integrated
confirmation is optional, not evidence already obtained. A broader workload or
stricter target needs a new accepted gate before a migration.

Reproduce once on the chosen source from the repository root:

```text
uv run python scripts/profile_reading_capacity.py --label <revision> --background-load "<observed/reported host load>" --output <report.json>
```

Raw records and instrumented traces:

- [Baseline JSON](reading-capacity-baseline.json), [baseline profile](reading-capacity-baseline.profile.txt).
- [After JSON](reading-capacity-after.json), [after profile](reading-capacity-after.profile.txt).

No release/full-suite/CI/live Telegram validation is implied by this profiler.

2026-10-07 backlog closure: the six stored workload pairs were checked against
all 36 timing/allocation bounds; all pass. The current active-queue public check
passed separately. Conditional conditional storage tasks are closed as not planned, with no storage
rewrite or new performance run. [Conditions for reopening](../roadmap.md#conditional-storage).
