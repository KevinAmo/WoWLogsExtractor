# WoW Log Extractor

## Qué hace

Extrae automáticamente cada **run de Mythic+** y cada **pull de raid** de los combat logs de
World of Warcraft Retail (los archivos `WoWCombatLog*.txt` que genera el propio juego) y los
guarda como archivos `.txt` individuales, uno por run/pull, junto con un `.json` de metadata
(dungeon o boss, nivel de la key, dificultad, duración, si se completó o no, etc.).

Cada `.txt` incluye ~10 segundos de contexto antes del inicio y después del final del
run/pull, además del contenido completo del combate. La herramienta **solo lee** los logs
originales de WoW: nunca los modifica ni los borra.

## Uso rápido

1. Haz doble clic en `Run WoW Log Extractor.bat`.
2. La primera vez, la herramienta intenta detectar sola la carpeta de logs de WoW (registro
   de Windows y rutas habituales de instalación). Si no la encuentra, te pedirá que escribas
   manualmente la ruta de la carpeta `_retail_\Logs` (dentro de tu instalación de World of
   Warcraft, carpeta `_retail_`).
3. Los archivos generados aparecen junto al script, en:
   - `WoWCombatLog Extracted\MPlus\` — runs de Mythic+
   - `WoWCombatLog Extracted\Raids\` — pulls de raid

Al terminar, verás un resumen en la consola (runs, pulls y errores encontrados) antes de que
se cierre la ventana.

## Activar el registro de combate avanzado en WoW

Para que los logs contengan toda la información necesaria (nombres de dungeon, key level,
afijos, encounter IDs, etc.) hace falta activar el **registro de combate avanzado**
(Advanced Combat Logging):

1. En el juego, abre **Opciones → Sistema** (o **Red / Network**, según la versión del
   cliente) y marca la casilla **"Registro de combate avanzado"** (*Advanced Combat Logging*).
2. Escribe `/combatlog` en el chat para empezar a grabar. WoW guarda el archivo dentro de
   `World of Warcraft\_retail_\Logs\`.

Hay addons (por ejemplo los orientados a M+ o raid) que activan el logging automáticamente al
entrar a una instancia, así no hace falta escribir `/combatlog` cada vez.

## Modo `--watch`

Si quieres que la herramienta vaya generando los archivos **mientras juegas**, sin tener que
volver a ejecutarla después de cada run, usa el modo de vigilancia continua:

```
Run WoW Log Extractor.bat --watch
```

o, desde una consola con Python:

```
python WoWLogExtractor.py --watch
```

En este modo la herramienta queda observando el log activo y va publicando cada `.txt` y
`.json` en cuanto termina cada run de M+ o pull de raid. Para salir, pulsa `Ctrl+C`.

## Cambiar la ruta del log o de salida

- Editando `config.json` (se crea junto al script tras la primera ejecución).
- Ejecutando con `--reconfigure` para que te vuelva a preguntar la ruta.
- Pasando `--log-dir "RUTA"` para usar una ruta puntual sin cambiar la configuración guardada.
- Pasando `--output "RUTA"` para cambiar la carpeta donde se guardan los archivos generados.

## Resetear el estado

La herramienta recuerda hasta dónde ha procesado cada log para no repetir trabajo. Si quieres
forzar un reprocesado completo:

- Ejecuta con `--reset-state`, o
- Borra manualmente el archivo `state.json` dentro de la carpeta de salida.

Volver a ejecutar la herramienta **nunca duplica archivos**: los nombres de los archivos son
estables, así que un run o pull ya extraído simplemente se sobreescribe de forma idéntica.

## Formato de los nombres de archivo

- Mythic+: `2026-08-30_10-25_MPlus_Valle-Cegador_+10.txt`
- Raid: `2026-08-30_21-19_Raid_Boss_Heroic_Kill.txt`

Si un run o pull queda a medias (por ejemplo, cierras el juego o se corta el log antes de que
termine el combate), el archivo se marca añadiendo `_INCOMPLETE` al nombre, y en el `.json`
correspondiente el campo `complete` queda en `false`.

## Requisitos

- Windows
- Python 3.10 o superior
- Sin dependencias externas (usa solo la biblioteca estándar de Python)
