# WoW Log Extractor

## Qué hace

Extrae automáticamente cada **run de Mythic+** y cada **pull de raid** de los combat logs de World of Warcraft Retail (`WoWCombatLog*.txt`). La herramienta solo **lee** los logs originales: nunca los modifica ni los borra.

Sin flags mantiene la salida legacy: un cuerpo completo lossless y un JSON de metadata por segmento. Opcionalmente puede añadir un paquete de análisis estructurado y menor, pensado para compartir e inspeccionar una run o pull sin enviar todo el log.

Cada cuerpo completo incluye el combate y aproximadamente 10 segundos de contexto antes y después. El análisis conserva, sin reescribirlas, las líneas de combate seleccionadas por relevancia objetiva.

## Uso rápido

Haz doble clic en `Run WoW Log Extractor.bat`. La primera vez intenta detectar la carpeta `_retail_\Logs` mediante el registro de Windows y rutas habituales. Si no la encuentra, pide la ruta manualmente. Los resultados se guardan, por defecto, junto al script en `WoWCombatLog Extracted`.

También puedes ejecutarlo desde una consola:

```text
cd WoWLogExtractor

# Full only
python WoWLogExtractor.py

# Full + analysis
python WoWLogExtractor.py --analysis

# Analysis only
python WoWLogExtractor.py --analysis-only

# Analysis compressed
python WoWLogExtractor.py --analysis-only --gzip

# Watch
python WoWLogExtractor.py --watch --analysis
```

## Modos de salida

| Comando | Resultado |
| --- | --- |
| `Run WoW Log Extractor.bat` | Modo completo legacy: `<basename>.txt` y `<basename>.json`. |
| `Run WoW Log Extractor.bat --analysis` | El full legacy más `<basename>/analysis/`. |
| `Run WoW Log Extractor.bat --analysis-only` | Solo `<basename>/analysis/`; no crea un full nuevo. |
| `Run WoW Log Extractor.bat --analysis-only --gzip` | Igual que el anterior, pero `analysis/combat.txt.gz`. |
| `Run WoW Log Extractor.bat --watch --analysis` | Vigila el log activo y publica full + análisis al terminar cada segmento. |
| `Run WoW Log Extractor.bat --analysis --keep-player-damage` | Igual que `--analysis`, pero conserva en `combat.txt` las líneas de daño saliente jugador/mascota -> NPC (ver "Qué se descarta de `combat.txt`"). |
| `Run WoW Log Extractor.bat --analysis-only --performance-player Dkyam` | Igual que `--analysis-only`, más `analysis/performance.json` por pull de raid y un `Diagnostics/*_diagnostic_packet.json` por sesión (ver "Rendimiento de raid por jugador"). |

`--analysis` y `--analysis-only` no se pueden combinar. `--bundle` requiere uno de esos dos modos de análisis:

```bat
Run WoW Log Extractor.bat --analysis --bundle
Run WoW Log Extractor.bat --analysis-only --gzip --bundle
```

`--keep-player-damage` también requiere `--analysis` o `--analysis-only`; usado sin un modo de análisis falla con un mensaje claro.

Flags de rendimiento de raid (ver "Rendimiento de raid por jugador"):

- `--performance-player SELECTOR`: activa `performance.json` por pull de raid y el packet de diagnóstico para un jugador. Requiere `--analysis` o `--analysis-only`; sin modo de análisis falla con un mensaje claro.
- `--packet-max-bytes N`: presupuesto de tamaño de cada packet (por defecto 200000 bytes). Solo afecta al packet.
- `--session-gap-minutes N`: hueco máximo entre pulls de una misma sesión (por defecto 120). Solo afecta al packet.

El ZIP contiene solo los cinco archivos del análisis; nunca incluye el full. Para un análisis normal, comparte `analysis/` o el ZIP; en runs grandes se recomienda `--gzip` o `--bundle`, porque el filtrado conservador puede dejar un `combat.txt` sin comprimir todavía grande. Comparte el full únicamente cuando alguien necesite depurar un caso excepcional o una herramienta requiera el log sin filtrar.

## Layout compatible con la salida legacy

Los ficheros legacy siguen directamente dentro de `MPlus/` o `Raids/`. El análisis vive en un directorio con el mismo basename:

```text
WoWCombatLog Extracted/
  MPlus/
    2026-08-30_10-25_MPlus_Valle-Cegador_+10.txt
    2026-08-30_10-25_MPlus_Valle-Cegador_+10.json
    2026-08-30_10-25_MPlus_Valle-Cegador_+10/
      analysis/
        combat.txt
        summary.json
        deaths.json
        players.json
        metadata.json
    2026-08-30_10-25_MPlus_Valle-Cegador_+10_analysis.zip
```

Con `--gzip`, cada cuerpo solicitado usa `.gz` en vez de `.txt`: el full es `<basename>.txt.gz` y el cuerpo reducido es `analysis/combat.txt.gz`. La compresión es determinista y lossless: al descomprimir un full se recuperan exactamente sus bytes originales, y al descomprimir `combat.txt.gz` se recuperan exactamente las líneas seleccionadas de `combat.txt`. No altera los JSON.

`--analysis-only` deja intacto un full de una ejecución anterior; simplemente no publica un full nuevo. El mismo `segment_id` permite que los modos reutilicen el mismo basename y que un segmento `_INCOMPLETE` que más tarde se complete converja en lugar de duplicarse.

## Contenido del paquete de análisis

El directorio `analysis/` y el ZIP contienen estos cinco archivos:

