# Raid performance diagnostics

Análisis compacto de rendimiento de raid por jugador: `performance.json` por pull y un
`diagnostic_packet.json` por sesión y jugador, pensado para compartirse con un LLM.

## Discovery

Base: `main` @ `86b0ec1`, 2026-10-04. Suite previa: **113 tests OK** (`python -m unittest discover
-s WoWLogExtractor/tests`), `py_compile` OK. Sin lint/typecheck configurado (no hay `pyproject.toml`,
`setup.cfg`, `.flake8`, `mypy.ini` ni `ruff.toml`). Sin seguimiento y ajenos a la feature:
`temp.md`, `wow-icon.webp` (no se tocan ni se incluyen en commits).

Todas las referencias son a `WoWLogExtractor/WoWLogExtractor.py` (W) y
`WoWLogExtractor/tests/test_extractor.py` (T). Las líneas de publicación, estado, tracker, parser y
`consume` se leyeron de primera mano; el resto procede de trazas delegadas.

### Código actual

- **Lectura y segmentos.** `FileProcessor.process_new_data` (W:2127) lee en binario por bloques de
  1 MiB y pasa cada línea completa a `SegmentTracker.feed` (W:1904). `feed` llama a `parse_line`
  para toda línea. El pre-contexto es un `deque` de 10 s / 5000 líneas (W:2004) que
  `_start_segment` (W:2017) reproduce dentro del segmento; el post-contexto son 10 s tras
  `end_ts` (W:1927). `Segment.write` (W:1452) es el único punto por el que pasa cada línea de un
  segmento, exactamente una vez (el `segment` se asigna tras el replay, W:2030).
- **Fase.** `AnalysisSession` no distingue pre/encuentro/post: solo tiene `current_encounter`
  (W:1232, W:1291). Sus agregados (`_aggregate`, W:1025+) suman las tres fases, `damage_done` usa
  `amount` bruto (sin restar overkill) y contaría autodaño.
- **Parser.** `parse_combat_event` (W:640) extrae `amount` y `absorbed` de daño, `aura_type`, y del
  bloque avanzado solo HP/posición del destino y el owner (W:593-606, W:674-693). **No** extrae:
  overkill, crítico, dosis de auras, sufijo de `SPELL_ENERGIZE`, campos de poder (índices 10-13 del
  bloque), tipo de fallo de cast, talentos ni equipo por pieza. `RESOURCE_EVENTS` se descartan en
  `_keep_policy` (W:978), es decir, después de llegar a `consume`.
- **Cabecera.** `COMBAT_LOG_VERSION` solo cierra el segmento y vacía el buffer (W:1922, W:1998);
  versión/build no se guardan en ningún sitio. Al reanudar, el warm-up rebobina 512 KiB (W:2193),
  así que la cabecera suele quedar fuera.
- **Identidad.** `segment_id = kind|<basename del log>|<start_ts ms>|<encounter_id>` (W:2013).
  `Segment.start_offset` existe solo en memoria (W:2026). No hay concepto de sesión ni número de
  intento.
- **Perfil y estado.** `OutputOptions.profile` (W:356) es la única clave de invalidación:
  `StateStore.get_offset` (W:2307) devuelve 0 si no hay entrada para el perfil actual. `claim`
  (W:2324) y `update` (W:2350) dejan como mucho un perfil por log. `ANALYSIS_SCHEMA_VERSION`
  (W:46) se escribe pero nunca se comprueba al leer.
- **Publicación.** `SegmentPublisher.publish` (W:1783): JSON raíz y cuerpo full; después se retira
  `metadata.json` y el `combat` del otro contenedor (W:1835), se copian `summary`, `combat`,
  `deaths`, `players` con `_copy_atomic`, el ZIP, y `analysis/metadata.json` al final como marker
  (W:1854). `_purge_stale` (W:1666) recorre todo directorio no `.staging` de `Raids/`;
  `cleanup_partials` (W:1559) borra árboles con `analysis/` vacío. Los restos de otro perfil bajo
  el mismo nombre no se purgan (salvo marker y `combat` opuesto).
- **Bucle.** `_run_once` (W:2616) procesa cada log y hace commit de estado tras publicar; `_watch`
  (W:2651) hace lo mismo por poll. `prepare` (W:2576) limpia staging bajo el lock.
- **Tests.** Helpers `LogBuilder` (T:52), `ExtractorTestCase` (T:79), `line_bytes` (T:46) y los de
  `AnalysisBundleTests` (T:1389+: `options`, `_session`, `_feed_real`, `_published_marker`).
  Inyección de fallos: `mock.patch.object(wle, "_copy_atomic", side_effect=...)` (T:2625-2694) y
  parche de `_atomic_write_bytes` (T:2050).

### Log real (solo lectura): `WoWCombatLog-100126_212857.txt`

635 MB, 2 098 692 líneas, 21:28–23:25 del 2026-10-01 (no cruza medianoche), cabecera única
`COMBAT_LOG_VERSION,22,ADVANCED_LOG_ENABLED,1,BUILD_VERSION,12.1.0,PROJECT_ID,1`, cliente esES.
14 pulls Heroico (dificultad 15): 7 de `3421 "Los colmillos gemelos"` (6 wipes + 1 kill) y 7 de
`3429 "El Altar en Espiral"` (wipes; uno de 18 s). Jugador: `Dkyam-DunModr-EU`,
`Player-1378-0B46B91A`, spec 62.

Hechos verificados (conteos sobre el log; lo inferido se marca):

1. `ENCOUNTER_START,id,"nombre",dificultad,tamaño,instancia`; `ENCOUNTER_END,...,success,fightMs`.
   `COMBATANT_INFO` va en las líneas inmediatamente posteriores a `ENCOUNTER_START`.
2. `COMBATANT_INFO`: índice 24 spec, 25 talentos `[(nodo,entrada,rango),...]`, 27 equipo
   `[(itemId,ilvl,(enchants),(bonus),(gemas)),...]`, 28 auras previas `[caster,spell,stacks,...]`.
   14/14 pulls con el mismo hash de talentos y de equipo. La lista de auras es **parcial**
   (faltaban auras que luego se eliminan sin haberse aplicado).
3. Bloque avanzado de 19 campos (índices 11-29 en `SPELL_*`): 0 GUID, 1 owner, 2-3 HP, 10
   powerType, 11 poder actual, 12 poder máx., 13 coste, 14-15 posición. Describe al **destino** en
   daño/heal (18 440/18 440) y al **lanzador** en `SPELL_CAST_SUCCESS` (2 599) y `SPELL_ENERGIZE`.
4. Sufijo de daño: `amount, base, overkill, school, resisted, blocked, absorbed, critical,
   glancing, crushing, ST|AOE`. `overkill` es `-1` si no hay; `amount` **incluye** el overkill
   (`116194/…/87283` con HP 0). `critical` es `1` o `nil`. `amount` es el daño posterior al escudo
   y `absorbed` va aparte (44 028 líneas con `absorbed>0` y `amount≠absorbed`, p. ej. `76467` y
   `1248`).
5. Un golpe totalmente absorbido **no** genera `SPELL_DAMAGE`: aparece como `SPELL_ABSORBED`
   (+`SPELL_MISSED`): 0 de 137 con línea de daño. `SPELL_ABSORBED` con hechizo tiene 21 campos
   (18 = cantidad absorbida, 8-10 hechizo atacante); la forma de melee, 18.
6. Autodaño: `1309786 Refracción`, origen = destino = jugador, 15,3 M. No es daño hecho.
7. `*_SUPPORT`: 0 en todo el log. `SPELL_CAST_FAILED` del jugador: 0 (3 806 de otros).
8. Dosis: último campo de `SPELL_AURA_APPLIED_DOSE` = stacks nuevos; de `SPELL_AURA_REMOVED_DOSE`
   = stacks restantes.
9. Recursos: en los 34 202 bloques del jugador `powerType` es 0 (maná), sin valores separados por
   `|`. Las Cargas Arcanas (tipo 16) **nunca** están en el bloque: solo como ganancias en
   `SPELL_ENERGIZE` (sufijo `amount, overEnergize, powerType, maxPower`; 4 452 líneas, máx. 4). No
   existe evento de gasto. Muestras de estado propio: 430-620/min, hueco máximo 2,46 s.
10. Muertes: 13 `UNIT_DIED` del jugador; la del pull 12 cae 0,33 s **después** de
    `ENCOUNTER_END`. 2 `SPELL_RESURRECT`, ambos entre pulls. Daño propio tras morir: 2 líneas en
    un pull (proyectiles en vuelo).
