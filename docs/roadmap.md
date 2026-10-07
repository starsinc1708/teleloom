# Teleloom roadmap

The first public release includes the implemented Telegram workspace:
accounts/bots, scoped history/search, frozen evidence, typed aggregates, exports,
compact fields/excerpts, sourced digest workflows, observed events, media,
confirmed mutations and owner-state preservation. [Reference](reference.md).

Future work is driven by reproducible [issues](https://github.com/starsinc1708/teleloom/issues),
measured limits and complete user workflows. Native model/skill workflow validation
across clients and broader real-world bug reports remain useful follow-up work;
current connection observations are in [acceptance](acceptance.md).

## Conditional storage

The [dated synthetic capacity gate](research/reading-capacity-gate.md) passed
after active-only queue scheduling. Storage migration is not implemented or
committed to a release. Reconsider only after a concrete larger workload or
stricter accepted target exposes a measured bottleneck, with preservation of
frozen originals, cursors, grants and unknown receipts. [ADR 0011](adr/0011-conditional-evidence-storage.md).

Unknown-delivery reconciliation can be improved with explicit owner-led evidence;
an absent search hit cannot establish that a send failed. Automatic resend remains
outside the accepted safety contract. No unmeasured market-leadership, token-savings
or provider-quality claims are made.
