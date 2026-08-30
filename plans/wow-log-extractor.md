# WoWLogExtractor — extractor de M+ y raid pulls desde combat logs de WoW Retail

## Goal y out-of-scope

**Goal:** herramienta local para Windows, Python 3.10+ **solo stdlib**, que procesa los combat
logs de WoW Retail (Advanced Combat Logging) en streaming y genera un `.txt` independiente por
cada run de Mythic+ y por cada pull de boss de raid, más un `.json` de metadata por archivo.
Ejecutable con doble clic vía `.bat`, con modo `--watch` opcional, procesado incremental
(offsets guardados) y sin duplicados al re-ejecutar.

**Ubicaciones:** `WoWLogExtractor\` es un subdirectorio del repo
(`C:\Users\kamry\Documents\Repos\WoWLogsExtractor\WoWLogExtractor\`). Todos los comandos de
verificación se ejecutan desde la raíz del repo.

**Hecho verificado en la máquina del usuario (log real, COMBAT_LOG_VERSION 22, build 12.1.0):**
- Retail moderno escribe **un archivo por sesión**: `WoWCombatLog-MMDDYY_HHMMSS.txt` en
  `<WoW>\_retail_\Logs\`. El clásico `WoWCombatLog.txt` puede existir en configuraciones
  antiguas. La herramienta procesa **todos** los `WoWCombatLog*.txt` de la carpeta Logs.
- Formato de línea: `M/D/YYYY HH:MM:SS.ffff␣␣EVENTO,args...` (dos espacios como separador).
- `CHALLENGE_MODE_START,"Valle Cegador",2859,584,10,[158,9,10]` → nombre, mapID,
  challengeModeID, keyLevel, affixes.
- `CHALLENGE_MODE_END,2859,1,10,1960162,...` → mapID, completed(0/1), keyLevel, tiempo ms.
  **WoW emite un `CHALLENGE_MODE_END` espurio (completed=0) al entrar a la dungeon, antes del
  START** → un END sin START abierto se ignora.
- `ENCOUNTER_START,3199,"Nombre",8,5,2859` → encounterID, nombre, difficultyID, groupSize,
  instanceID. `ENCOUNTER_END,...,success,fightTimeMs`.

**Out-of-scope:** análisis del contenido (deaths, DPS, etc.); resolución de map IDs vía bases
de datos externas o Internet; GUI; soporte de Classic; compresión; filtrado de eventos (se
conservan TODAS las líneas de cada segmento, byte a byte).

## Archivos e interfaces

Todo en `WoWLogExtractor\`:

- `WoWLogExtractor.py` — script único, stdlib only. Componentes internos:

  - **`LogParser`** — streaming binario con offsets por bytes; lee en bloques, separa por
    `\n`; cada línea se decodifica (UTF-8, `errors='replace'`) solo para parsear; a la salida
    se escriben los **bytes originales** intactos. El troceo de argumentos del evento es
    **CSV-aware** (comillas dobles pueden contener comas: `"Council, Ascended"`); splitter
    propio pequeño, no `csv` module (las líneas no son CSV estricto: contienen `[...]`).
    **Línea final parcial:** un resto al final del archivo sin `\n` NO se procesa ni se
    compromete; el offset queda justo antes. Línea que no parsea (timestamp/evento ilegible):
    se escribe igualmente al segmento activo si lo hay; nunca rompe el proceso.

  - **`SegmentTracker`** — máquina de estados por archivo de log:
    - Ring buffer `deque` de `(timestamp, raw_bytes)`; se evictan líneas con
      `ts < ts_actual − 10 s`, tope duro 5000 líneas (si el tope recorta los 10 s, se acepta:
      es pre-contexto best-effort). Pre-contexto = contenido del buffer al abrir el segmento
      (incluye líneas con `ts ≥ start_ts − 10 s`, empates incluidos).
    - `CHALLENGE_MODE_START` → abre segmento M+ (buffer + línea). Mientras está abierto, TODO
      va al archivo de la M+; los `ENCOUNTER_*` internos NO generan archivos propios (solo
      alimentan `bosses` en metadata).
    - `CHALLENGE_MODE_END` con M+ abierta → fase de cola (trailing): sigue escribiendo líneas
      con `ts ≤ end_ts + 10 s` (empates incluidos); la primera línea posterior finaliza el
      segmento (esa línea no se escribe, pero queda en el buffer para el siguiente segmento).
      END sin START abierto → ignorar (caso espurio verificado).
    - `ENCOUNTER_START` fuera de M+ → segmento de raid pull; mismas reglas de buffer/cola.
      `ENCOUNTER_END` con `encounterID` distinto al abierto → finaliza el abierto como
      `_INCOMPLETE` e ignora el END. `CHALLENGE_MODE_END` con mapID distinto → se acepta
      (cierra la M+ con sus datos) — el mapID no cambia dentro de una run real.
    - Cierres forzosos: un nuevo START (de cualquier tipo, incluida la fase de cola), una
      línea `COMBAT_LOG_VERSION`, o un salto de timestamp hacia atrás > 30 s → finaliza el
      segmento abierto (`_INCOMPLETE` si no tenía END; **si ya tenía END y estaba en fase de
      cola, finaliza COMPLETO** con lo escrito hasta ahí).
    - EOF con segmento abierto: si ya tenía END → finalizar COMPLETO (la cola de 10 s es
      best-effort). Si no tenía END: archivo no-más-reciente o `mtime` > 15 min → finalizar
      `_INCOMPLETE`; si puede estar escribiéndose aún → dejar pendiente (el offset
      comprometido no avanza más allá del byte de inicio del pre-contexto del segmento;
      la próxima ejecución lo retoma desde ahí).

  - **`StateStore`** — `state.json` dentro de la carpeta de salida. Por archivo de log
    (clave: nombre): `offset` comprometido, `size` y `mtime` al commit, `head_hash` (SHA-1 de
    los primeros 256 bytes) y `tail_hash` (SHA-1 de los 256 bytes anteriores al offset).
    Reset a 0 (reprocesar) si: `size < offset`, `head_hash` distinto, o `tail_hash` distinto
    (detecta reemplazo con prefijo idéntico). Al reanudar, "warm-up": rebobina hasta 512 KB
    antes del offset solo para rellenar el ring buffer (sin abrir segmentos) y procesa normal
    desde el offset. Escritura atómica: temp + `os.replace`. **Orden de commit:** primero se
    publican los outputs del segmento, luego se avanza el estado; una interrupción entre ambos
    solo causa reescritura idempotente en la siguiente pasada, nunca pérdida.

  - **Identidad de segmento y nombres** (anti-duplicados/anti-sobrescritura):
    - Identidad estable = `(source_file, tipo, start_ts con precisión de ms, id principal)` —
      el nombre del archivo de log origen forma parte de la identidad, de modo que segmentos
      idénticos en logs distintos nunca colisionan. Se persiste en el `.json` (`segment_id`).
    - Nombre base según spec (`HH-MM`). Resolución: un nombre está "ocupado" si existe su
      `.txt` o su `.json`. Si el `.json` existente tiene el mismo `segment_id` → misma
      entidad, se sobreescribe (idempotente). Si tiene otro `segment_id` → variante con
      segundos (`HH-MM-SS`), después `-2`, `-3`… Un `.json` sin `.txt` con `segment_id`
      distinto es un huérfano de crash → reclamable (se sobreescribe). Resultado: dos pulls
      en el mismo minuto conviven de forma estable entre pasadas, watch, reruns y
      `--reset-state`.
    - **Protocolo de publicación recuperable** (orden fijo): (1) el cuerpo se escribe en
      `<nombre>.txt.partial` durante el segmento; (2) al finalizar se publica el `.json`
      (temp + `os.replace`); (3) se renombra `.txt.partial` → `.txt`; (4) se avanza
      `state.json`. Interrupción tras (2): queda json-sin-txt, reclamado en la siguiente
      pasada (mismo `segment_id` → se regenera en el mismo nombre). Interrupción tras (3):
      par completo publicado, el estado no avanzó → la siguiente pasada regenera y
      sobreescribe idénticamente. Nunca queda un `.txt` final sin `.json`. `.partial`
      huérfanos se eliminan al arrancar.

  - **`Config`** — `config.json` junto al script: `log_dir`, `output_dir`. Autodetección de
    `_retail_\Logs`: (1) registro de Windows (`HKLM\SOFTWARE\WOW6432Node\Blizzard
    Entertainment\World of Warcraft` → `InstallPath`), (2) escaneo superficial (profundidad
    acotada, sin recursión) de rutas habituales en las unidades fijas: `Program Files*`, raíz,
    `Games`, `Juegos`, `BattleNet`, `Battle.net`, `Blizzard` + `World of
    Warcraft\_retail_\Logs`. Cada candidato en try/except individual: una unidad inaccesible
    nunca aborta el escaneo. Si nada → `input()` interactivo, valida y guarda.

  - Tabla de dificultades: 1 Normal-Dungeon, 2 Heroic-Dungeon, 8 MythicKeystone, 14 Normal,
    15 Heroic, 16 Mythic, 17 LFR, 23 Mythic-Dungeon, 24/33 Timewalking; desconocido →
    `Difficulty<N>`.

  - Sanitizado de nombres: quitar `\/:*?"<>|` y controles, espacios→`-`, unicode intacto,
    recorte a longitud razonable.

  - **Esquema de metadata** (contrato completo; `null` cuando el dato no existe):
    - Comunes: `segment_id` (str), `type` (`"mythic_plus"`|`"raid"`), `date` (`YYYY-MM-DD`),
      `start_time` / `end_time` (`YYYY-MM-DD HH:MM:SS.mmm`, hora local del log; `end_time`
      null si incompleto), `complete` (bool — se vio el END), `source_file` (str),
      `context_seconds` (10), `lines` (int).
    - M+: `dungeon` (str|null), `map_id` (int), `challenge_mode_id` (int|null), `key_level`
      (int), `affixes` (list[int]), `completed` (bool|null — flag del END, null si incompleto),
      `duration_ms` (int|null), `bosses` (list de `{encounter_id, boss, success}`).
    - Raid: `encounter_id` (int), `boss` (str), `difficulty_id` (int), `difficulty` (str),
      `raid_size` (int|null), `success` (bool|null — null si incompleto),
      `duration_ms` (int|null).

  - Salida: `<carpeta del script>\WoWCombatLog Extracted\{MPlus,Raids}\` (configurable).

  - **CLI:** sin args = procesar lo nuevo y salir con resumen (`N runs, M pulls, E errors` +
    ruta de salida); `--watch` = polling cada 2 s sobre el archivo más reciente + detección de
    archivos nuevos (rotación: al aparecer uno más nuevo se apura el viejo hasta EOF y se
    aplican sus reglas de EOF) y truncados; Ctrl+C limpio (finaliza segmentos con END,
    persiste estado, deja pendientes los sin END). `--log-dir`, `--output`, `--reset-state`,
    `--reconfigure`. Solo lectura sobre los logs originales. Error por archivo: se reporta y
    se continúa con los demás. Error no capturado: traceback + `input()` para no cerrar la
    consola (solo en modo interactivo/bat).

- `Run WoW Log Extractor.bat` — doble clic; localiza `py`/`python`, lanza el script, pausa.
- `README.md` — uso, dónde sale la salida, activar Advanced Combat Logging, `--watch`,
  cambiar ruta del log, resetear estado.
- `tests\test_extractor.py` — unittest (stdlib), fragmentos sintéticos.

## Tareas ordenadas

1. Esqueleto de `WoWLogExtractor.py`: parseo de línea/timestamp, splitter CSV-aware, tabla de
   dificultades, sanitizado de nombres.
2. `SegmentTracker` completo (M+, raid, buffer 10 s, cola 10 s, incompletos, sesión nueva,
   END espurio, mismatch de IDs, reglas de EOF).
3. Escritura de salida: `.txt` byte a byte vía `.partial`+rename, `.json` metadata según
   esquema, identidad de segmento y resolución de nombres, estructura de carpetas.
4. `StateStore` + incremental + truncado/reemplazo (head+tail hash) + warm-up + orden de
   commit outputs→estado.
5. `Config` + autodetección (registro + escaneo resiliente) + prompt interactivo.
6. CLI (`main`), resumen, manejo de errores con pausa, `--watch` con rotación y Ctrl+C.
7. `Run WoW Log Extractor.bat` + `README.md`.
8. Tests sintéticos (lista en Verificación) + ejecución.
9. Prueba real de solo lectura sobre `D:\BattleNet\World of Warcraft\_retail_\Logs` +
   inspección manual de una muestra.

## Criterios de verificación

- `python -m unittest discover -s WoWLogExtractor/tests -v` (desde la raíz del repo) en verde.
  Casos: M+ completa; dos M+ consecutivas; raid wipe; raid kill; varios pulls del mismo boss
  (incluidos dos en el mismo minuto → dos archivos estables, re-ejecutar no cambia nombres ni
  pierde ninguno); ENCOUNTER dentro de M+ (no genera archivo aparte); encounter incompleto;
  challenge incompleto; END espurio pre-START; nombre de boss con coma entre comillas;
  unicode (ruso/español/apóstrofes); truncado/reemplazo del log (incl. reemplazo que vuelve a
  crecer por encima del offset); ejecución incremental sin duplicados (2ª pasada = 0 nuevos);
  segmento abierto que continúa en una 2ª ejecución (archivo crece entre pasadas); línea final
  parcial sin `\n` (no se pierde ni corrompe al completarse después); EOF con END visto pero
  <10 s de cola → COMPLETO, no `_INCOMPLETE`; pre/post contexto ~10 s con límites exactos
  (`ts ≥ start−10s`, `ts ≤ end+10s`); contenido del segmento == bytes originales del rango;
  **crash-injection**: interrumpir la publicación tras cada paso (json publicado / txt
  renombrado / antes de avanzar estado) y re-ejecutar → exactamente un par `.txt`+`.json`
  final por segmento, sin huérfanos ni duplicados; colisión mismo-minuto tras `--reset-state`
  → nombres estables; reemplazo de log legacy con mismos primeros 256 bytes, distinta cola y
  tamaño > offset → reprocesa desde 0 (tail_hash); rotación en watch con segmento abierto y
  con cola pendiente → el archivo viejo finaliza según sus reglas de EOF y el nuevo se
  procesa una sola vez; candidato de autodetección inaccesible → el escaneo continúa.
- Ejecución real: `python WoWLogExtractor/WoWLogExtractor.py --log-dir "D:\BattleNet\World of
  Warcraft\_retail_\Logs" --output <tmp>` → genera al menos la M+ "Valle Cegador +10" del
  30-08 y pulls de raid; los logs originales conservan tamaño/mtime; segunda ejecución
  inmediata reporta 0 nuevos.
- Muestreo manual: 1 M+ y 1 raid generados; primera línea ≈10 s antes del START, última ≈10 s
  tras el END; líneas START/END intactas; JSON conforme al esquema.
- `Run WoW Log Extractor.bat` ejecuta y pausa; resumen visible.