| Archivo | Contenido |
| --- | --- |
| `combat.txt` o `combat.txt.gz` | Líneas raw seleccionadas, en su orden original. No se transforma cada línea. |
| `summary.json` | Resumen factual del segmento: tipo (Mythic+ o raid), identidad, duración, resultado, contadores y datos objetivos de casts, interrupciones y dispels disponibles. |
| `deaths.json` | Una entrada por muerte de jugador, con la ventana causal disponible, golpe final cuando existe, auras activas y eventos/líneas raw relacionados. |
| `players.json` | Agregados best-effort por jugador; las mascotas se atribuyen al propietario solo cuando hay evidencia. |
| `metadata.json` | Versión de esquema, `segment_id`, perfil de salida, artefactos publicados, tamaños y warnings objetivos. |

Desde `analysis_schema_version: 2` (publicado en `metadata.json`), estas son las formas exactas:

- `players.json` es un objeto `{"players": [...]}` (en v1 era una lista suelta). Cada jugador trae `guid`, `name`, `class_id`, `spec_id`, `role`, `item_level` (media redondeada de los ilvl > 0 del equipo reportado en `COMBATANT_INFO`; aproximado, `null` cuando no hay dato disponible), `deaths`, `interrupts`, `dispels`, `damage_done`, `damage_taken`, `healing_done`, `healing_received`, `self_healing`, `absorbs_received` y `pets`.
- `summary.json.enemy_cast_successes` es ahora una lista de `{"spell_id", "spell_name", "count"}` ordenada por `count` descendente (en v1 era un objeto indexado por spell id).
- Las entradas de interrupción usan `interrupted_spell_id`/`interrupted_spell`; las de dispel, `dispelled_spell_id`/`dispelled_spell`; los eventos de absorción, `shield_spell_id`/`shield_spell`; y los eventos `COMBATANT_INFO` dentro de `deaths.json` usan `spec_id`/`item_level`. Las claves de v1 `extra_spell_id`/`extra_spell_name` ya no existen.

Los JSON describen hechos observados. No determinan culpa, evitabilidad, si una interrupción era posible ni la disponibilidad teórica de cooldowns. Cuando el formato no permite derivar un campo con confianza, el campo queda ausente o `null`; los límites de seguridad o fallos de parseo se expresan mediante warnings objetivos. Las estadísticas son best-effort, no rankings ni parses de Warcraft Logs.

`analysis/combat.txt` no pretende ser un fichero válido para subir a Warcraft Logs (WCL), ni sustituye al full para ese propósito. Es una selección de líneas para análisis local o compartido junto con sus JSON.

## Qué se descarta de `combat.txt`

El paquete de análisis no es un recorte arbitrario: cada línea se decide una sola vez con la misma política, así que nunca se cuenta dos veces ni se escribe una línea marcada para descartar. En resumen: se descartan los eventos de recursos (energía/maná ganada, drenajes, leech) porque no aportan evidencia de interacción; se descarta el resultado del daño saliente de jugadores y mascotas contra NPCs (pero se sigue sumando a los agregados); y se descarta cualquier línea NPC->NPC o mascota irrelevante->mascota irrelevante que no involucre a ningún jugador. Todo lo demás -estructura del log, muertes, interrupciones, dispels, invocaciones, casts, auras y daño/heal recibido por jugadores o sus mascotas propias- se conserva.

| Categoría | Eventos | ¿En `combat.txt`? |
| --- | --- | :-: |
| Se descarta siempre | `SPELL_ENERGIZE`, `SPELL_PERIODIC_ENERGIZE`, `SPELL_DRAIN`, `SPELL_LEECH`; `SWING_DAMAGE_LANDED` con destino NPC y origen no propio (p. ej. NPC contra NPC); líneas NPC->NPC sin actor relevante; heal entre mascotas irrelevantes | No |
| Se descarta de `combat.txt` por defecto, pero se sigue contando en `event_counts` y sumando a `damage_done` en `players.json` | Resultado de daño jugador/mascota -> NPC: `SWING_DAMAGE`, `SPELL_DAMAGE`, `SPELL_PERIODIC_DAMAGE`, `RANGE_DAMAGE`, `DAMAGE_SHIELD`, `DAMAGE_SPLIT`; `SPELL_ABSORBED` saliente (solo cuenta en `event_counts`, no suma a `damage_done`) | No (sí con `--keep-player-damage`) |
| Se descarta de `combat.txt` por defecto y nunca se agrega | `SWING_DAMAGE_LANDED` con destino NPC y origen jugador o mascota propia (el golpe ya lo cuenta su `SWING_DAMAGE` emparejado) | No (sí con `--keep-player-damage`) |
| Se conserva siempre | Eventos estructurales, `COMBATANT_INFO`, líneas no parseables (fallback de parseo), muertes, `PARTY_KILL`, todo evento con destino un jugador o su mascota propia (incluido `SWING_DAMAGE_LANDED`), casts, interrupciones, dispels, invocaciones, misses/auras de jugador sobre NPC, casts/auras hostiles | Sí |

El full sigue siendo el fallback sin pérdida: si algo no aparece en `combat.txt`, está garantizado en el log completo.

Un caso particular: `SWING_DAMAGE_LANDED` con destino un jugador **sí** se conserva, aunque a primera vista parezca "solo resultado", porque su bloque avanzado trae el HP de la víctima (`target_hp`/`target_max_hp`), necesario para reconstruir la ventana de muerte. Para no contar el mismo golpe de melee dos veces, dentro de `deaths.json` ese registro se serializa con `"supplemental_state": true` y sin `amount`/`absorbed`: el único `amount` del golpe lo aporta la línea `SWING_DAMAGE` emparejada.

## Tamaños y reducción