11. Mascotas: `Fénix Arcano` enlaza por GUID exacto `SPELL_SUMMON` destino = origen del daño, con
    spell IDs propios. `Reflejo exacto`: el GUID del daño difiere del invocado y sus líneas de daño
    no llevan owner; solo sus casts lo llevan en el bloque de origen.
12. Boss: los nombres de unidad (`Ithraz`, `Vexhul`, `Zul'jan`) no coinciden con el nombre del
    encuentro; no hay evento nativo que identifique al boss. Hay objetivos `Vehicle-` y jugadores
    hostiles (flags `0x548`).
13. IDs Arcane observados (no supuestos): `365350` Oleada Arcana (cast con `CAST_START`, buff
    `365362`), `321507` Toque de los magi (debuff `210824` en el objetivo, daño `210833`), `5143`
    Misiles Arcanos (un `CAST_SUCCESS`, ticks `7268` cada ~0,15 s, sin `CAST_START`), `44425`
    Tromba Arcana, `153626` Orbe Arcano (daño `153640`), `1295924` Descarga prismática (daño
    `1295924`/`1295939`, buff `1295942`), `30451` Explosión Arcana (1 cast), `449569` Meteorito
    (daño sin cast), `263725` Lanzamiento libre (stacks), `451038` Alma Arcana, `1223797`
    Intuición. No aparecen: Evocación, Presteza mental, Poder cambiante, Nether Precision.
14. Rendimiento de lectura: filtro por subcadena sobre los 635 MB en 1,6 s; el splitter por
    caracteres procesa ~53 000 líneas/s.

### Preguntas abiertas resueltas por decisión

- **Boss vs adds**: sin evidencia nativa. Se clasifica `boss` solo si el nombre de la unidad es
  idéntico al del encuentro; en otro caso `unknown` con evidencia adjunta (HP máx., bits de
  marcador, presencia). No se añade tabla de NPCs por encuentro.
- **Cargas Arcanas**: solo como inferencia medible (ver Reglas Arcane).

## Goal + out-of-scope

Procesar localmente una noche de raid completa y obtener, para un jugador seleccionable, (a) un
`performance.json` por pull con métricas calculadas estrictamente dentro de los límites del
encuentro y (b) un `diagnostic_packet.json` por sesión que agregue los pulls por boss y contenga
evidencia suficiente para razonar sobre patrones de ejecución sin acceso al disco. Primer caso de
uso: Dkyam (Arcane Mage); el motor no contiene su nombre.

Principios: separar observación de inferencia; toda tasa lleva numerador, denominador y tamaño de
muestra; lo que no se puede derivar se declara como limitación concreta, no se inventa.

### Fuera de alcance

- Comparación con rendimiento teórico, simulaciones, otros jugadores o Warcraft Logs.
- Disponibilidad teórica de cooldowns, usos «perdidos» y consumo «incorrecto» de procs (requieren
  modelar talentos, cargas y resets). Se devuelven como limitaciones nombradas.
- Segmentos Mythic+ (no generan `performance.json` en v1) y Classic.
- Varios jugadores por ejecución; representación Markdown del packet; tabla de NPCs boss por
  encuentro; atribución de mascotas sin evidencia de owner.
- Cambios en `summary.json`, `players.json`, `deaths.json`, `combat.txt`, el `.json` legacy o en
  `ANALYSIS_SCHEMA_VERSION`. `_aggregate` no se modifica.
- Servicios, red, bases de datos, dependencias externas, llamadas a un LLM, refactorización
  general. Push y PR.

### Decisiones y supuestos

1. **CLI.** `--performance-player SELECTOR` (requiere un modo analysis; error claro si no).
   `SELECTOR` es un GUID `Player-…`, un nombre completo `Nombre-Reino-Región` o un nombre corto.
   `--packet-max-bytes N` (por defecto 200000) y `--session-gap-minutes N` (por defecto 120) solo
   afectan al packet.
2. **Perfil.** Con el flag, el perfil añade `+perf-<h>` con `h = sha1(selector normalizado |
   PERFORMANCE_SCHEMA_VERSION | PERFORMANCE_RULES_VERSION | versión de cada perfil de spec)[:12]`.
   Cambiar jugador, esquema o reglas es otro perfil: `get_offset` devuelve 0 y el log se reprocesa
   una vez. Sin el flag, la cadena de perfil y `as_dict` son idénticas a las actuales. Presupuesto
   y hueco de sesión **no** entran en el perfil (cambiarlos no reprocesa 635 MB: solo reconstruye
   el packet).
3. **Ubicación.** `performance.json` vive en `<basename>/analysis/` y participa del marker
   existente. El packet vive en un tercer directorio, `<output>/Diagnostics/
   <YYYY-MM-DD_HH-MM-SS>_<nombre>_<guid8>_diagnostic_packet.json`, fuera del alcance de
   `_purge_stale` y `cleanup_partials`.
4. **Un fichero = una unidad atómica.** El packet es un único fichero escrito con
   `_atomic_write_bytes`; no existe estado «parcialmente publicado».
5. **Disco = función de los resultados publicados.** El packet se reconstruye en cada ejecución con
   el flag a partir de los markers válidos, no de deltas ni de offsets. Los packets propios que ya
   no se derivan de los resultados actuales (otro jugador, otra versión, sesión que cambió de
   inicio) se eliminan; solo se borran ficheros de `Diagnostics/` con el sufijo propio y que se
   parsean como packet. Una ejecución sin el flag no toca `Diagnostics/`. Orden de la
   reconstrucción: primero se escriben todos los packets nuevos o cambiados; solo si todas las
   escrituras tienen éxito se eliminan los obsoletos. Un fallo conserva los packets anteriores,
   no borra nada y deja la reconstrucción pendiente.
6. **Sesión.** Las sesiones se construyen **por GUID resuelto**: cadena máxima de pulls de raid de
   ese GUID, ordenados por inicio, en la que el hueco entre el fin observado de un pull y el inicio
   del siguiente no supera `--session-gap-minutes`. No depende de la fecha (una noche que cruza
   medianoche es una sesión) ni del fichero (varios logs se unen si el hueco lo permite). Un cambio
   de build o de configuración del personaje no parte la sesión: parte los grupos de comparación.
   - Un selector corto que resuelve a GUID distintos en pulls distintos produce packets separados,
     nunca estadísticas combinadas. El nombre del fichero incluye nombre y los 8 primeros
     caracteres de `sha1(GUID)`:
     `<inicio>_<nombre>_<guid8>_diagnostic_packet.json`.
   - Los pulls `absent` o `ambiguous` no entran en ninguna estadística; se listan en
     `pulls_without_player` (con estado y candidatos) de cada packet cuyo intervalo de sesión,
     ampliado con el hueco permitido, contiene su inicio. Si ningún pull resuelve, no se escribe
     packet y el resumen del CLI lo dice (`resuelto en 0 de N pulls: A ausentes, B ambiguos`).
   - **Duplicados** (mismo GUID, encuentro e inicio en ms desde dos logs): gana el pull completo
     frente al incompleto; a igual completitud, el de mayor fin observado; después, el nombre de
     fichero de log menor. No se fusionan eventos. El packet conserva las referencias de todas las
     copias en `sources` y declara en `duplicate_conflicts` las copias completas cuyos totales
     difieren. El resultado no depende del orden de recolección.
7. **Pulls incompletos.** Sin `ENCOUNTER_END` no hay `duration_ms` ni fin del encuentro. Se define
   un **fin de observación** = timestamp de la última línea vista del segmento. Los totales se
   publican; las tasas usan `observed_seconds` con `duration_basis: "observation_end"` y nombre
   propio (`dps_observed`), nunca `dps_encounter`. Las auras abiertas se cierran con
   `end_basis: "observation_end"`. En la agrupación de sesión el fin del pull es su fin de
   observación. En los agregados cuentan como intento `incomplete` y quedan fuera de
   distribuciones, medianas y ventanas comunes, listados como excluidos con ese motivo.
8. **Cabecera del juego.** El tracker recuerda la última cabecera vista; `StateStore` la persiste
   en la entrada del perfil al hacer commit. Es exacto: `COMBAT_LOG_VERSION` vacía el buffer, así
   que un segmento pendiente nunca empieza antes de la última cabecera, y una cabecera anterior al
   offset comprometido es la que está guardada. Sin dato en estado se usa la primera línea del
   fichero. Procedencia publicada: `stream | state | file_start | unknown`.
