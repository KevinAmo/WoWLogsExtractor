# WoWLogsExtractor

Herramienta local para Windows que extrae runs de Mythic+ y pulls de raid de los combat
logs de WoW Retail. Ver `plans/wow-log-extractor.md` para el diseño completo.

## Convenciones

- `WoWLogExtractor/WoWLogExtractor.py` es un único archivo **solo stdlib** (Python 3.10+).
  No añadir dependencias externas: debe ejecutarse con doble clic en cualquier equipo.
- Tests: `python -m unittest discover -s WoWLogExtractor/tests` desde la raíz del repo.
  Un test de regresión debe fallar sin su corrección; comprobarlo antes de darlo por bueno.
- Los combat logs reales (`D:\BattleNet\World of Warcraft\_retail_\Logs` en esta máquina)
  son **solo lectura**: nunca modificarlos, moverlos ni borrarlos.
- Los segmentos se copian byte a byte: no filtrar ni reescribir líneas del log.
- Al reanudar por offset guardado, validar la huella (hash) del prefijo ya consumido, no
  solo el tamaño: un log reemplazado puede ser más grande que el offset anterior.
- Todos los perfiles de salida (`full`, `--analysis*`, `--gzip`, `--bundle`, `--keep-player-damage`,
  `--performance-player`) publican sobre los mismos nombres (`<basename>.txt`, `<basename>/analysis/`).
  Cualquier cambio en la publicación o en `StateStore` debe probarse con un crash a mitad de una
  republicación bajo otro perfil y con la vuelta al perfil anterior: dos revisiones independientes han
  encontrado bugs ahí. `Diagnostics/` es un dato derivado que se reconstruye desde los `performance.json`
  publicados, así que un cambio en el packet o en su reconstrucción debe probarse con un paquete sin su
  marcador al que apunta un packet existente (el packet debe quedar retenido, nunca borrado ni reducido),
  con un packet ausente y sin datos de log nuevos, y con un fallo de escritura del packet.
- Un error de lectura nunca equivale a «no existe»: solo `FileNotFoundError` significa ausencia
  (`os.path.isdir`/`exists` se tragan el resto). Borrar o reducir datos derivados exige una lectura
  completa y correcta, y nunca en una ejecución con errores de proceso. En `--watch`, un worker que
  lanzó una excepción no se reutiliza (se recrea desde el offset confirmado), y el que estaba en
  curso al llegar un Ctrl+C publica lo ya terminado pero no confirma su offset: una interrupción
  puede caer entre dos instrucciones cualesquiera, así que no se intenta decidir si quedó «sano».
  Publicar es idempotente; confirmar un offset no. `--watch` nunca reconstruye `Diagnostics/` (ni
  crea, reescribe o borra packets; solo la limpieza de arranque retira `.tmp` propios).
