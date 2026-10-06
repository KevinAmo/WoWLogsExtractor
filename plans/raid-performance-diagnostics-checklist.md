# Raid performance diagnostics — checklist

Derivada de `plans/raid-performance-diagnostics.md`. Rama `feature/raid-performance-diagnostics`.

- [x] 1. Base: rama creada, suite previa verde (113 tests), `py_compile` OK.
- [x] 2. Primitivas de parseo (`parse_log_header`, `_power_state`, `_damage_suffix`,
      `_energize_suffix`, `_aura_stacks`, `parse_combatant_details`) + tests con líneas reales.
- [x] 3. Opciones y contexto: `OutputOptions.performance_player`, huella `+perf-<h>`, CLI, cabecera
      de juego en tracker / `FileProcessor` / `StateStore` + tests.
- [x] 4. `PerformanceAccumulator` general: fase, identidad, daño, casts, muertes, auras, recursos,
      cronología, topes + tests unitarios.
- [x] 5. Reglas Arcane: opener, ventanas de burst, procs, cargas inferidas con autocomprobación,
      continuidad, limitaciones + tests.
- [x] 6. Publicación: `performance.json` en payload, marker y ZIP; retirada del obsoleto; matriz de
      fallos de `CLAUDE.md` verde antes de integrar el packet.
- [x] 7. Packet: recolección, sesiones por GUID, duplicados, agregados, observaciones, presupuesto,
      reconstrucción y limpieza; disparadores en `_run_once`, `_watch`, `prepare` + tests.
- [x] 8. End-to-end sintético, ejemplo versionado y regresión byte a byte de los modos existentes.
- [x] 9. Validación sobre el log real (copia en scratch): contraste con el sondeo, tiempo, memoria
      pico y tamaños. Contraste PASS en 14 cifras; 86,4 s y 60,7 MB con el flag frente a 83,3 s y
      61,4 MB sin él; packet ≈ 99 KB; segunda ejecución sin cambios; SHA-1 del log original intacto.
- [x] 10. Documentación: ambos README, `CLAUDE.md`, esta checklist.
- [x] 11. Embudo de revisión: mecánica (Sonnet, 8 hallazgos corregidos), empírica y cross-family
      (Codex GPT-6 Astra High, 6 hallazgos corregidos y confirmados; regresión P2 del Ctrl+C
      corregida y revisada aparte). Suite final: 257 tests.

Pendiente, fuera de esta feature: `players.json` sigue promediando el tabardo en `item_level`;
sesiones que cruzan medianoche, varios ficheros y duplicados están probados solo con datos
sintéticos.