9. **Semántica de daño.** Componentes publicados por separado; `effective = amount − max(overkill,
   0)` es la cifra principal. Se excluyen del daño hecho el autodaño y el daño a objetivos no
   hostiles (se reportan aparte). `absorbed_by_target` procede solo de `SPELL_ABSORBED` con el
   jugador como atacante (fuente única; el campo `absorbed` de la línea de daño no se vuelve a
   sumar). No se afirma equivalencia con otras herramientas.
10. **Mascotas.** Solo con owner conocido por la evidencia ya existente (`pet_owners`: summon o
    bloque de origen). Van en un cubo propio, nunca mezcladas en la tabla por hechizo del jugador.
11. **Límites de los intervalos de aura (observado frente a inferido).** Cada intervalo lleva
    `start_basis` (`observed` | `pre_context` | `combatant_info` | `unknown`) y `end_basis`
    (`observed` | `encounter_end` | `observation_end`). Una eliminación cuya aplicación no se vio
    y que no figura en el estado inicial produce un intervalo con `start: null` y
    `start_basis: "unknown"`: la lista de `COMBATANT_INFO` es parcial y el instante de aplicación
    no está establecido. Se publican dos cifras con nombres distintos: `uptime_observed_s` (solo
    intervalos con inicio establecido) y `uptime_upper_bound_s` (suponiendo activa desde `t = 0`
    en los de inicio desconocido), más `unknown_start_intervals`. Las ventanas de burst y sus
    estadísticas de entrada solo se construyen sobre intervalos con inicio establecido; los demás
    van a `partial_windows` sin estadísticas de entrada. Un intervalo termina solo con evidencia
    (`REMOVED`) o en el fin del encuentro/observación; no se cierra por la muerte, y el pull
    publica el instante de muerte.
12. **Estado vital.** `alive_at_start` es `true`, `false` o `unknown`, con `basis`: `pre_context`
    (un `UNIT_DIED` sin resurrección, o un cast propio, antes del inicio), o deducido de la primera
    evidencia dentro del encuentro (`first_action`, `death`, `resurrection`). Sin ninguna evidencia
    queda `unknown` y no se supone vivo: las métricas por tiempo vivo y la continuidad valen `null`
    con `reason`.
13. **Denominadores.** Toda tasa con denominador cero o desconocido se serializa como `null` con
    `reason`. `performance.json` y el packet se serializan con `allow_nan=False`: nunca `NaN` ni
    `Infinity`.
14. **Topes y métricas derivadas.** Valores por pull: `MAX_PERF_SPELLS = 256`,
    `MAX_PERF_TARGETS = 256`, `MAX_PERF_AURAS = 512`, `MAX_PERF_AURA_INTERVALS = 4000` (total),
    `MAX_PERF_TIMELINE = 6000`, `MAX_PERF_WINDOWS = 64`, `MAX_PERF_CONFIGS = 128`,
    `MAX_PERF_RESOURCE_POINTS = 240`. Se mantienen **en streaming, independientes del detalle
    retenido**: totales de daño, contadores de casts/hits/ticks, sumas de uptime por aura,
    estadísticas de huecos (número, suma, máximo), estadísticas de recursos y los agregados de
    cada ventana de burst ya abierta. Los hechizos u objetivos que excedan su tope se suman en un
    cubo `other` para que los totales sigan cuadrando. Lo que **sí** depende del detalle retenido
    (cronologías, listas de intervalos, ventanas a partir de la nº 65) se marca `partial: true`
    con `covered_until_s`; el packet excluye ese pull de las comparaciones de esa métrica y lo
    lista. La detección de ambigüedad de identidad no depende de ningún tope: cuando la caché de
    GUID ya comprobados está llena se compara el nombre directamente.
    - **Auras: dos saturaciones distintas.** (a) *Historial de intervalos truncado*
      (`MAX_PERF_AURA_INTERVALS`): la aura ya está seguida, su estado y sus sumas de uptime siguen
      en streaming y son exactas; solo su lista de intervalos queda `partial` con
      `covered_until_s`. (b) *Clave de aura rechazada* (`MAX_PERF_AURAS`): la aura nueva no se
      sigue, no se publica ninguna cifra para ella y el pull lleva
      `aura_coverage: {"complete": false, "rejected_keys": n, "first_rejected_s": t}` con el
      warning `perf_aura_keys_truncated`. El packet excluye ese pull de toda comparación de uptime
      y de ventanas de burst con el motivo `aura_keys_truncated`. Las auras que definen las reglas
      del spec (buff de burst, debuff de ventana, procs) reservan su clave al crear el acumulador,
      de modo que nunca son las rechazadas.

Decisión del owner del plan (2026-10-04): tras la ronda 2 de Codex GPT-6 Astra High (8 de 9
hallazgos resueltos) se adopta su resolución para F4, recogida en la decisión 14, y se confirma la
decisión 5: los packets propios que ya no se derivan de los resultados publicados se eliminan.
Revisión cerrada en dos rondas, sin una tercera.

## Files/interfaces touched

### `WoWLogExtractor/WoWLogExtractor.py`

- **Constantes.** `APP_VERSION`; `PERFORMANCE_SCHEMA_VERSION = 1`; `PACKET_SCHEMA_VERSION = 1`;
  `PERFORMANCE_RULES_VERSION = 1`; `DIAGNOSTICS_DIR_NAME = "Diagnostics"`; los topes
  `MAX_PERF_*` con los valores de la decisión 14; reglas `OPENER_SECONDS = 20`,
  `ACTION_GAP_SECONDS = 2.5`, `COMMON_WINDOWS = (30, 60, 120)`.
- **`OutputOptions`.** Campo `performance_player: str | None`; validación (requiere analysis);
  `performance_fingerprint`; sufijo `+perf-<h>` en `profile`; `as_dict` añade la clave
  `performance` solo cuando está activo.
- **Parser (funciones nuevas, sin tocar `ParsedCombatEvent.as_dict`).**
  - `parse_log_header(args) -> dict`: `combat_log_version`, `advanced_logging`, `build_version`,
    `project_id`; campos ausentes como `None`.
  - `_power_state(payload, base)`: `(info_guid, power_type, current, maximum, cost)` del bloque
    avanzado; `None` si el bloque no es reconocible o el tipo no es un entero simple.
  - `_damage_suffix(event, payload, value_index)`: `amount`, `overkill`, `absorbed`, `critical`
    con la misma detección moderna/legacy que ya usa el parser; `None` por campo no fiable.
  - `_energize_suffix(payload)`: `amount`, `over_energize`, `power_type`, `max_power`.
  - `_aura_stacks(payload)`: entero final de los eventos `*_DOSE`.
  - `parse_combatant_details(args)`: talentos, equipo `[(item_id, ilvl)]`, auras previas, huellas
    `sha1[:12]` del texto bruto de talentos y de equipo, y `status` por bloque: `ok | absent |
    unsupported_layout`.
- **`PerformanceAccumulator`** (uno por segmento de raid cuando el flag está activo).
  - `observe(timestamp, event, args, parsed, pet_owners, line_offset)`: llamado desde
    `AnalysisSession.consume` tras el aprendizaje de mascotas (después de W:1277) y antes de
    `_apply_policy` (W:1286). Recibe cada línea una vez, antes del filtro raw.
  - **Fase** por orden de eventos, no por timestamp: `pre` hasta el `ENCOUNTER_START` propio del
    segmento, `encounter` hasta su `ENCOUNTER_END`, `post` después. El `ENCOUNTER_START` propio es
    el que coincide en id **y** en timestamp con `segment.start_ts`: un pull corto anterior del
    mismo encuentro que quepa en el pre-contexto no se confunde con el actual, y sus
    `ENCOUNTER_START`/`ENCOUNTER_END` se tratan como pre-contexto. `pre` inicializa estado (auras,
    vida, mascotas, cast en curso); `post` solo aporta evidencia etiquetada.
  - **Identidad.** Coincidencia por GUID, o por nombre (completo o parte anterior al primer `-`,
    sin distinguir mayúsculas). Dos GUID distintos para un nombre → `status: ambiguous` con
    candidatos y sin métricas. Ninguno → `absent`. Se escribe `performance.json` en los tres casos.
    `COMBATANT_INFO` solo trae GUID y precede a la primera línea con nombre: mientras el jugador
    no está resuelto se retiene la configuración compacta de cada GUID (`MAX_PERF_CONFIGS`) y al
    resolver se adopta la suya, incluidas las auras iniciales; si se supera el tope, la
    configuración del jugador puede faltar y se publica `combatant_info: "not_retained"` con su
    warning, nunca la de otro GUID. La selección por nombre y por GUID debe dar métricas idénticas.
  - **Coste acotado.** Solo se interpretan en detalle las líneas cuyo origen o destino es el
    jugador o una mascota suya; el resto se descarta tras comparar GUID (y nombre, mientras haga
    falta para detectar ambigüedad).
  - **Acumulados.** Daño por hechizo y por objetivo (clave NPC id, nombre observado, nº de
    instancias); casts por hechizo (`start`, `success`, `failed` por motivo,
    `start_without_outcome`), con `started_before_pull` para los que cruzan el inicio; hits, ticks
    periódicos y críticos separados de los casts; hechizos con daño y sin cast; muertes,
    resurrecciones, tiempo vivo/muerto, `death_in_post_context`, daño tras la muerte; intervalos
    de auras sobre el jugador (cualquier origen) y de auras del jugador sobre otros; muestras de
    recursos; cronología de acciones.
  - **Límites.** Cada colección tiene tope `reject-new` y emite warning `{code, cap, dropped}`. Los
    totales siguen siendo exactos y cada métrica derivada del detalle retenido lleva su propio
    `partial`/`covered_until_s` (decisión 14): un warning en otra parte del JSON no basta.
  - `result(segment_metadata, game_context) -> dict`.
