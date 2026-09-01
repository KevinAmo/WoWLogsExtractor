# WoWLogsExtractor

Herramienta pequeña para separar los combat logs de WoW Retail en runs de Mythic+ y pulls de raid. Conserva el formato de salida legacy por defecto y puede generar paquetes de análisis más pequeños y estructurados, sin dependencias externas: Python 3.10+ y Windows.

## Uso

Desde la carpeta `WoWLogExtractor`, ejecuta el `.bat` o llama al script con Python. Estos son los modos principales:

```text
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

Para el análisis normal, comparte el directorio `analysis/` o usa `--bundle` para crear su ZIP. Reserva la salida completa para depuración excepcional o para herramientas que necesiten el log sin filtrar.

La guía completa, el layout de archivos y el significado de los JSON están en [WoWLogExtractor/README.md](WoWLogExtractor/README.md).
