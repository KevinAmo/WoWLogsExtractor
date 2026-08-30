# Checklist de implementación — WoWLogExtractor

- [x] 1. Esqueleto: parseo línea/timestamp, splitter CSV-aware, dificultades, sanitizado
- [x] 2. SegmentTracker (M+, raid, buffer/cola 10 s, incompletos, sesión nueva, END espurio, mismatch IDs, EOF)
- [x] 3. Salida: .partial+rename, .json por esquema, identidad de segmento, carpetas
- [x] 4. StateStore: incremental, head+tail hash, warm-up, orden outputs→estado
- [x] 5. Config: autodetección (registro + escaneo resiliente) + prompt
- [x] 6. CLI: resumen, errores con pausa, --watch con rotación y Ctrl+C
- [x] 7. Run WoW Log Extractor.bat + README.md
- [x] 8. Tests sintéticos + ejecución en verde
- [x] 9. Prueba real (solo lectura) + inspección manual de muestra