- **Reglas por spec.** `SPEC_RULES: dict[int, dict]` con `62 → ARCANE_RULES` (`id`, `version`,
  `validated_builds = ("12.1.0",)`, IDs del punto 13 de Discovery). Se selecciona por `spec_id`,
  nunca por nombre. `_arcane_section(acc, rules, build)` produce:
  - opener (primeros `OPENER_SECONDS`, más los casts iniciados antes del pull);
  - ventanas de burst = intervalos del buff `365362`, con casts por hechizo, daño dentro, maná al
    entrar y salir (muestra más cercana y su antigüedad), stacks de `263725` al entrar, desfase
    respecto al Toque de los magi más próximo (`210824`) y muerte dentro de la ventana;
  - procs: aplicaciones, refrescos con stacks al máximo observado y decrementos de `263725`;
  - cargas: ganancias y `over_energize` por hechizo (observado) y contador inferido (ganancias,
    reinicio en cada `44425`) con autocomprobación: con el contador a 4, un `ENERGIZE` con
    `amount 0` confirma y uno con `amount > 0` contradice; se publica la tasa de acuerdo;
  - continuidad: huecos sin cast ni tick de canalización mayores que `ACTION_GAP_SECONDS`, como
    «huecos observados entre acciones», causa no determinada, excluyendo el tiempo muerto;
  - `limitations`: lista fija y concreta de lo no evaluable.
  - Build ausente o no validada: las métricas observadas se publican igual, con
    `rules_validated_for_build: false`. Spec desconocido o sin reglas: sección
    `{"status": "not_applied", "reason": …}` y métricas generales intactas.
- **`AnalysisSession`.** Parámetro opcional `performance`; una llamada a `observe` en `consume`.
  Sin cambios en política, agregados ni salidas.
- **`Segment`.** Crea el acumulador en `begin_body` si el flag está activo y `kind == raid`; pasa
  el offset de cada línea (`start_offset + raw_bytes`) y la cabecera de juego recibida del tracker.
- **`SegmentTracker` / `FileProcessor`.** `log_header` + procedencia; se actualiza en `feed` y en
  warm-up al ver `COMBAT_LOG_VERSION`; `FileProcessor` lo inicializa desde estado o desde la
  primera línea del fichero.
- **`StateStore`.** `update(path, offset, log_header=None)` guarda `log_header` en la entrada del
  perfil; `get_log_header(path)` lo devuelve solo si el offset y los hashes validan. Sin cambios
  de versión ni de migración.
- **`SegmentPublisher`.** `_analysis_payload` añade `performance.json` al payload y a `artifacts`,
  y las claves `performance` (`fingerprint`, versiones, selector, `player_status`) y
  `performance_bytes` al marker. `publish` lo copia antes del marker y, si la publicación actual
  no lo produce, lo retira junto con el marker antiguo (W:1835). `analysis_bundle_bytes` conserva
  su definición.
- **Packet.** `collect_performance_results(raids_dir, fingerprint)`: acepta un pull solo si su
  marker existe, lista `performance.json`, su huella coincide, el fichero se parsea y su
  `segment_id` es el del marker; lo demás se cuenta como omitido con motivo.
  `build_sessions(results, gap_minutes)`, `build_packet(session, budget)` y
  `rebuild_diagnostics(output_dir, options, …)`: funciones puras sobre los dicts publicados, salida
  determinista (sin hora de generación), escritura atómica solo si los bytes cambian, y limpieza de
  packets propios obsoletos.
  - Contenido: versiones (esquemas, extractor, juego, reglas); jugador y configuraciones del
    personaje (spec, ilvl, huellas, pulls que usan cada una); sesión y procedencia; `definitions`
    (unidad y denominador de cada métrica); `data_quality`; agregados por grupo `(encuentro,
    dificultad, build, configuración)`; resumen por pull; observaciones; ventanas seleccionadas;
    referencias locales; `pulls_without_player`; `sources` por pull y `duplicate_conflicts`.
  - Agregados: intentos, kills/wipes/incompletos, distribución de duración; mediana, mínimo,
    máximo y cuartiles con `n`; tasa conjunta = Σ numeradores / Σ denominadores, con nombre
    distinto de la mediana por pull; ventanas comunes (`COMMON_WINDOWS`) solo con los pulls que
    las cubren, listando los excluidos y el motivo; firmas de opener con frecuencia `n/d`; pulls
    representativos con el criterio por el que se eligieron.
  - Observaciones: `{id, kind: observed|inferred, statement, numerator, denominator, pulls,
    excluded, evidence, applicability}`. Ninguna atribuye causa.
  - Referencias locales: fichero de log, `segment_id` y offsets de bytes (segmento, inicio y fin
    del encuentro), estables ante republicaciones; el nombre publicado va como pista no estable.
  - Presupuesto: orden fijo de reducción (cronologías de burst de pulls no representativos →
    cronologías de opener no representativas → tablas por hechizo/objetivo recortadas → listas de
    intervalos de auras → series de maná). Los resúmenes por pull nunca se recortan. `complete`,
    `budget.max_bytes`, `budget.actual_bytes` y `omitted[]` (qué, de qué pulls, por qué). Si lo
    esencial supera el presupuesto se emite igualmente con `budget_exceeded: true`. Solo bytes
    medidos; ninguna cifra de tokens.
- **`Extractor` / CLI.** Bandera `diagnostics_dirty`, inicialmente `True` cuando el flag está
  activo. `rebuild_diagnostics` se ejecuta al final de `_run_once` siempre, y en `_watch` al final
  de cada poll mientras la bandera esté activa: se activa al arrancar (reconciliación inicial,
  aunque no haya datos nuevos ni se publique nada), cuando un poll publica un segmento y cuando
  una reconstrucción falla; solo se desactiva tras una reconstrucción correcta. Un fallo de
  reconstrucción se reporta como error y no interrumpe el watch. Cambiar `--packet-max-bytes` o
  `--session-gap-minutes` y reiniciar reconstruye sin reprocesar segmentos. `prepare` limpia
  `*.tmp` de `Diagnostics/`. `build_parser` y `run` exponen los tres flags; el resumen final
  indica los packets escritos y los pulls sin jugador resuelto.

### Contrato de `performance.json` v1

Claves de primer nivel y forma. Tiempos `*_s` en segundos relativos al `ENCOUNTER_START` propio
(3 decimales); `t_ms` enteros. Toda tasa es `{"value", "numerator", "denominator_s"}` o
`{"value": null, "reason": "…"}`. Listas ordenadas de forma determinista (importe descendente y
después id ascendente, salvo cronologías, por tiempo).

```
performance_schema_version, extractor_version, fingerprint
rules:      {general_version, spec: {id, version, validated_for_build} | null}
segment:    {segment_id, encounter_id, boss, difficulty_id, difficulty, raid_size, start_time,
             end_time, duration_ms, complete, result: kill|wipe|incomplete,
             observed_seconds, duration_basis: encounter_end|observation_end}
source:     {file, segment_start_offset, encounter_start_offset, encounter_end_offset,
             observation_end_offset}
game:       {combat_log_version, advanced_logging, build_version, project_id,
             header_source: stream|state|file_start|unknown}
player:     {selector, status: resolved|absent|ambiguous, guid, name, candidates: [{guid, name}]}
```

Las claves siguientes solo existen con `player.status == "resolved"`:

