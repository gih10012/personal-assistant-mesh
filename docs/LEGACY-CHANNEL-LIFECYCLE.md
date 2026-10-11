# Legacy communication loop lifecycle

PAM-009c/d source slice. This is an owner-only **in-process** observation and
controlled-stop interface, not an HTTP endpoint, grant, lease or production
handoff proof. It does not change native Shell/network/worker/Leader permissions.
No production receiver was started, stopped or moved to implement this slice.

`server._start_channel` retains the same `(channel, supervisor)` result. An
explicit fenced `ChannelSupervisor` is unchanged. When there is no supervisor,
the existing poll/send loops are now owned by `channel.loop_lifecycle`, a
`ChannelLoopLifecycle`. The default startup still creates one poll and one send
thread, with the same callbacks and retry cadence. No witness/candidate/term or
second receiver is created. Disabled transport still starts nothing.

An owner who holds the `channel` supplied to `serve(..., ready=...)` can use:

```python
group = channel.loop_lifecycle
before = group.snapshot()          # no transport/authority calls
returned = group.stop(timeout=5)    # existing event; only these owned threads
after = group.snapshot()           # observe the same handles, do not restart
```

`stop` accepts a finite 0–60 second bound. It requests stop on this Channel's
existing event and joins its actual poll/send Thread handles against one shared
monotonic deadline. It skips joining the current thread. It never kills a
transport, resets the event, replays an operation, stops the HTTP server or
touches an unrelated Leader/history task. The server's normal shutdown now also
uses this bounded cleanup. A 40-second iLink long poll may outlast the five-second
default: `False` remains an unclosed observation, not permission to replace it.
Observe or join the same group again after the original callback returns.

The report includes actual thread handles/liveness, observed loop entries and
returns, and each Channel loop's admitted/returned/active callback counts. These
counts include the entire `poll_once`/`send_once` callback: transport return alone
does not count if ingestion or send-result recording is still blocked. Admission
and lifecycle stop share the Channel loop lock, so no new loop callback is
admitted after that controlled stop boundary. Existing admitted work is allowed
to return with its original identity. Exceptions also unwind a callback; a
callback return does **not** mean its external result is known or canonical state
was committed. A returned unknown send stays unknown with the same client ID.

`owned_threads_stopped` requires actual started, distinct Thread handles, observed
entry/return of both owned callbacks, a requested stop and no live owned thread.
`local_stop_closed` additionally requires no active/unreturned Channel loop
callbacks and no still-running Channel loop. Missing/unstarted handles, partial
startup, missing callback instrumentation or a deadline do not produce closure.
`loop_failures_observed` is a bounded lane name list, not exception text.

Every report keeps `external_operation_quiescence_verified`,
`canonical_channel_state_closed_verified` and `handoff_authorized` false. The
object is a process-local owner capability, not a serializable proof; its JSON,
PID, socket status or copy in another process cannot authorize takeover. Calls
made directly outside these owned loops are not certified by this observation.
Private account/context/outbox contents are not included in the report. Duplicate
group/start and a pre-existing running loop are rejected; a partial start retains
its handles and cannot be silently retried as a fresh group.

## Next controlled runtime step

First integrate this frozen source into the **existing sole receiver process**
at a backed-up, owner-approved deployment boundary and retain its live `channel`
and `loop_lifecycle` handles. Do not infer handles for the old uninstrumented
process or install a second receiver to observe it. Keep the original authority,
account/context/cursor/message/outbox identities and existing Leader services.

Then request carrier-only stop through that owner-held group, observe actual
callback/Thread return on the same handles, and independently reconcile the
original communication state, unknown outbox and in-flight identities. A caller
timeout stays unclosed. Only after real external/canonical closure is established
may a separately trusted owner verifier authorize one controlled fenced carrier
bootstrap/handoff. The existing `channel_handoff.collect_readiness` currently
accepts an already-fenced `ChannelSupervisor`, not this legacy bootstrap group;
legacy canonical-state/independent-verifier integration is a separate next slice.
Neither this source nor moving a carrier proves VPS-partition HA: communication
authority/state/arbitration still must leave the VPS-only fault domain.

Focused tests use real local Thread objects and held **synthetic** transport/
commit callbacks. They cover actual stop return vs timeout, full callback scope,
unknown identity preservation, duplicate/partial start, stop-before-admission,
invalid deadlines and self-stop, and keep unrelated synthetic Leader tasks
unchanged. They are not live iLink or production handoff evidence.
