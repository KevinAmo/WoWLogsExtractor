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
- Todos los perfiles de salida (`full`, `--analysis*`, `--gzip`, `--bundle`, `--keep-player-damage`)
  publican sobre los mismos nombres (`<basename>.txt`, `<basename>/analysis/`). Cualquier cambio en
  la publicación o en `StateStore` debe probarse con un crash a mitad de una republicación bajo otro
  perfil y con la vuelta al perfil anterior: dos revisiones independientes han encontrado bugs ahí.
