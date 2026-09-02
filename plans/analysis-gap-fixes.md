# Analysis bundle gap fixes

## Goal + out-of-scope

Cerrar las diferencias observadas entre `plans/analysis-bundles.md` (ya implementado y mergeado en
`ba9e2f9`) y el comportamiento real medido sobre un log Retail. La validación del 2026-09-02 sobre
una copia read-only de `WoWCombatLog-083026_102324.txt` (M+ Valle Cegador +10, 87.0 MB, 273 047
líneas) dio:

| Comprobación | Resultado |
| --- | --- |
| Player `UNIT_DIED` en full vs entradas en `deaths.json` | 18 / 18 |
| `SPELL_INTERRUPT`, `SPELL_DISPEL`, `COMBATANT_INFO` full vs `combat.txt` | 52/52, 41/41, 30/30 |
| Log fuente intacto (size + mtime) | sí |
| **Reducción `combat.txt` vs full** | **0.45 %** (86.6 MB de 87.0 MB) |
| Eventos por ventana de muerte | 200–480 (dominados por `SPELL_ENERGIZE` y dosis de buffs) |
| `players[].item_level` | siempre `null` aunque `COMBATANT_INFO` trae el ilvl por slot |
| `players[].class_id` | ausente |
| `enemy_cast_successes` | `{"1238066": 52}`: sin nombre de hechizo, ilegible para una IA |
| Pets invocados antes del inicio del segmento | no se enlazan al owner (solo se usa el bloque avanzado cuando describe al destino) |

Las tareas 12/13 del checklist previo se marcaron hechas, pero la reducción real es ~0 %, así que el
filtro no cumple el objetivo de tamaño. Este plan corrige la política de relevancia, completa los
campos de `players.json`, mejora la legibilidad de `summary.json`/`deaths.json` y enlaza pets por el
bloque avanzado del origen. Se mantiene todo lo demás: modo default byte-idéntico, `--analysis`,
`--analysis-only`, `--gzip`, `--bundle`, perfiles de estado, lock, staging, watch, caps y warnings.

Fuera de alcance: cualquier tabla externa de hechizos (defensivos, procs), clasificación de culpa o
evitabilidad, reescritura de líneas, nuevas dependencias, cambios en el formato legacy `.txt/.json`.
El único flag nuevo es el opt-out `--keep-player-damage` descrito abajo; no se añade ningún otro.
La reducción esperada es ~45–50 % en M+ reales, no el 85 % del ejemplo del enunciado: el resto de
bytes son heals/auras entre jugadores y daño recibido, que el enunciado obliga a conservar. Se
documenta el número medido, no uno prometido.

### Decisión de política (desviación explícita respecto al texto literal del enunciado)

