# Ejemplo de `diagnostic_packet.json`

`diagnostic_packet_example.json` es el packet de diagnóstico que escribe la herramienta en
`Diagnostics/` para una sesión de raid, tal cual: JSON compacto en una sola línea, UTF-8, sin
salto de línea final. No está reformateado; si quieres leerlo cómodamente, ábrelo con un visor
de JSON o pásalo por `python -m json.tool`.

## De dónde sale

Lo genera el test end-to-end `DiagnosticEndToEndTests` de `tests/test_extractor.py` a partir
de una sesión **sintética** (no de una raid real): cabecera `COMBAT_LOG_VERSION` con build
`12.1.0`, dos bosses y cinco pulls (dos kills, tres wipes, uno de 18 s más corto que
`MIN_COMPARABLE_SECONDS` y uno en el que el jugador muere a mitad del combate), un mago Arcano
(spec 62) como jugador seleccionado y algo de ruido (otro jugador y un NPC aliado). El fixture
usa marcas de tiempo y cantidades fijas, así que el packet es estable byte a byte.

Sus cifras solo ilustran el formato: no describen el rendimiento de nadie ni sirven como
referencia de daño, casts o cargas.

## Cómo regenerarlo

El test compara el packet generado con este fichero byte a byte y falla si difieren. Si el
cambio es intencionado (por ejemplo, un cambio de formato o de `APP_VERSION`), regenera el
fichero desde la raíz del repo con la variable de entorno `WLE_UPDATE_EXAMPLES=1`:

```text
# Git Bash
WLE_UPDATE_EXAMPLES=1 python -m unittest discover -s WoWLogExtractor/tests -k DiagnosticEndToEndTests

# PowerShell
$env:WLE_UPDATE_EXAMPLES = "1"; python -m unittest discover -s WoWLogExtractor/tests -k DiagnosticEndToEndTests; Remove-Item Env:WLE_UPDATE_EXAMPLES
```

Revisa el diff del fichero antes de hacer commit.