Al publicar un análisis, la consola y `metadata.json` informan de los tamaños observados por segmento. En una Mythic+ real (Valle Cegador +10, 87 MB de full) la reducción medida de `combat.txt` fue de 47,4 % con la política por defecto; con `--keep-player-damage` baja a unos pocos puntos. El resto de bytes que quedan en `combat.txt` corresponden sobre todo a heals y auras entre jugadores y al daño que reciben, que se conservan a propósito porque son necesarios para reconstruir la ventana de muerte y el estado del grupo.

- `full_uncompressed_bytes`: bytes raw del segmento completo, incluso en `--analysis-only`.
- `full_stored_bytes`: bytes del full publicado; es `null` si no se publicó un full.
- `combat_uncompressed_bytes` y `combat_stored_bytes`: tamaño del cuerpo de análisis antes y después de gzip, si se solicitó.
- `analysis_bundle_bytes`: suma de `combat` almacenado, `summary.json`, `deaths.json` y `players.json`; excluye `metadata.json` para evitar una medida circular.
- `analysis_zip_bytes`: tamaño del ZIP cuando se solicita `--bundle`.
- `reduction_percent`: compara `combat` y full **sin comprimir**. Por ello mide el filtrado, no una diferencia accidental de contenedores de compresión.

La consola puede mostrar también el tamaño real de la carpeta `analysis/` con sus cinco archivos. Dentro del ZIP, `metadata.json` deja `analysis_zip_bytes` como `null`; el `metadata.json` publicado junto al análisis contiene el tamaño final del ZIP.

## Procesamiento incremental y perfiles

La herramienta guarda su progreso en `state.json` para no repetir logs ya procesados. El estado se separa por perfil de salida: full, análisis, solo análisis, gzip, bundle y `--keep-player-damage` tienen sus propios offsets (este último añade el sufijo `+keep-player-damage` al perfil). Como todos los perfiles publican sobre los mismos nombres (`<basename>.txt` y `<basename>/analysis/`), la última ejecución con un perfil distinto se convierte en la dueña del estado de cada log (aunque no publique nada nuevo): al cambiar de flags se vuelve a procesar ese log una vez y se republican sus segmentos sobre los mismos nombres (sin duplicados, y sin borrar artefactos de otros perfiles); repetir las mismas flags no publica nada. Antes de reemplazar un paquete `analysis/` se retira su `metadata.json` anterior, así que un paquete con `metadata.json` presente es siempre un paquete completo del perfil que indica.

`--reset-state` borra los offsets de todos los perfiles y vuelve a escanear los logs. Los nombres son estables y los artefactos completos se publican antes de avanzar el offset, de modo que una nueva ejecución converge sobre un único conjunto de archivos para el segmento. La herramienta mantiene un bloqueo exclusivo sobre el directorio de salida: no ejecutes dos instancias a la vez contra la misma salida.

## Rendimiento de raid por jugador

### Qué es y para qué

Con `--performance-player` la herramienta calcula, para **un jugador por ejecución**, un `performance.json` por cada pull de raid y un `diagnostic_packet.json` por sesión y jugador. El packet resume los pulls agrupados por boss y lleva la evidencia suficiente para que un LLM (o una persona) razone sobre patrones de ejecución **sin recibir los logs**: es el fichero que se comparte. Solo se procesan pulls de raid; en esta versión los segmentos de Mythic+ no generan `performance.json`.

### Uso

```text
python WoWLogExtractor.py --analysis-only --performance-player Dkyam
python WoWLogExtractor.py --analysis-only --performance-player Dkyam --packet-max-bytes 100000 --session-gap-minutes 90
```

`--performance-player` requiere `--analysis` o `--analysis-only`. El selector puede ser:

- un GUID: `Player-…`;
- un nombre completo: `Nombre-Reino-Región`;
- un nombre corto: `Nombre` (sin distinguir mayúsculas; coincide con la parte anterior al primer `-`).

Si el jugador no aparece en un pull, `performance.json` se escribe igualmente con `player.status: "absent"` y sin métricas. Si el nombre corto corresponde a dos GUID distintos dentro de un mismo pull, el estado es `"ambiguous"`, con la lista de candidatos y sin métricas. Si el análisis de rendimiento de un pull falla (un error inesperado al procesar sus líneas o al construir el resultado), la extracción y los demás artefactos no cambian, y `performance.json` se escribe con `player.status: "error"`, sin métricas y con `error: {type, message, phase}`. Si ningún pull resuelve al jugador no se escribe ningún packet, y la línea de resumen de la consola lo indica, por ejemplo:

```text
Diagnostics: 0 packet(s) written, 0 unchanged, 0 removed; player resolved in 0 of 14 pulls: 14 absent, 0 ambiguous, 0 failed
```

`--packet-max-bytes N` (por defecto 200000) y `--session-gap-minutes N` (por defecto 120) solo afectan al packet: cambiarlos no reprocesa los logs, solo reconstruye `Diagnostics/` a partir de los `performance.json` ya publicados.

### Dónde sale

| Archivo | Contenido | ¿Compartir? |
| --- | --- | :-: |
| `<basename>/analysis/performance.json` | Detalle por pull del jugador. Aparece en `artifacts` de `metadata.json` (junto con `performance_bytes` y el bloque `performance`) y se incluye en el ZIP de `--bundle`. | No: detalle local |
| `Diagnostics/<inicio>_<nombre>_<guid8>_diagnostic_packet.json` | Packet de sesión: agregados por boss, resumen por pull, observaciones y evidencia. `<inicio>` es `YYYY-MM-DD_HH-MM-SS`; `<guid8>` son los 8 primeros caracteres de `sha1(GUID)`. | Sí |

`Diagnostics/` cuelga directamente de la carpeta de salida, fuera de `MPlus/` y `Raids/`, y no lo tocan la purga de restos ni la limpieza de parciales.

### Perfiles y reprocesado

