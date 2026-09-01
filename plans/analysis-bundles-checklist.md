# Implementation checklist - analysis bundles

- [x] 1. Reconfirm baseline tests and real Retail event layouts without modifying source logs.
- [x] 2. Add `OutputOptions`, CLI validation, streaming deterministic gzip, and size reporting.
- [x] 3. Add output-wide locking and migrate `StateStore` to compatible per-profile v2 state.
- [x] 4. Extend identity, staging, publication, crash recovery, and stale-output convergence.
- [x] 5. Add tolerant event-family parsing with raw fallbacks and real-format fixtures.
- [x] 6. Implement bounded actor relevance, deferred causal filtering, caps, and warnings.
- [x] 7. Add objective incremental run/player aggregates, interrupts, dispels, casts, and absorbs.
- [x] 8. Add streamed death windows, adaptive aura context, and incomplete-coverage signaling.
- [x] 9. Publish analysis JSON, combat, deterministic gzip, optional ZIP, and non-circular sizes.
- [x] 10. Add the requested analysis, state, publication, cardinality, and watch tests.
- [x] 11. Run the complete mechanical verification suite and fix all regressions.
- [x] 12. Validate read-only against the real WoW log and record measured reduction/evidence.
- [x] 13. Perform the causal-completeness audit and make filtering more conservative if needed.
- [x] 14. Update documentation, complete the review funnel, and prepare the branch for commit.