```
character:  {combatant_info: ok|absent|not_retained|unsupported_layout, spec_id, class_id,
             item_level, talents: {status, fingerprint, count},
             equipment: {status, fingerprint, items: [[item_id, ilvl]]},
             initial_auras: {status, count}}
life:       {alive_at_start: true|false|"unknown", basis, deaths: [{t_s}],
             resurrections: [{t_s}], death_in_post_context: [{after_end_s}],
             alive_seconds, dead_seconds}
damage:     {player: {hits, ticks, crits, amount, overkill, effective, absorbed_by_target},
             pets:   {… mismas claves …, by_pet: [{npc_id, name, effective, by_spell: […]}]},
             total_effective, after_death_effective,
             excluded: {self_damage, non_hostile_target, pre_context, post_context},
             by_spell:  [{spell_id, name, hits, ticks, crits, amount, overkill, effective,
                          absorbed_by_target}],
             by_target: [{key, npc_id, name, kind: creature|vehicle|pet|player, instances, hits,
                          amount, overkill, effective, role: boss|unknown,
                          evidence: {max_hp, raid_marker, name_matches_encounter}}],
             damage_without_cast: [spell_id],
             rates: {dps_encounter, dps_while_alive, dps_observed}}
casts:      {by_spell: [{spell_id, name, start, success, failed: {motivo: n},
                         start_without_outcome, started_before_pull}],
             total_success, rates: {casts_per_minute}}
auras:      {coverage: {complete, rejected_keys, first_rejected_s},
             on_player:   [{spell_id, name, aura_type, source: self|other, applications,
                            refreshes, max_stacks, uptime_observed_s, uptime_upper_bound_s,
                            unknown_start_intervals,
                            intervals: [{start, end, start_basis, end_basis, max_stacks}],
                            partial, covered_until_s}],
             from_player: [{spell_id, name, aura_type, applications, refreshes, removals,
                            targets, intervals (solo auras de reglas de spec)}]}
resources:  {by_power_type: {"<tipo>": {samples, max_gap_s, first: {t_s, current, max},
                                        last: {…}, min_observed, max_observed,
                                        series: [[t_s, current]], partial,
                                        rates: {samples_per_minute}}},
             energize: [{spell_id, name, power_type, events, amount, over_energize}]}
continuity: {threshold_s, gaps_count, gaps_total_s, gap_max_s,
             longest: [{start_s, end_s, duration_s}], basis} | {value: null, reason}
timeline:   {entries: [[t_ms, kind, spell_id, target_key]], partial, covered_until_s}
             kind: start|success|failed
opener:     {seconds, entries: [[t_ms, kind, spell_id, target_key]], signature: [spell_id]}
spell_names:{"<spell_id>": nombre observado}
spec:       sección del spec | {status: "not_applied", reason}
warnings:   [{code, cap, dropped}]
```

`amount` es el campo del log (incluye overkill); `effective = amount − max(overkill, 0)`. El
daño de mascotas nunca entra en `by_spell`. `total_effective = player.effective +
pets.effective`. `dps_encounter` usa `duration_ms`; `dps_while_alive` usa el daño hecho estando
vivo sobre `alive_seconds`; `dps_observed` existe solo en pulls incompletos.

Añadidos tras la prueba de humo sobre el log real (2026-10-04): `character.item_level_basis`;
`damage.windows: [{seconds, effective, casts_success, covered}]` para cada valor de
`COMMON_WINDOWS` (daño efectivo de jugador y mascotas y casts completados en `[0, N]`, mantenidos
en streaming; `covered` es falso si el pull o el tiempo vivo no alcanzan `N`); las muestras de
recursos proceden de toda línea cuyo bloque avanzado describe al jugador, no solo de sus casts.

### Contrato de `diagnostic_packet.json` v1

Función pura y determinista de los `performance.json` válidos y de las opciones de packet. Sin
hora de generación. Los pulls se identifican con `pull_id` (`p01`…, por orden de inicio) y las
configuraciones con `config_id` (`c1`…). JSON compacto (`separators=(",", ":")`), UTF-8,
`allow_nan=False`.

```
packet_schema_version, extractor_version, complete
budget:    {max_bytes, actual_bytes, budget_exceeded, omitted: [{what, pulls, reason}]}
versions:  {performance_schema_version, fingerprint,
            rules: {general_version, spec: {id, version} | null}}
player:    {selector, guid, name}
session:   {id, start_time, end_time, crosses_midnight, gap_minutes, policy, source_files,
            pull_count}
game:      [{build_version, combat_log_version, header_source, rules_validated_for_build, pulls}]
character_configs: [{config_id, combatant_info, spec_id, class_id, item_level,
                     item_level_basis, talents_fingerprint, equipment_fingerprint, pulls}]
definitions: {"<métrica>": {description, unit, numerator, denominator}}
data_quality: {skipped_results: [{name, reason}], warnings: [{pull_id, code, cap, dropped}],
               unavailable: [{metric, pulls, reason}], duplicates: [{pull_id, sources}],
               duplicate_conflicts: [{pull_id, fields}]}
pulls_without_player: [{segment_id, start_time, boss, status, candidates}]
groups:    [{group_id, encounter_id, boss, difficulty_id, difficulty, build_version, config_id,
             pulls, attempts, kills, wipes, incomplete,
             duration_s: STATS,
             metrics: {"<métrica>": STATS + {excluded: [{pull_id, reason}]}},
             pooled:  {"<métrica>": {value, numerator, denominator, n, pulls}},
             common_windows: [{seconds, n, pulls, excluded, effective: STATS,
                               casts_success: STATS}],
             opener_signatures: {denominator, excluded,
                                 signatures: [{signature, count, pulls}]},
             spells:  [{spell_id, name, effective, share_of_effective, casts_success,
                        pulls_with_casts}],
             targets: [{key, npc_id, name, role, effective, share_of_effective, max_hp, pulls}],
             representative_pulls: [{pull_id, reason}]}]
pulls:     [{pull_id, segment_id, group_id, start_time, boss, result, complete, duration_s,
             duration_basis, raid_size, effective, pets_effective, absorbed_by_target,
             self_damage_excluded, dps_encounter, dps_while_alive, dps_observed,
             alive_at_start, deaths_s, death_in_post_context, alive_seconds, casts_success,
             casts_per_minute, top_spells: [[spell_id, effective, casts_success]],
             continuity: {gaps_count, gaps_total_s, gap_max_s} | null,
             resources: {"<tipo>": {samples, max_gap_s, min_observed, first, last}},
             spec: resumen de la sección de spec | null,
             partial: [métricas parciales], warnings: [códigos],
             sources: [{file, segment_id, segment_start_offset, encounter_start_offset,
                        encounter_end_offset, published_name}]}]
observations: [{id, kind: observed|inferred, statement, numerator, denominator, unit, pulls,
                excluded: [{pull_id, reason}], evidence: [{pull_id, t_s, ref}],
                applicability}]
evidence:  {openers: {"<pull_id>": {signature, entries}},
            burst_windows: {"<pull_id>": [ventanas]},
            gaps: {"<pull_id>": [huecos más largos]},
            deaths: {"<pull_id>": [{t_s, last_casts: [[t_ms, spell_id]]}]}}
spell_names: {"<spell_id>": nombre observado}
```

`STATS = {n, min, q1, median, q3, max}` sobre los pulls elegibles (cuartiles por interpolación
lineal, deterministas); con `n = 0` todos los campos son `null`. `pooled` es siempre Σ numeradores
/ Σ denominadores y lleva nombre propio (`dps_encounter_pooled`…), distinto de la mediana.

**Elegibilidad.** Un pull entra en las estadísticas de un grupo solo si es completo y el jugador
está resuelto; los incompletos cuentan en `attempts`/`incomplete`. Cada métrica añade sus propias
exclusiones (métrica `partial`, denominador nulo, `aura_keys_truncated`, pull más corto que la
ventana), siempre listadas con motivo.

**Ajustes tras revisar el packet del log real (2026-10-05).** (a) `MIN_COMPARABLE_SECONDS = 30`:
un pull completo más corto cuenta en `attempts` y en `duration_s`, pero queda fuera de las
estadísticas de tasas por pull (`dps_encounter`, `dps_while_alive`, `casts_per_minute`) y de la
elección de representativos, listado con motivo `pull_shorter_than_min_comparable`; las tasas
`pooled` siguen incluyéndolo porque ya ponderan por duración. (b) La firma de 12 casts es
demasiado específica para comparar (en el log real, 1 de 7): `opener_signatures.prefixes` publica,
para longitudes 2, 3, 4 y 6, el prefijo más común con su recuento y pulls, y `opener_consistency`
se formula sobre esos prefijos. (c) `charge_over_energize` usa como denominador el total generado
(ganadas + sobrantes), no solo las ganadas. (d) `deaths_before_end` indica el resultado del pull
junto a cada fracción, porque una muerte al 98 % de un wipe no es comparable con una muerte a
mitad de un kill.