- El flag forma parte del perfil de salida: se añade el sufijo `+perf-<huella>`, donde la huella depende del selector normalizado, la versión del esquema, la versión de las reglas y la versión de cada perfil de spec. Cambiar el jugador, las reglas o el esquema es otro perfil y reprocesa cada log una vez. Sin el flag, el perfil y el estado son los de siempre.
- Pasar de un perfil con el flag a uno sin él retira `performance.json` de los pulls que se republican.
- Con el flag, `Diagnostics/` se reconstruye en cada ejecución a partir de los resultados ya publicados (no de deltas ni de offsets). Primero se escriben los packets nuevos o cambiados (solo si sus bytes difieren); solo si todas las escrituras tienen éxito se eliminan los packets propios que ya no se derivan de esos resultados (otro jugador, otra versión, una sesión que cambió de inicio). Solo se borran ficheros con el sufijo `_diagnostic_packet.json` que además se parsean como packet; cualquier otro fichero de `Diagnostics/` queda intacto. Si una escritura falla, se conservan los packets anteriores, no se borra nada y la reconstrucción queda pendiente hasta la siguiente ejecución. Lo mismo si no se puede listar `Raids/`, leer un paquete o leer un packet existente: solo una carpeta inexistente cuenta como «sin resultados». Una ejecución en la que el procesado de algún log da error no reconstruye `Diagnostics/`: lo deja tal cual, lo indica con una línea en la consola y la reconstrucción se hace en la siguiente ejecución sin errores.
- Un paquete incompleto (una carpeta `analysis/` sin `metadata.json`, o con un `metadata.json` que no se puede parsear) puede ser una republicación cortada a medias, porque el marcador es lo primero que se retira. Todo packet existente que liste ese paquete en `pulls[].sources[].published_name` queda retenido: no se sobrescribe ni se borra, y la línea de resumen termina en `; N packet(s) kept: a pull is being republished`. Se libera cuando el paquete se repara (reprocesando el log, por ejemplo con `--reset-state`, vuelve su `metadata.json`) o cuando esa carpeta desaparece (borrarla o moverla fuera de `Raids/`); la siguiente ejecución reconstruye o borra entonces el packet con las reglas normales.
- `--watch` publica `performance.json` en cada pull, pero nunca reconstruye `Diagnostics/`: no crea, reescribe ni borra ningún packet (la limpieza de arranque sí retira los `.tmp` propios que dejara un fallo anterior). Al salir lo recuerda con la línea `Diagnostics: not rebuilt in --watch mode; run once without --watch to rebuild the packets`. Ejecuta después una vez sin `--watch` con el mismo flag para reconstruir los packets.
- Una ejecución sin el flag nunca toca `Diagnostics/`.
- Una segunda ejecución con las mismas flags no republica nada y deja el packet con los mismos bytes: el packet no incluye hora de generación.

### Política de sesión

Las sesiones se construyen **por GUID resuelto**: es la cadena máxima de pulls de ese GUID, ordenados por inicio, en la que el hueco entre el fin observado de un pull y el inicio del siguiente no supera `--session-gap-minutes`. Por tanto:

- No depende de la fecha: una noche que cruza medianoche es una sesión (`session.crosses_midnight`).
- No depende del fichero de log: varios logs se unen si el hueco lo permite (`session.source_files`).
- Un cambio de build, talentos o equipo **no parte la sesión**: parte los grupos de comparación (`groups`, por encuentro, dificultad, build y configuración del personaje).
- Un nombre corto que resuelve a GUID distintos en pulls distintos produce packets distintos, nunca estadísticas combinadas.
- Los pulls `absent`, `ambiguous` o `error` no entran en ninguna estadística; se listan en `pulls_without_player` de cada packet cuyo intervalo de sesión, ampliado con el hueco permitido, contiene su inicio.
- Duplicados (mismo GUID, encuentro e inicio en milisegundos, en dos logs): gana el pull completo frente al incompleto; a igual completitud, el de mayor fin observado; después, el nombre de log menor. No se fusionan eventos. `sources` conserva las referencias de todas las copias y `data_quality.duplicate_conflicts` declara las copias completas cuyos totales difieren. El resultado no depende del orden de recolección.

### Esquema de `performance.json` (v1)

Los tiempos `*_s` son segundos relativos al `ENCOUNTER_START` propio del pull (3 decimales). Toda tasa es `{"value", "numerator", "denominator_s"}` o `{"value": null, "reason": "…"}`. El JSON se escribe con `allow_nan=False`: nunca contiene `NaN` ni `Infinity`. Las listas tienen un orden determinista (importe descendente y después id ascendente, salvo las cronologías, por tiempo).

