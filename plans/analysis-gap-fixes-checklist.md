# Implementation checklist - analysis gap fixes

- [x] 1. Baseline frozen: 87 tests green on main; real-log reference run kept in scratch (0.45 %).
- [x] 2. Parser: SWING_DAMAGE_LANDED as damage, source_owner_guid, COMBATANT_INFO item level, event-specific spell names in as_dict.
- [x] 3. OutputOptions/CLI: keep_player_damage option, profile suffix, validation, --help.
- [x] 3b. AnalysisSession: _keep_policy table, count/write split, resource + LANDED handling, pet owner from source block, class_id/item_level, named enemy_cast_successes, players wrapper.
- [x] 4. Regression tests (each verified failing without its change).
- [x] 5. Existing assertions updated; full suite, py_compile, --help green.
- [x] 6. Real-log re-validation: reduction, deaths, preserved counts, sampled deaths, idempotent second run, source untouched.
- [x] 7. READMEs and this checklist updated; causal-completeness review done.