**Ajustes tras la revisión mecánica del diff (2026-10-05).**
- *Lectura fallida ≠ resultado vacío.* Solo un directorio `Raids/` inexistente significa «sin
  resultados»; cualquier otro error de lectura hace fallar la reconstrucción, que no escribe ni
  borra nada y queda pendiente.
- *Sin reconstrucción tras errores de proceso.* Si la ejecución (o el poll de `--watch`) tuvo
  algún error al procesar logs, `Diagnostics/` se deja tal cual y la reconstrucción queda
  pendiente: una publicación a medias retira su marker y haría encoger o desaparecer el packet de
  su sesión.
- *`skipped_results` acotado y por sesión.* Un paquete omitido solo entra en el packet si su
  inicio (del `segment_id` del marker o, en su defecto, del nombre del directorio) cae en el
  intervalo de la sesión ampliado con el hueco; tope `MAX_PACKET_SKIPPED = 50` con
  `skipped_results_omitted`. Así un paquete antiguo ajeno no cambia los bytes de ningún packet.
- *Aislamiento de fallos.* Una excepción del acumulador o de un constructor de resultado no
  aborta la extracción: se deja de alimentar el acumulador y se publica un `performance.json`
  con `player.status: "error"` y `error: {type, message, phase}`, sin métricas; el packet lo lista
  en `pulls_without_player`. Los artefactos existentes no cambian.
- *Opener.* Del pre-contexto solo entra en `timeline`/`opener` el `start` de un cast que sigue
  pendiente al iniciar el pull; la firma se forma solo con casts completados dentro del encuentro.
- *Auras.* Las claves de reglas de spec ya creadas cuando se activan las reglas pasan a llevar
  historial; `auras.coverage.rejected_keys_is_lower_bound`; una aura con más portadores que el
  tope queda `partial` (motivo `metric_partial: aura_holders`).
- *Contrato.* `by_target[].kind` admite también `other` (GUID de tipo no reconocido).

**Ajustes tras la revisión cross-family del diff (2026-10-05, Codex GPT-6 Astra High).** Seis
hallazgos, todos corregidos y confirmados como resueltos en la re-revisión:
- *`--watch` tras una publicación fallida (P1, preexistente).* El offset en memoria avanza antes
  de publicar el segmento de EOF; un worker que falla ya no se conserva, y el poll siguiente lo
  recrea desde el offset confirmado y repite la publicación.
- *`stat` fallido leído como ausencia (P1).* La inspección de paquetes y de packets obsoletos
  propaga todo error que no sea `FileNotFoundError`; los candidatos a borrado se clasifican todos
  antes del primer borrado.
- *Estado inicial del spec.* Gancho `begin` de las reglas: se ejecuta antes de aplicar el primer
  evento del encuentro, de modo que los valores no dependen de qué evento llegue primero.
- *GUID exacto en `COMBATANT_INFO`* resuelve al jugador aunque no haya más eventos.
- *Reservas de auras desde el inicio.* Se reserva la unión de las auras de todas las reglas
  registradas antes de procesar el pre-contexto.
- *Casts por ventana acotados* (`MAX_PERF_WINDOW_SPELLS = 32`, cubo `other`, `casts_partial`).

Decisión del owner del plan (2026-10-05): la corrección del P1 de `--watch` introdujo una
regresión P2 (un Ctrl+C a mitad de poll dejaba sin publicar un pull ya terminado). A petición del
usuario se revisa esa corrección con Codex fuera del tope de dos rondas. El primer intento
(restaurar el worker salvo que una bandera marcara su publicación como interrumpida) fue
rechazado: una interrupción puede caer entre dos instrucciones cualesquiera, y Codex reprodujo
cuatro ventanas (bandera activada después de desacoplar el segmento; offset antiguo confirmado
contra un log reemplazado o truncado; `pop` fuera del bloque protegido; recuento doble). Diseño
definitivo: no se decide si un worker interrumpido está «sano». El worker permanece en el
diccionario, y el que estaba en curso al llegar el Ctrl+C **publica** lo que ya vio terminar
(idempotente: los mismos nombres al repetir) pero **nunca confirma su offset**; los recuentos son
de un solo uso (`take_counts`) y, con una interrupción en curso, `Diagnostics/` no se reconstruye
hasta la siguiente ejecución.

Verificación de ese diseño (2026-10-05). Codex agotó su cuota a mitad de la revisión del rediseño
y no emitió veredicto. En su lugar se hizo un barrido exhaustivo: un `KeyboardInterrupt` inyectado
en cada línea ejecutada del camino de `--watch`, en cuatro escenarios (log activo, log rotado, dos
pulls, log reemplazado entre polls) y con y sin `--performance-player`, comprobando tras cada uno
que una ejecución posterior converge (cada pull publicado una vez, offset al final, packet
coherente con los markers, nada contado dos veces). El barrido encontró un fallo **preexistente**
(igual en `main`): el cierre confirmaba el offset de un worker sin comprobar si su log había sido
reemplazado desde el último poll, de modo que un Ctrl+C entre polls tras un reemplazo saltaba el
primer pull del fichero nuevo. Corregido: el cierre no confirma si `identity_changed()`.
Pendiente y fuera de alcance, también preexistente: una interrupción en la ventana de una
instrucción entre `mkstemp`/`os.fdopen` y la asignación siguiente (`_atomic_write_bytes`,
`_copy_atomic`) puede convertir el Ctrl+C en un `OSError` y dejar un `.state.json.*.tmp` en la
raíz de salida; no pierde datos.

Revisión de Codex del rediseño (2026-10-05, GPT-6 Astra High, tras reiniciarse su cuota): sus
cuatro hallazgos anteriores, resueltos; pide cambios por un hallazgo nuevo y señala tres
heredados de `main`. Decisión del usuario: corregir el nuevo y el heredado nº 1; documentar los
otros dos.
- *Corregido (P2 nuevo).* El cierre decidía si reconstruir `Diagnostics/` con un recuento de
  errores «del poll», que aún no estaba anotado si el Ctrl+C llegaba justo tras el bucle. Lo que
  importa no es un recuento por poll sino un hecho que persiste: `unreplayed`, el conjunto de logs
  cuyo worker falló y cuyos datos aún no se han repetido con éxito. Mientras no esté vacío no se
  reconstruye, sea cual sea el poll o el instante del Ctrl+C.
- *Corregido (heredado nº 1).* El worker de un log desaparecido se retiraba antes de finalizarlo:
  un Ctrl+C en medio perdía el único ejemplar del pull que retenía. Ahora sigue registrado hasta
  terminar, y el cierre puede publicarlo.
- *Limitación conocida, heredada, fuera de alcance (nº 2).* Si el Ctrl+C interrumpe la
  publicación de un segmento cuya fuente ya no existe (rescate de un log reemplazado, o log
  borrado), ese pull se pierde: sus bytes solo estaban en el segmento desacoplado. Evitarlo
  exige staging recuperable entre procesos.
- *Limitación conocida, heredada, fuera de alcance (nº 3).* Entre `identity_changed()` y
  `StateStore.update()` el fichero puede ser reemplazado: `update` vuelve a calcular los hashes
  sobre lo que haya en la ruta. Cerrarlo exige atar la huella del checkpoint a los bytes
  realmente procesados, no al contenido actual de la ruta.
- *Segunda ronda sobre estas correcciones (Codex, 2026-10-06): pide cambios otra vez.* El
  conjunto `unreplayed` se vaciaba antes de tiempo en dos secuencias (el log fallido desaparece;
  el log pasa a ser el más reciente y se relee sin republicar), y la reconstrucción borraba un
  packet existente. Diagnóstico del owner: tres rondas intentando que el bucle de `--watch`
  *recuerde* en memoria si hay un paquete a medias; la verdad está en el disco. **Decisión del
  usuario: las dos cosas.** (A) Regla basada en disco en `rebuild_diagnostics`: un packet
  existente que incluya un pull cuyo paquete está en disco sin marker (`marker_missing` o
  `marker_unreadable`) queda retenido, ni se reescribe ni se borra, hasta que el paquete se
  repare o desaparezca; se informa como `held`. (B) `--watch` publica `performance.json` por
  pull pero **no reconstruye `Diagnostics/`** en esta versión: se imprime un aviso y los packets
  se generan con una ejecución normal posterior. Esto sustituye el disparador de reconstrucción
  en `--watch` acordado en la revisión del plan (F5): menos piezas móviles en la superficie que
  más fallos ha concentrado. Se eliminan `unreplayed` y `lost_errors`; se conservan la repetición
  de un worker fallido, la no confirmación del worker interrumpido o con log reemplazado, los
  recuentos de un solo uso y la finalización del log desaparecido sin retirarlo antes.