| Clave | Contenido |
| --- | --- |
| `performance_schema_version`, `extractor_version`, `fingerprint` | Versiones y huella del perfil de rendimiento. |
| `rules` | `general_version` y `spec: {id, version, validated_for_build}` (o `null`). |
| `segment` | Identidad del pull: `segment_id`, `encounter_id`, `boss`, dificultad, `raid_size`, `start_time`, `end_time`, `duration_ms`, `complete`, `result` (`kill`, `wipe` o `incomplete`), `observed_seconds`, `duration_basis` (`encounter_end` u `observation_end`). |
| `source` | Fichero de log y offsets de bytes (segmento, inicio y fin del encuentro, fin de observación). |
| `game` | Cabecera del log: versión, logging avanzado, `build_version`, `project_id` y `header_source` (`stream`, `state`, `file_start` o `unknown`). |
| `player` | `selector`, `status` (`resolved`, `absent`, `ambiguous` o `error`), `guid`, `name`, `candidates`. Con `error`, además `error: {type, message, phase}` (`observe` o `result`). |
| `character` * | `combatant_info` (`ok`, `absent`, `not_retained`, `unsupported_layout`), spec, clase, `item_level` e `item_level_basis`, huellas de talentos y equipo, auras iniciales. |
| `life` * | `alive_at_start` (`true`, `false` o `"unknown"`) con su `basis`, muertes, resurrecciones, `death_in_post_context`, `alive_seconds`, `dead_seconds`. |
| `damage` * | `player` y `pets` (hits, ticks, críticos, `amount`, `overkill`, `effective`, `absorbed_by_target`), `total_effective`, `after_death_effective`, `excluded`, `by_spell`, `by_target`, `damage_without_cast`, `windows` (ventanas comunes) y `rates`. |
| `casts` * | Por hechizo: `start`, `success`, `failed` por motivo, `start_without_outcome`, `started_before_pull`; `total_success` y `casts_per_minute`. |
| `auras` * | `coverage`, auras sobre el jugador (con intervalos) y auras del jugador sobre otros. |
| `resources` * | Muestras por tipo de poder y eventos de energize. |
| `continuity` * | Huecos observados entre acciones. |
| `timeline`, `opener` * | Cronología de acciones (`[t_ms, kind, spell_id, target_key]`) y primeros `OPENER_SECONDS` (20) segundos con su firma. |
| `spell_names` | Nombre observado por `spell_id`. |
| `spec` * | Sección del spec (ver "Perfil Arcane") o `{"status": "not_applied", "reason": …}`. |
| `warnings` | `[{code, cap, dropped}]` de los topes alcanzados. |

\* Solo existe cuando `player.status` es `resolved`.

Topes por pull: `MAX_PERF_SPELLS = 256`, `MAX_PERF_TARGETS = 256`, `MAX_PERF_AURAS = 512`, `MAX_PERF_AURA_INTERVALS = 4000` (total), `MAX_PERF_TIMELINE = 6000`, `MAX_PERF_WINDOWS = 64`, `MAX_PERF_WINDOW_SPELLS = 32` (hechizos distintos por ventana de spec), `MAX_PERF_CONFIGS = 128`, `MAX_PERF_RESOURCE_POINTS = 240`. Los totales (daño, casts, uptime, huecos, recursos) se mantienen en streaming y siguen siendo exactos al saturar; los hechizos u objetivos que exceden su tope se suman en un cubo `other` (en una ventana de spec, la ventana y su sección llevan además `casts_partial: true`, warning `perf_window_spells_truncated`, y el pull lista `burst_window_casts`/`touch_window_casts` en `partial`; el total de casts de la ventana sigue siendo exacto). Lo que depende del detalle retenido (cronologías, listas de intervalos, ventanas a partir de la nº 65) se marca `partial: true` con `covered_until_s`, y el packet excluye ese pull de la comparación correspondiente y lo lista. Hay dos saturaciones de auras distintas: si se llena el historial de intervalos, el uptime de las auras ya seguidas sigue siendo exacto y solo su lista de intervalos queda `partial`; si se rechaza una clave de aura nueva, esa aura no se publica con ninguna cifra, el pull lleva `auras.coverage: {complete: false, rejected_keys, rejected_keys_is_lower_bound, first_rejected_s}` (`rejected_keys_is_lower_bound: true` cuando ya hay `MAX_PERF_AURAS` claves rechazadas distintas y se deja de contar) con el warning `perf_aura_keys_truncated`, y el packet lo excluye de las comparaciones de uptime y de ventanas de burst con el motivo `aura_keys_truncated`. Las auras que definen las reglas del spec reservan su clave desde el primer evento, también en el precontexto y antes de conocer el spec (se reserva la unión, pequeña y fija, de las auras de todas las reglas registradas), así que nunca son las rechazadas; al conocerse el spec, solo las auras de objetivo de sus reglas conservan la lista de intervalos. Si un aura la tienen a la vez más de `MAX_PERF_INSTANCES` lanzadores u objetivos, su fila queda `partial: true` con `holders_truncated: true` y `covered_until_s` en el primer titular no seguido (warning `perf_aura_holders_truncated`), y el packet excluye ese pull de las comparaciones de uptime y de burst de esa aura con el motivo `metric_partial: aura_holders`.

### Definiciones de métricas

**Límites del encuentro.** Solo cuenta lo ocurrido entre el `ENCOUNTER_START` propio del pull y su `ENCOUNTER_END`, decidido por orden de eventos y no por timestamp (un pull corto anterior del mismo encuentro dentro del pre-contexto no se confunde con el actual). El pre-contexto solo inicializa estado (auras, vida, mascotas, cast en curso); el post-contexto solo aporta evidencia etiquetada (por ejemplo `death_in_post_context`).

**Daño: `amount`, `overkill`, `effective`.** `amount` es el campo del log y **incluye** el overkill; `overkill` es el exceso sobre la vida restante del objetivo (el log usa `-1` cuando no hay); `effective = amount − max(overkill, 0)` y es la cifra principal. Los tres se publican por separado.

**Qué no es daño hecho.** El autodaño (origen = destino = jugador) y el daño a objetivos no hostiles se excluyen y se reportan aparte en `damage.excluded`, igual que el daño de pre y post-contexto: no es daño del jugador sobre el encuentro. El daño a jugadores hostiles o a vehículos se clasifica por `kind` en `by_target`.

**`absorbed_by_target`.** Procede únicamente de `SPELL_ABSORBED` con el jugador como atacante. El campo `absorbed` de la línea de daño no se vuelve a sumar. Un golpe totalmente absorbido no genera línea de daño, solo `SPELL_ABSORBED`, así que solo así queda registrado. No se afirma equivalencia con otras herramientas.

**Mascotas.** Solo se atribuyen cuando el owner se conoce por la evidencia ya existente (un `SPELL_SUMMON` con el GUID exacto, o el bloque de origen). Van en su propio cubo (`damage.pets`, con `by_pet`) y nunca se mezclan en la tabla por hechizo del jugador. `total_effective = player.effective + pets.effective`.

