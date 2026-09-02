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

`--analysis` y `--analysis-only` no se pueden combinar. `--bundle` requiere uno de esos dos modos de análisis:

```bat
Run WoW Log Extractor.bat --analysis --bundle
Run WoW Log Extractor.bat --analysis-only --gzip --bundle
```

`--keep-player-damage` también requiere `--analysis` o `--analysis-only`; usado sin un modo de análisis falla con un mensaje claro.

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

## Modo `--watch`

Para extraer mientras juegas:

```bat
Run WoW Log Extractor.bat --watch --analysis
```

La herramienta sigue el log activo y publica cada run/pull cuando termina. Para salir, pulsa `Ctrl+C`. Puedes añadir `--gzip` o `--bundle` a un modo de análisis si lo necesitas.

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