- *Revisión de Codex de (A) y (B) (2026-10-06): **aprobado**, sin hallazgos bloqueantes.* Una
  observación P3: la limpieza de arranque (`prepare`) retira los `.tmp` propios de `Diagnostics/`
  también antes de un `--watch`; la documentación lo dice con precisión en vez de afirmar que
  `--watch` no toca nada bajo `Diagnostics/`. Riesgo aceptado: si el paquete incompleto era el
  primer pull de una sesión, la sesión recalculada empieza más tarde y recibe otro nombre de
  fichero, de modo que coexisten el packet retenido y el nuevo hasta que el paquete se repara.
- *Sobre el barrido.* No es exhaustivo y no debe llamarse así: una interrupción por ejecución, en
  líneas de una ejecución sin errores, con granularidad de línea (no de bytecode ni dentro de
  llamadas en C), solo en analysis-only y con tres polls. Su oráculo además dejaba de exigir el
  pull antiguo tras un reemplazo, por lo que daba por buena la limitación nº 2. Corregido el
  oráculo y añadidos los escenarios de publicación fallida y de log desaparecido.

**Pulls representativos** (criterio publicado en `reason`): el kill más reciente si lo hay, el
wipe más largo, el pull de `dps_encounter` mediano y el de menor `dps_encounter` entre los
elegibles; sin repetir.

**Observaciones v1.** Generales: `deaths_before_end` (pulls con muerte dentro del encuentro sobre
pulls elegibles, con la fracción de duración a la que ocurre), `dead_time_share` (Σ segundos
muerto / Σ duración), `action_gap_share` (Σ segundos en huecos observados / Σ segundos vivo; causa
no determinada), `opener_consistency` (frecuencia de la firma más común),
`damage_spell_concentration` (reparto del daño efectivo por hechizo). Arcane, solo con la sección
de spec aplicada: `surge_first_use` (instante del primer uso), `surge_touch_order`,
`burst_window_casts` (casts por ventana y ventanas con muerte dentro),
`clearcasting_refresh_at_max` (refrescos observados con stacks al máximo; no implica
desperdicio), `charge_over_energize` (Σ `over_energize` / Σ ganancias, por hechizo),
`barrage_inferred_charges` (`kind: inferred`, con la tasa de acuerdo de la autocomprobación en
`applicability`). Ninguna observación atribuye causa ni usa las palabras «perdido»,
«desperdiciado» o «movimiento»; `statement` es una plantilla fija en inglés con las cifras.

**Evidencia y presupuesto.** `evidence` lleva detalle de los pulls representativos primero. Orden
fijo de reducción cuando `actual_bytes > max_bytes`: (1) `evidence.burst_windows` de pulls no
representativos; (2) `evidence.openers.entries` de no representativos (se conserva la firma);
(3) `evidence.gaps` y `evidence.deaths` de no representativos; (4) `groups[].spells` y
`targets` a los 8 y 5 primeros, `pulls[].top_spells` a 5; (5) `evidence` de representativos en el
mismo orden. `pulls`, `groups` (salvo el recorte del paso 4), `observations`, `definitions` y
`data_quality` nunca se eliminan. Cada paso aplicado añade una entrada a `budget.omitted`;
`complete` es falso si hay alguna. Si tras todos los pasos se sigue por encima,
`budget_exceeded: true`. `actual_bytes` es el tamaño real del fichero escrito (se calcula
iterando hasta punto fijo, ya que el propio campo forma parte del JSON).

### Otros

- `WoWLogExtractor/tests/test_extractor.py`: clases nuevas (lista en Verification).
- `WoWLogExtractor/examples/diagnostic_packet_example.json`: packet generado desde el fixture
  sintético del test end-to-end; un test comprueba que el fichero coincide byte a byte.
- `WoWLogExtractor/README.md` y `README.md`: uso, layout, esquema de ambos JSON, definiciones de
  métricas, política de sesión, presupuesto, límites de interpretación y números medidos.
- `CLAUDE.md`: la lista de perfiles incluye `--performance-player` y `Diagnostics/`.
- `plans/raid-performance-diagnostics.md` y `plans/raid-performance-diagnostics-checklist.md`.

## Ordered task breakdown

1. **Base.** Rama `feature/raid-performance-diagnostics`; checklist; suite verde (113).
2. **Primitivas de parseo.** `parse_log_header`, `_power_state`, `_damage_suffix`,
   `_energize_suffix`, `_aura_stacks`, `parse_combatant_details`, con tests sobre líneas reales
   (punto 4, 8 y 9 de Discovery) y payloads malformados.
3. **Opciones y contexto.** `OutputOptions` + CLI + huella de perfil; cabecera en tracker,
   `FileProcessor` y `StateStore`. Tests de perfil, de reanudación y de compatibilidad sin flag.
4. **Acumulador general.** Fase, identidad, daño, casts, muertes, auras, recursos, cronología,
   topes. Tests unitarios con totales calculables a mano.
5. **Reglas Arcane.** Sección de spec, inferencia de cargas con autocomprobación, limitaciones.
6. **Publicación.** `performance.json` en payload, marker y ZIP; retirada del fichero obsoleto.
   Matriz de fallos de `CLAUDE.md`, ejecutada y verde **antes** de integrar el packet, para que un
   fallo quede aislado en una sola capa de publicación.
7. **Packet.** Recolección, sesiones, agregados, observaciones, presupuesto, reconstrucción y
   limpieza; disparadores en `_run_once`, `_watch` y `prepare`.
8. **End-to-end y regresión.** Sesión sintética de varios pulls; ejemplo versionado; byte a byte
   de los modos existentes.
9. **Log real.** Copia en scratch; ejecución con `--analysis-only --performance-player Dkyam`;
   tiempo, memoria pico y tamaños; contraste manual con el sondeo independiente.
10. **Documentación.** Ambos README, `CLAUDE.md`, checklist.
11. **Embudo de revisión.** Mecánica, empírica y cross-family sobre el diff real (incluido el
    working tree sin commit).

## Verification criteria

### Comandos

- `python -m unittest discover -s WoWLogExtractor/tests`: 113 previos + nuevos, todos verdes.
- `python -m py_compile WoWLogExtractor/WoWLogExtractor.py`.
- `python WoWLogExtractor/WoWLogExtractor.py --help`: muestra los tres flags;
  `--performance-player` sin modo analysis falla con mensaje claro.
- `git diff --check`.
- Cada test de regresión se comprueba fallando con su cambio revertido.

### Tests de comportamiento (resultado esperado calculable)

- **Límites.** Daño y casts en pre y post-contexto no entran en los totales; un cast iniciado
  antes del `ENCOUNTER_START` y completado dentro cuenta una vez con `started_before_pull`; un aura
  aplicada en pre está activa en `t = 0`; dos líneas con el mismo timestamp a ambos lados del
  `ENCOUNTER_START` caen en fases distintas; un pull corto anterior del mismo encuentro dentro del
  pre-contexto (su `ENCOUNTER_START` y `ENCOUNTER_END` incluidos) no desplaza los límites ni
  aporta daño al pull actual.
- **Pull incompleto.** Log que termina tras `ENCOUNTER_START`: `duration_ms` nulo,
  `duration_basis: "observation_end"`, `dps_observed` con el denominador esperado y
  `dps_encounter` nulo con motivo; auras abiertas con `end_basis: "observation_end"`; seguido de
  otro pull, la sesión usa el fin de observación; excluido de medianas y ventanas comunes y listado
  como tal; al completarse, su publicación sustituye a la incompleta en el packet.
- **Casts frente a impactos.** Una canalización con un `CAST_SUCCESS` y N ticks da 1 cast y N
  hits; daño periódico contado como ticks; `CAST_START` sin desenlace no se llama cancelación;
  hechizo con daño y sin cast en su cubo propio.
- **Daño.** `overkill -1` frente a positivo; golpe totalmente absorbido (solo `SPELL_ABSORBED`);
  parcialmente absorbido sin doble suma; autodaño excluido; objetivo no hostil excluido; par
  `SWING_DAMAGE`/`SWING_DAMAGE_LANDED` contado una vez.