**Casts, hits y ticks.** Son contadores distintos: un cast es un `SPELL_CAST_SUCCESS`; un hit es una línea de daño directo; un tick es una línea de daño periódico. Un canal es 1 cast más N ticks (p. ej. Misiles Arcanos cuenta un cast y N ticks de daño). `start_without_outcome` es un `SPELL_CAST_START` sin desenlace registrado: **no** se interpreta como cancelación. Los hechizos con daño y sin cast (p. ej. un proc) van a `damage_without_cast`; un cast iniciado antes del pull y completado dentro cuenta una vez, con `started_before_pull`.

**Tasas por tiempo.**

- `dps_encounter`: `total_effective` entre la duración completa del encuentro (`duration_ms`).
- `dps_while_alive`: daño hecho estando vivo (`total_effective − after_death_effective`) entre `alive_seconds`.
- `dps_observed`: solo en pulls incompletos, entre los segundos observados (hasta la última línea vista); en ellos `dps_encounter` es `null` con `reason`.
- Cualquier tasa con denominador cero o desconocido es `null` con `reason`.

**Tasa conjunta frente a mediana.** En el packet, la tasa conjunta (`*_pooled`) es Σ numeradores / Σ denominadores de los pulls elegibles; la mediana por pull es otra cifra con otro nombre. No son intercambiables: la conjunta pesa por duración. Las estadísticas son `{n, min, q1, median, q3, max}` (cuartiles por interpolación lineal) y con `n = 0` valen `null`.

**Auras.** Cada intervalo lleva `start_basis` (`observed`, `pre_context`, `combatant_info` o `unknown`) y `end_basis` (`observed`, `encounter_end` u `observation_end`). Una eliminación cuya aplicación no se vio y que no figura en el estado inicial produce un intervalo con `start: null` y `start_basis: "unknown"`, porque la lista de auras de `COMBATANT_INFO` es parcial. Se publican dos cifras con nombres distintos: `uptime_observed_s` (solo intervalos con inicio establecido) y `uptime_upper_bound_s` (suponiendo activa desde `t = 0` en los de inicio desconocido), más `unknown_start_intervals`. Un intervalo termina por evidencia (`REMOVED`) o en el fin del encuentro/observación; no se cierra por la muerte del jugador.

**Recursos.** Las muestras proceden de toda línea cuyo bloque avanzado describe **al propio jugador** (lanzador en un cast o energize; destino en daño o heal recibido), nunca de la vida o el poder del boss. El mínimo observado (`min_observed`) es el menor valor muestreado y **no** el mínimo real: entre muestras el valor pudo bajar más. `max_gap_s` es el mayor hueco entre muestras. Los puntos de `series` están acotados (`MAX_PERF_RESOURCE_POINTS`) y la serie se marca `partial` si se recorta.

**Continuidad.** `continuity` lista los huecos observados entre acciones consecutivas (casts completados o ticks de canal) dentro de un periodo vivo, mayores que `threshold_s` (`ACTION_GAP_SECONDS = 2.5`). Es un hecho observado, no una causa: no distingue movimiento, mecánica, decisión ni lag, y excluye el tiempo muerto.

**Boss o add.** `role` es `unknown` salvo que el nombre de la unidad coincida exactamente con el del encuentro (`boss`); no existe en el log un evento que identifique al boss y no hay una tabla de NPC por encuentro. `evidence` adjunta `max_hp`, `raid_marker` y `name_matches_encounter`.

**Pulls cortos y ventanas comunes.** `MIN_COMPARABLE_SECONDS = 30`: un pull completo más corto cuenta en `attempts` y en `duration_s`, pero queda fuera de las estadísticas de tasas por pull (`dps_encounter`, `dps_while_alive`, `casts_per_minute`) y de la elección de pulls representativos, con el motivo `pull_shorter_than_min_comparable` (las tasas `*_pooled` sí lo incluyen, porque ya ponderan por duración). Las ventanas comunes (`COMMON_WINDOWS = (30, 60, 120)` s) comparan el daño efectivo y los casts completados en `[0, N]` solo entre pulls que cubren `N` segundos con el jugador vivo; los demás se listan como excluidos con el motivo (`pull_shorter_than_window`, etc.). Los pulls incompletos quedan fuera de distribuciones, medianas y ventanas comunes y se listan como excluidos con ese motivo.

### Perfil Arcane

Para el spec 62 (Mago Arcano) se aplican las reglas `mage-arcane` v1, validadas contra la build `12.1.0`. Las reglas se seleccionan por `spec_id` y los hechizos y auras se identifican **por spell ID, nunca por nombre**. Con otra build (o sin cabecera), las métricas observadas se publican igual con `rules_validated_for_build: false`; con un spec sin reglas la sección es `{"status": "not_applied", "reason": …}` y las métricas generales no cambian. La sección `spec` informa de:

