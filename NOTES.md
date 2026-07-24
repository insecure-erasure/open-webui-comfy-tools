# Notes

## Resolución de imagen — cómo funciona

El parámetro `size` que el LLM pasa al tool **no controla píxeles absolutos**, solo la **relación de aspecto**.

### Flujo

1. El LLM pasa `size` (ej. `"2000x3000"`) o se usa el AdminValve `default_size`
2. El script reduce las dimensiones por MCD → obtiene un ratio (ej. `2:3`)
3. El ratio se inyecta en el nodo `StringConcatenate` (84) como `string_a` y `string_b`
4. `FluxResolutionCalc` (69) combina el ratio con `megapixel: "1.0"` (hardcodeado en el workflow) y calcula la resolución real

### Ejemplos

| size (LLM) | Ratio inyectado | Resolución real (~1.0 MP) |
|---|---|---|
| `2000x3000` | 2:3 | ~816×1224 |
| `1920x1080` | 16:9 | ~1336×752 |
| `768x1152` (default) | 2:3 | ~816×1224 |
| `1024x1024` | 1:1 | ~1024×1024 |

### Para cambiar la resolución base

Modificar `megapixel` en `FluxResolutionCalc` (nodo 69) del workflow. Por ejemplo, `"2.0"` duplicaría los píxeles.