- **Auras.** Aplicación, refresh, `APPLIED_DOSE`, `REMOVED_DOSE` (no cierra el intervalo),
  eliminación, estado inicial desde `COMBATANT_INFO`, aura abierta al final del encuentro y
  `uptime_observed_s` exacto en el caso con todos los límites observados. Fixture solo-eliminación
  (eliminada en `t = 30` sin aplicación ni estado inicial): `start: null`,
  `start_basis: "unknown"`, `uptime_observed_s = 0`, `uptime_upper_bound_s = 30`,
  `unknown_start_intervals = 1`. Fixture con estado inicial parcial: el aura listada tiene
  `start_basis: "combatant_info"` y la no listada queda con inicio desconocido. Un buff de burst
  con inicio desconocido va a `partial_windows` sin estadísticas de entrada y no cuenta en las
  comparaciones de ventanas.
- **Muerte.** Muerte dentro del encuentro, resurrección, tiempo vivo/muerto, daño posterior fuera
  del numerador por tiempo vivo, muerte en post-contexto (forma del pull 12) como evidencia y no
  como muerte del encuentro. Estado inicial: muerto conocido por pre-contexto (`alive_at_start:
  false`, `basis: "pre_context"`); desconocido seguido de resurrección (`false`, `basis:
  "resurrection"`, tiempo muerto desde `t = 0`); sin ninguna evidencia (`unknown`, tasas por
  tiempo vivo `null` con `reason`).
- **Denominadores.** Observación de duración cero y tiempo vivo cero: tasas `null` con `reason`;
  el JSON resultante se parsea con un decodificador estricto y no contiene `NaN` ni `Infinity`.
- **Mascotas.** Owner por summon; owner desconocido no atribuido; GUID que difiere del invocado no
  atribuido; daño de mascota fuera de la tabla del jugador y sin doble conteo.
- **Recursos.** Bloque del lanzador frente al del objetivo (el HP y el poder del boss nunca se
  toman como del jugador); bloque ausente; muestreo parcial con `sample_count` y hueco máximo;
  `over_energize`; contador de cargas con autocomprobación que acierta y que falla.
- **Contexto.** Cabecera fuera del pre-contexto; reanudación por offset con cabecera en estado;
  sin cabecera (`unknown`); build no validada; cambio de talentos y de equipo entre pulls en grupos
  distintos; `COMBATANT_INFO` ausente, parcial y con layout no soportado.
- **Identidad.** Jugador ausente; nombre ambiguo; selección por GUID, nombre completo y corto.
  Con la primera línea con nombre posterior a `COMBATANT_INFO`, la selección por nombre y por
  GUID producen el mismo `performance.json` salvo el selector (spec, talentos, equipo y auras
  iniciales incluidos). Un segundo GUID coincidente que aparece después de llenar la caché de GUID
  comprobados sigue produciendo `ambiguous`. Fixture con el mismo nombre corto resolviendo a GUID A
  en unos pulls y a GUID B en otros, más un pull ausente y uno ambiguo: dos packets con nombres de
  fichero distintos, sin estadísticas combinadas, y los pulls sin jugador en
  `pulls_without_player`.
- **Agregados.** Pulls de distinta duración; tasa conjunta ≠ mediana por pull con valores
  conocidos; ventana común que excluye el pull corto y lo declara; kills, wipes e incompletos.
- **Límites y presupuesto.** Para cada tope, un pull sintético largo que lo supera con eventos
  significativos **después** de la saturación: las colecciones no crecen más allá del tope, el
  warning existe, los totales (daño, casts, huecos) coinciden con los de una ejecución sin tope
  alcanzado sobre los mismos eventos, y un burst, hueco o ventana común posterior a la saturación
  aparece como `partial` con `covered_until_s` y queda excluido de la comparación en el packet,
  nunca como resultado completo. Auras, dos fixtures: (a) historial de intervalos saturado → el
  uptime de cada aura seguida coincide con el de la ejecución sin tope y solo la lista es
  `partial`; (b) claves saturadas, con una aura nueva aplicada en `t = 10` y eliminada en
  `t = 30` → esa aura no aparece con ninguna cifra, `aura_coverage.complete` es `false` con
  `rejected_keys = 1`, las auras ya seguidas conservan su uptime exacto, el pull queda excluido de
  las comparaciones de uptime y burst con motivo `aura_keys_truncated`, y la aura de burst del
  spec sigue seguida aunque aparezca después de la saturación. Presupuesto pequeño → `complete: false`, `omitted`
  describe lo recortado y los resúmenes por pull siguen completos; presupuesto insuficiente para
  lo esencial → `budget_exceeded`.
- **Idempotencia y recuperación.** Segunda ejecución `(0, 0, 0)` y packet con bytes idénticos;
  `_INCOMPLETE` que se completa sustituye su contribución; fallo entre la publicación de un pull y
  el packet se repara en la siguiente ejecución sin datos nuevos; con el packet borrado y sin
  datos nuevos, un poll ocioso de `--watch` lo reconstruye antes del cierre y sin republicar
  segmentos; un fallo de escritura del packet en `--watch` se reintenta en el poll siguiente; un
  fallo al escribir un packet de reemplazo conserva los anteriores y no borra obsoletos; packet
  de otro jugador eliminado al cambiar de selector; fichero ajeno en `Diagnostics/` intacto;
  sesión que cruza medianoche y sesión repartida en dos ficheros; hueco mayor que el umbral parte
  la sesión; cambiar `--session-gap-minutes` o `--packet-max-bytes` reconstruye sin reprocesar.
- **Duplicados.** Par incompleto/completo del mismo pull en dos logs: gana el completo y `sources`
  lista ambos; dos copias completas con totales distintos: ganador por la regla de desempate y
  `duplicate_conflicts` declarado. Ambos casos con el orden de recolección invertido dan packets
  byte a byte idénticos.
- **Matriz de perfiles (`CLAUDE.md`).** Para `jugador A ↔ sin flag`, `A ↔ B` y cambio de versión
  de reglas: fallo inyectado a mitad de la republicación (`_copy_atomic` en `performance.json` y
  en `deaths.json`; `_atomic_write_bytes` en el marker), comprobación de que no queda marker ni
  `performance.json` obsoleto anunciado, vuelta al perfil anterior con reparación, y repetición
  `(0, 0, 0)`. `performance.json` desaparece al republicar sin el flag.
- **Aislamiento.** `Diagnostics/` sobrevive a `cleanup_partials` y `_purge_stale`.
- **Regresión.** Sin el flag: perfil, `state.json`, marker y los cinco artefactos idénticos a los
  actuales; cuerpo full byte a byte en `full`, `--analysis`, `--gzip`; `combat.txt` idéntico con y
  sin el flag.
- **End-to-end.** Sesión sintética de dos bosses y varios pulls (kill, wipes, uno corto, uno con
  muerte): un `performance.json` por pull y un packet coherente con ellos; el ejemplo versionado
  coincide byte a byte.

### Log real (copia en scratch; el original no se modifica)

- Tamaño y `mtime` del original iguales antes y después.
- 14 `performance.json`, un packet, `player.status: resolved`, GUID `Player-1378-0B46B91A`,
  spec 62, build `12.1.0`, una única configuración de talentos y equipo.
- Contraste con el sondeo independiente: duración de cada pull = `fightMs`; `amount` bruto de
  `7268` y `44425` igual a `171 584 155` y `121 536 754`; `44425` con 1 042 casts y `5143` con
  738; `1309786` ausente del daño hecho; 12 muertes en encuentro + 1 en post-contexto (pull 12);
  opener del pull 3 con `365350` → `321507` → `5143`; muestras de maná y hueco máximo en el rango
  medido.
- Recursos, medidos en esta máquina sobre el mismo log y con `--analysis-only`, con y sin el
  flag: memoria pico del proceso con el flag ≤ la de sin el flag + 50 MB; tiempo total con el flag
  ≤ 1,35 × el de sin el flag. Se registran ambas mediciones y los bytes de `performance.json` y
  del packet; se inspeccionan los warnings de topes (se espera ninguno en este log; si aparece
  alguno se documenta el detalle retenido). Superar un límite es un fallo de aceptación, no una
  nota.
- Segunda ejecución `(0, 0, 0)` sin cambios en el packet. Con `--packet-max-bytes` reducido el
  packet declara lo omitido.
- Los resultados sintéticos no se presentan como validación sobre la raid real, y viceversa.

### Documentación

- README con uso, esquema, definiciones, política de sesión, límites de interpretación y los
  números medidos (no estimados).