- **Opener**: primer éxito de Oleada Arcana y de Toque de los magi, y cuál fue primero.
- **Ventanas de burst**: intervalos del buff de Oleada Arcana, con casts por hechizo, daño efectivo dentro, maná al entrar y al salir (muestra más cercana dentro de 2 s, con su antigüedad), stacks de Lanzamiento libre al entrar, desfase respecto al Toque de los magi más próximo y muerte dentro de la ventana. Solo se construyen sobre intervalos con inicio establecido; los demás van a `partial_windows` sin estadísticas de entrada.
- **Ventanas de Toque** (debuff en el objetivo) y **procs**: aplicaciones y refrescos de Lanzamiento libre, refrescos con stacks al máximo observado, y decrementos de stacks emparejados con un cast de Misiles Arcanos en el mismo timestamp (el resto se publica como no explicado).
- **Cargas Arcanas**: las cargas **observadas** son las ganancias y el `over_energize` por hechizo de `SPELL_ENERGIZE`; el contador **inferido** parte de esas ganancias y se reinicia con cada Tromba Arcana. Como las cargas nunca aparecen como recurso en el log, el contador se contrasta con el propio log en cada energize (con el contador a 4, un energize de cantidad 0 confirma y uno de cantidad mayor que 0 contradice) y se publica la tasa de acuerdo (`agreement_rate`: `checks − contradicted` sobre `checks`). Lo inferido va siempre marcado `kind: inferred`.
- **Limitaciones** (lista fija en la sección, `limitations`):
  - No se evalúa la disponibilidad teórica de cooldowns ni los usos "perdidos" de Oleada Arcana, Toque de los magi u Orbe Arcano: requeriría modelar talentos, cargas y reinicios.
  - La expiración de Lanzamiento libre no se distingue de su consumo: un decremento solo se empareja con Misiles Arcanos si hay un `CAST_SUCCESS` de Misiles con el mismo timestamp; el resto se publica como no explicado.
  - Las Cargas Arcanas nunca se registran como recurso: el contador se infiere y se contrasta con el log.
  - No se separa el daño a boss y a adds: ningún evento del log identifica al boss.
  - No se evalúa el recorte de canalizaciones de Misiles Arcanos: los ticks cuentan como acciones y la duración pretendida del canal no se conoce.
  - La pertenencia a una ventana sigue el orden de líneas del log: un cast registrado antes de la aplicación del buff con el mismo timestamp (por ejemplo, el propio cast de Oleada Arcana) queda fuera de la ventana.
  - Las ventanas de Toque asocian el daño por clave de objetivo (NPC id): varias instancias del mismo NPC no se distinguen.

### Esquema del packet (v1)

JSON compacto, UTF-8, sin hora de generación (misma entrada, mismos bytes). Los pulls se identifican con `pull_id` (`p01`…, por orden de inicio) y las configuraciones con `config_id` (`c1`…).

| Clave | Contenido |
| --- | --- |
| `packet_schema_version`, `extractor_version`, `complete` | Versiones; `complete` es falso si el presupuesto obligó a omitir algo. |
| `budget` | `max_bytes`, `actual_bytes`, `budget_exceeded`, `omitted` (qué, de qué pulls, por qué). |
| `versions`, `player`, `session`, `game` | Versiones de esquema y reglas, jugador (`selector`, `guid`, `name`), sesión (inicio, fin, `crosses_midnight`, `gap_minutes`, política, ficheros, nº de pulls) y builds vistas con su `rules_validated_for_build`. |
| `character_configs` | Configuraciones del personaje (spec, ilvl, huellas de talentos y equipo) y los pulls que usa cada una. |
| `definitions` | Para cada métrica: descripción, unidad, numerador y denominador. |
| `data_quality` | `skipped_results` (paquetes de `Raids/` que no son un resultado válido, con su motivo; solo los que empiezan dentro del intervalo de la sesión ampliado con el hueco, como `pulls_without_player`, y como mucho 50, con `skipped_results_omitted` si hay más), `warnings` por pull, `unavailable`, `duplicates`, `duplicate_conflicts`. |
| `pulls_without_player` | Pulls `absent`/`ambiguous`/`error` cercanos a la sesión, con estado y candidatos. |
| `groups` | Un grupo por `(encuentro, dificultad, build, configuración)`: intentos, kills, wipes, incompletos, distribución de duración, `metrics` (estadísticas con `excluded` y motivo), `pooled`, `common_windows`, firmas de opener con su frecuencia `n/d` y `opener_signatures.prefixes` (para los 2, 3, 4 y 6 primeros casts, el prefijo más común con su recuento y su propio `denominator`: la firma completa de 12 casts rara vez se repite), `spells`, `targets` y `representative_pulls` (con el criterio en `reason`). |
| `pulls` | Resumen por pull: resultado, duración y base, tasas, vida, muertes, casts, continuidad, recursos, resumen del spec, métricas `partial`, warnings y `sources`. |
| `observations` | Ver abajo. |
| `evidence` | Detalle de pulls concretos: openers, ventanas de burst, huecos más largos y últimos casts antes de cada muerte; empieza por los pulls representativos. |
| `spell_names` | Nombre observado por `spell_id`. |

**Observaciones.** Cada una es `{id, kind, statement, numerator, denominator, unit, pulls, excluded, evidence, applicability}`. `kind` es `observed` (contado directamente del log) o `inferred` (derivado de un modelo, como el contador de cargas, con la tasa de acuerdo en `applicability`). `statement` es una plantilla fija en inglés con las cifras; `excluded` lista los pulls fuera del cálculo con su motivo; `evidence` apunta a `{pull_id, t_s, ref}`. Ninguna atribuye causa. Generales: `deaths_before_end` (con el resultado del pull junto a cada fracción), `dead_time_share`, `action_gap_share`, `opener_consistency` y `damage_spell_concentration`. Arcane, solo con el spec aplicado: `surge_first_use`, `surge_touch_order`, `burst_window_casts`, `clearcasting_refresh_at_max`, `charge_over_energize` (denominador: total generado, ganadas más sobrantes) y `barrage_inferred_charges`.

**Elegibilidad.** Un pull entra en las estadísticas de un grupo solo si está completo y el jugador está resuelto; los incompletos cuentan en `attempts` e `incomplete`. Cada métrica añade sus exclusiones (métrica `partial`, denominador nulo, `aura_keys_truncated`, pull más corto que la ventana o que `MIN_COMPARABLE_SECONDS`), siempre con motivo.