El enunciado pide a la vez "conserva jugador -> NPC" (y el test requerido nº 2, "Damage player ->
NPC se conserva") y "elimina procs repetitivos puramente ofensivos" con un objetivo de reducción
grande. Ninguna de las 13 preguntas de análisis listadas (por qué murió, healing disponible, daño
evitable, interrupts, dispels, defensivos, mecánicas, tiempo bajo de HP, CDs usados, adds
involucrados, boss) necesita las líneas de *resultado* del daño saliente de jugadores/pets sobre
NPCs. Sí necesitan los casts del jugador, sus auras sobre NPCs (CC), sus misses sobre NPCs
(`IMMUNE` señala fases) y todo lo que tenga a un jugador o pet propio como destino.

Por defecto el filtro es compacto (descarta las líneas de resultado de daño saliente pero las sigue
agregando). El flag `--keep-player-damage` restaura la lectura literal (las conserva en
`combat.txt`) y forma parte del perfil de estado, de modo que cambiar el flag hace backfill una vez
y repetirlo no duplica bundles. Así el test nº 2 se cubre en sus dos lecturas: por defecto el daño
saliente aparece en `damage_done`/`event_counts`; con el flag aparece además la línea raw.

#### Tabla normativa `(count, write)` de `_keep_policy`

`count` = incrementa `event_counts` y alimenta agregados de `players.json`/detalles; `write` =
la línea raw va a `combat.txt`. La decisión se toma una sola vez por record; el look-back de
`_mark_hostile` aplica exactamente la misma función y los flags `aggregated`/`selected` del record
la hacen idempotente (nunca se cuenta dos veces ni se escribe una línea con `write=False`).

| Familia | Condición | count | write |
| --- | --- | :-: | :-: |
| Estructurales (`STRUCTURAL_EVENTS`, incl. `COMBATANT_INFO`) | siempre | sí | sí |
| Parse fallback / línea no parseable | siempre | sí | sí |
| `UNIT_DIED`, `UNIT_DESTROYED`, `PARTY_KILL` | siempre | sí | sí |
| `RESOURCE_EVENTS` (`SPELL_ENERGIZE`, `SPELL_PERIODIC_ENERGIZE`, `SPELL_DRAIN`, `SPELL_LEECH`) | siempre | no | no |
| `SWING_DAMAGE_LANDED` | destino jugador o pet propio | no (`event_counts` cuenta solo `SWING_DAMAGE`; nunca agrega) | sí |
| `SWING_DAMAGE_LANDED` | destino NPC | no | solo con `--keep-player-damage` y origen jugador/pet propio |
| Daño saliente (`SWING_DAMAGE`, `SPELL_DAMAGE`, `SPELL_PERIODIC_DAMAGE`, `RANGE_DAMAGE`, `DAMAGE_SHIELD`, `DAMAGE_SPLIT`) | origen jugador o pet con owner conocido, destino NPC no propio | sí (`damage_done` del owner) | solo con `--keep-player-damage` |
| `SPELL_ABSORBED` saliente | igual que arriba | sí (solo `event_counts`; **no** suma a `damage_done`, el escudo del NPC no es daño hecho) | solo con `--keep-player-damage` |
| Daño / heal / `SPELL_ABSORBED` / `SPELL_HEAL_ABSORBED` / misses / auras | destino jugador o pet propio | sí | sí |
| Casts (`CAST_EVENTS`), `SPELL_INTERRUPT`, dispels, summons | origen jugador, pet propio u hostil relevante | sí | sí |
| Misses y auras salientes (jugador/pet -> NPC) | siempre | sí | sí |
| Heal pet irrelevante -> pet irrelevante | sin jugador directo | no | no |
| NPC desconocido -> NPC desconocido | ninguno relevante | no | no (puede promoverse después por look-back, con la misma tabla) |

Los `RESOURCE_EVENTS` no aportan evidencia de interacción: no marcan hostiles ni entran en la
ventana de muerte. En la ventana de muerte (`death_relevant`) además se excluyen los
`RESOURCE_EVENTS`, y `SWING_DAMAGE_LANDED` se serializa como registro suplementario de estado:
`as_dict` omite `amount`/`absorbed` y añade `"supplemental_state": true`, conservando
`target_hp`/`target_max_hp`/posición y la línea raw. Así cada golpe de melee aporta un único
`amount` (el de `SWING_DAMAGE`) y el HP de la víctima (el de `LANDED`).

Decisión del owner del plan (2026-09-02): tras la ronda 2 de Codex GPT-5.6 Sol High se fijan el
predicado de validez de owner (rechazo de flags hostiles) y la política determinista de `item_level`
(ignorar tuplas inválidas, media de las válidas). Revisión cerrada en dos rondas.

Decisión del owner del plan (2026-09-02, tras la revisión de diff de Codex): (a) antes de reemplazar el
primer fichero del payload de `analysis/`, se elimina el `metadata.json` anterior y el cuerpo `combat`
del otro contenedor, de modo que un crash a mitad de una republicación nunca deja un marker antiguo
sobre artefactos mezclados; (b) como todos los perfiles comparten `analysis/` y `.txt`, el commit de
estado de un perfil descarta las entradas de los demás perfiles de ese fichero: cambiar de flags
siempre hace backfill una vez (idempotente sobre los mismos basenames) y repetir las mismas flags
sigue sin publicar nada; (c) `pet_only_heal` exige que el destino no sea jugador ni pet propio;
(d) las líneas no parseables pasan por la misma política `(sí, sí)` y cuentan como `UNPARSEABLE`.

## Files/interfaces touched

- `WoWLogExtractor/WoWLogExtractor.py`
  - Constantes: `ANALYSIS_SCHEMA_VERSION = 2`; `RESOURCE_EVENTS`; `DAMAGE_RESULT_EVENTS =
    DAMAGE_EVENTS | {"SWING_DAMAGE_LANDED"}`; `SPEC_CLASSES` junto a `SPEC_ROLES`.
  - `OutputOptions`: campo `keep_player_damage`; entra en `profile` como sufijo
    `+keep-player-damage` y en `as_dict`. `build_parser`/`run` lo exponen como
    `--keep-player-damage` (válido solo con un modo analysis; error claro si no).
  - `parse_combat_event`: `SWING_DAMAGE_LANDED` se parsea como daño (amount + bloque avanzado, con
    `expected_school = 1` igual que `SWING_DAMAGE`). Nuevo campo `source_owner_guid` cuando el
    bloque avanzado describe al origen. `COMBATANT_INFO` rellena `spec_id` e `item_level` (campos
    nuevos de `ParsedCombatEvent`), nunca `spell_id`/`amount`. `item_level` = media redondeada de
    los `ilvl > 0` del array de equipo `[(itemID,ilvl,(..),(..),(..)),...]` localizado como el
    primer argumento que empieza por `[(` tras el array de talentos. Política determinista de
    tolerancia: cada tupla se evalúa por separado; las tuplas malformadas y las de `ilvl <= 0`
    se ignoran, y `item_level` es la media redondeada (half-up) de las restantes; si no queda ninguna
    válida (array vacío, ausente o totalmente malformado) -> `None`. Nunca afecta a la
    retención de la línea.
  - `ParsedCombatEvent.as_dict`: claves específicas por evento en lugar de `extra_spell_*`:
    `interrupted_spell_id/interrupted_spell` (`SPELL_INTERRUPT`), `dispelled_spell_id/
    dispelled_spell` (`SPELL_DISPEL`, `SPELL_DISPEL_FAILED`, `SPELL_STOLEN`), `shield_spell_id/
    shield_spell` (`SPELL_ABSORBED`, `SPELL_HEAL_ABSORBED`); `spec_id`/`item_level` para
    `COMBATANT_INFO`; `supplemental_state` para `SWING_DAMAGE_LANDED`. `extra_spell_*` desaparece.
  - `AnalysisSession`: `_keep_policy(parsed) -> (count, write)` según la tabla;
    `_count_record` (event_counts + `_aggregate`) separado de `_select_record` (write);
    `_mark_hostile` usa `_keep_policy` en el look-back; `death_relevant` excluye
    `RESOURCE_EVENTS`; `_remember_pet` también desde `source_owner_guid` con el predicado de validez de owner:
    el owner es un GUID `Player-`, el origen tiene flags pet/guardian **y no** tiene el bit
    `REACTION_HOSTILE`; el mismo predicado se aplica al camino existente por `target_owner_guid`
    (flags del destino). Un pet hostil (incluido el de un jugador enemigo) nunca entra en
    `pet_owners`: sigue tratándose como hostil relevante; `_new_player` añade `class_id` e
    `item_level`; `COMBATANT_INFO` rellena `spec_id`, `role`, `class_id`, `item_level`;
    `enemy_cast_successes` se emite como lista `[{"spell_id", "spell_name", "count"}]` ordenada por
    `count` desc, luego `spell_id` asc (ids nulos al final), luego nombre; `summary_and_players`
    devuelve `players` como `{"players": [...]}` y `_analysis_payload` lo escribe así.
  - `_aggregate`: `SWING_DAMAGE_LANDED` nunca suma; `SPELL_ABSORBED` sigue sumando solo
    `absorbs_received` del destino jugador.
- `WoWLogExtractor/tests/test_extractor.py`: tests nuevos (abajo) y actualización de las
  aserciones sobre `enemy_cast_successes`, `players.json`, `extra_spell_*`, `Bite`/`Sinister
  Strike` en `combat.txt`, y `--help`.
- `WoWLogExtractor/README.md` y `README.md`: sección "Qué se descarta de combat.txt" con la tabla
  resumida, `--keep-player-damage`, número medido de reducción, formas v2 exactas de los JSON
  (`players.json` con wrapper, `enemy_cast_successes` como lista, claves `interrupted_spell*`,
  `dispelled_spell*`, `shield_spell*`, `supplemental_state`, `spec_id`/`item_level`/`class_id`),
  significado aproximado de `item_level`, y que `extra_spell_*` de v1 ya no existe.
- `plans/analysis-gap-fixes.md` (este archivo) y `plans/analysis-gap-fixes-checklist.md`.

## Ordered task breakdown

1. Congelar baseline: 87 tests verdes en `main`; guardar el output real de referencia en scratch
   (ya hecho: 0.45 %, 18/18 muertes).
2. Parser: `SWING_DAMAGE_LANDED` como daño; `source_owner_guid`; `spec_id`/`item_level` en
   `COMBATANT_INFO` tolerante; renombrado por evento y `supplemental_state` en `as_dict`. Tests
   unitarios con líneas reales (pet cast success con owner en bloque de origen; LANDED N->P con HP de
   víctima; COMBATANT_INFO real con ilvl ~310) y con payloads malformados (array vacío, ilvl 0,
   tuplas mixtas, array malformado, línea con prefijo de longitud distinta): valor exacto esperado
   según la política determinista (media de las tuplas válidas positivas; `None` solo si no hay
   ninguna), `amount` nunca, línea siempre retenida.
3. `OutputOptions`/CLI: `keep_player_damage`, perfil, validación, `--help`.
4. `AnalysisSession`: `_keep_policy` + separación count/write + look-back idéntico e idempotente;
   exclusiones en ventana de muerte; pets por origen; `class_id`/`item_level`;
   `enemy_cast_successes` con nombres; wrapper de players.
5. Tests de regresión (cada uno debe fallar sin su cambio):
   - test parametrizado sobre la tabla `(count, write)`: para cada fila un fixture y aserción de
     presencia en `combat.txt`, `event_counts` y agregados exactos (`damage_done` con total
     exacto; `SPELL_ABSORBED` saliente no suma);
   - energize ausente de `combat.txt`, de `event_counts` y de la ventana de muerte;
   - par `SWING_DAMAGE`/`SWING_DAMAGE_LANDED` sobre un jugador: ambas líneas en `combat.txt`, un
     solo `amount` en la ventana de muerte, `target_hp` presente en el registro `LANDED`,
     `damage_taken` sumado una vez;
   - LANDED P->N ausente por defecto y presente con `--keep-player-damage`; daño P->N y pet->N
     ausentes por defecto, presentes con el flag, y `damage_done` idéntico en ambos modos;
   - pet enlazado por bloque de origen antes de cualquier `SPELL_SUMMON`; owner `nil`/cero y owner
     no jugador ignorados; pet con flags hostiles cuyo owner es un `Player-` enemigo queda hostil,
     no entra en `pet_owners` y su daño no se atribuye a ningún jugador; owner por
     encima de `MAX_PLAYER_AGGREGATES` no rompe ni atribuye;
   - `COMBATANT_INFO` dentro de una ventana de muerte serializa `spec_id`/`item_level` y no
     `spell_id`/`amount`;
   - forma exacta v2: `players.json == {"players": [...]}`, cada entrada de
     `enemy_cast_successes` con exactamente `spell_id`, `spell_name`, `count` y orden determinista,
     claves `interrupted_spell*`/`dispelled_spell*` presentes y `extra_spell_*` ausente en summary y
     en eventos de death; misma forma en el ZIP;
   - misses P->N y auras P->N conservados; look-back de hostil no resucita líneas con
     `write=False` ni cuenta dos veces;
   - perfil `+keep-player-damage` separado en `state.json` (backfill una vez, sin duplicados).
6. Actualizar aserciones existentes; suite completa + `py_compile` + `--help`.
7. Re-validar sobre la copia del log real con `--analysis --bundle --reset-state`: reducción medida,
   18/18 muertes, 52/41/30 conservados, criterio de completitud de daño letal (abajo), eventos por
   muerte, segunda ejecución idéntica -> `(0, 0, 0)`, fuente intacta. Repetir con
   `--keep-player-damage` y comprobar que `damage_done` coincide.
8. README (ambos) + checklist; revisión crítica de causalidad con las muertes muestreadas.

## Verification criteria

- `python -m unittest discover -s WoWLogExtractor/tests` verde (87 legacy + nuevos). Cada test de
  regresión nuevo se comprueba fallando contra `main` (o con su cambio revertido).
- `python -m py_compile WoWLogExtractor/WoWLogExtractor.py` y `--help` OK; `--keep-player-damage`
  sin modo analysis falla con mensaje claro.
- Default: fixture produce `.txt` byte-idéntico y mismo `.json` que antes (tests legacy intactos).
- Real log (copia): `reduction_percent` >= 40 % por defecto; `deaths.json` = nº de `UNIT_DIED` de
  players en full; `SPELL_INTERRUPT`, `SPELL_DISPEL`, `COMBATANT_INFO`, `ENCOUNTER_START/END`
  idénticos en full y `combat.txt`; toda línea del full de `DAMAGE_RESULT_EVENTS`, `HEAL_EVENTS`,
  auras, `SPELL_ABSORBED`, misses o casts con destino jugador está en `combat.txt` (comprobado por
  script, no por muestra); energize = 0 en `combat.txt`; eventos por muerte claramente por debajo
  del baseline; `players[].item_level` en 280–315 y `class_id` no nulo para todo spec presente
  en `SPEC_CLASSES` (el spec 1480 del log real no está en la tabla y queda `null` a propósito: no
  se adivina la clase);
  `enemy_cast_successes[0].spell_name` no nulo; segunda ejecución con mismas flags no publica
  nada; size+mtime del log fuente sin cambios.
- Completitud de daño letal (script sobre >= 3 muertes): para cada muerte muestreada, el último
  evento de daño con `amount` contra el jugador anterior a `UNIT_DIED` en el full aparece en
  `deaths.json` con el mismo `amount`; ningún evento de daño con destino el jugador dentro de la
  ventana está ausente; los pares `SWING_DAMAGE`/`LANDED` aportan un único `amount`; aparecen heals
  recibidos y auras aplicadas/eliminadas; `hostiles` no está vacío.
- README lista qué se descarta, documenta `--keep-player-damage`, las formas v2 exactas, dice que el
  full es el fallback, y publica el porcentaje medido.