**Presupuesto.** `--packet-max-bytes` se mide en **bytes del fichero escrito**; no se estima ningún número de tokens. Si el packet lo supera se recorta en un orden fijo: evidencia de burst, openers, huecos y muertes de pulls no representativos; después las tablas `spells`/`targets` de cada grupo y `top_spells` de cada pull; por último la evidencia de los representativos. Los resúmenes por pull, `groups` (salvo ese recorte), `observations`, `definitions` y `data_quality` nunca se eliminan. Cada recorte añade una entrada a `budget.omitted` y deja `complete: false`. Si lo esencial por sí solo supera el presupuesto, el packet se emite igualmente con `budget_exceeded: true`.

**Referencias locales.** Cada entrada de `pulls[].sources` lleva el fichero de log original, el `segment_id` y los offsets de bytes (segmento, inicio y fin del encuentro). Esos datos son estables ante republicaciones; el nombre publicado (`published_name`) es solo una pista que puede cambiar.

### Límites de interpretación

- No hay comparación con rendimiento teórico, simulaciones, otros jugadores ni Warcraft Logs.
- El nivel de objeto no implica un DPS esperado.
- Las observaciones no atribuyen causa.
- Un hueco entre acciones no es "tiempo perdido": solo es un intervalo sin acciones observadas.
- No se evalúan la disponibilidad de cooldowns, los usos perdidos ni el uso incorrecto de procs.
- El número de Cargas Arcanas es una inferencia, no un dato del log.
- No se distingue el daño a boss del daño a adds (`role: unknown` salvo coincidencia exacta de nombre).
- Las mascotas sin evidencia de owner (por ejemplo, un reflejo cuyo GUID de daño difiere del invocado y cuyas líneas de daño no llevan owner) no se atribuyen al jugador.
- Los nombres son los del idioma del cliente que grabó el log; las claves son IDs.
- Los resultados con datos sintéticos (tests y ejemplo) comprueban el formato y el cálculo, no validan nada sobre una raid real.
- Nota conocida: `players.json` sigue calculando `item_level` con la media de todos los huecos con ilvl > 0, incluido el tabardo; el `item_level` de `performance.json` excluye camisa y tabardo (`item_level_basis`). Es una limitación existente, no una promesa de cambio.

### Ejemplo reproducible

`WoWLogExtractor/examples/diagnostic_packet_example.json` es un packet generado desde un fixture sintético por la suite de tests, que comprueba que el fichero coincide byte a byte. Para regenerarlo, ejecuta los tests con la variable de entorno `WLE_UPDATE_EXAMPLES=1`. Sus números ilustran solo el formato.

### Mediciones

Medido el 2026-10-05 en esta máquina sobre una noche de raid Heroico real (no son garantías):

| Medida | Valor |
| --- | --- |
| Log | 635 MB, 2,1 M líneas, 14 pulls |
| `--analysis-only` sin el flag | 83,3 s, 61,4 MB de memoria pico |
| `--analysis-only --performance-player` | 86,4 s, 60,7 MB de memoria pico |
| `performance.json` | 23-306 KB por pull; 2,3 MB los 14 |
| Packet con el presupuesto por defecto (200 000 bytes) | ≈ 99 KB (98 619 bytes), sin nada omitido |
| Packet con `--packet-max-bytes 30000` | Declara lo omitido y activa `budget_exceeded`: solo los resúmenes esenciales ocupan ≈ 66 KB |
| Segunda ejecución | No republica nada y deja el packet intacto (mismos bytes y misma fecha de modificación) |
| Vuelta a `--analysis-only` sin el flag | Retira `performance.json` de los 14 pulls y no toca `Diagnostics/` |

Contraste independiente superado: los totales de daño por hechizo, los recuentos de hits, los de casts y las muertes coinciden exactamente con un recuento independiente de una sola pasada sobre el log raw.

## Modo `--watch`

Para extraer mientras juegas:

```bat
Run WoW Log Extractor.bat --watch --analysis
```

La herramienta sigue el log activo y publica cada run/pull cuando termina. Para salir, pulsa `Ctrl+C`: se publican los runs/pulls que ya habían terminado. Si el `Ctrl+C` llega justo mientras se procesa un log, ese log no avanza su posición guardada (la siguiente ejecución lo vuelve a leer desde ahí y republica sobre los mismos nombres). Con `--performance-player`, `--watch` publica `performance.json` en cada pull pero no reconstruye `Diagnostics/`: ejecuta una vez sin `--watch` al terminar para reconstruir los packets. Puedes añadir `--gzip` o `--bundle` a un modo de análisis si lo necesitas.

## Activar el registro de combate avanzado en WoW

Para que los logs incluyan nombres de dungeon, nivel de key, afijos, encounter IDs y otros campos disponibles, activa **Registro de combate avanzado** (*Advanced Combat Logging*):

1. En el juego abre **Opciones → Sistema** (o **Red / Network**, según la versión) y marca la casilla.
2. Escribe `/combatlog` en el chat para empezar a grabar. WoW guarda el archivo dentro de `World of Warcraft\_retail_\Logs\`.

Algunos addons de M+ o raid activan el logging automáticamente al entrar a una instancia.

## Configuración

- `--reconfigure`: vuelve a detectar o pedir la carpeta de logs.
- `--log-dir "RUTA"`: usa una carpeta de logs puntual sin modificar la configuración guardada.
- `--output "RUTA"`: cambia la carpeta de salida.
- `--config "RUTA"`: usa otro `config.json`.

`config.json` se crea junto al script después de la primera ejecución. En Windows también puedes editarlo directamente.

## Requisitos

- Windows
- Python 3.10 o superior
- Sin dependencias externas: usa solo la biblioteca estándar de Python
