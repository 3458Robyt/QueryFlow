# Arquitectura y frontera del producto

QueryFlow es una capa determinista de control de cambios para activos de
BigQuery Studio. Codex coordina la conversación y edita la tarea; QueryFlow
decide qué recurso canónico se puede leer, qué política aplica, cómo se calcula
el digest y cuándo una publicación puede llegar a GCP.

Además, QueryFlow ofrece una evaluación FinOps/salud cloud bajo demanda. Esta
segunda ruta no reemplaza la anterior ni convierte la CLI en un agente de
mutaciones: produce un snapshot verificable para negocio, finanzas y
plataforma.

## Flujo

```text
Catálogo → tarea aislada → edición → diff Web Preview → validación
       → diagnóstico/estado → aprobación de digest → publicación → lectura posterior

Allowlist → Asset Inventory/Recommender → agregados Workbench opcionales
         → contexto empresarial → hallazgos deterministas → informe ejecutivo/técnico
         → plan gobernado de solo lectura
```

- Cloud Shell aloja la CLI, el workspace Git local, la auditoría y el Web
  Preview.
- Workbench es el límite remoto para dry-run y muestras acotadas.
- Dataform/BigQuery son adaptadores de lectura y publicación; ningún modelo
  generativo está embebido en la CLI.
- El Web Preview es una proyección de solo lectura. No tiene endpoints de
  escritura ni botones que salten la aprobación conversacional.
- La evaluación FinOps limita el alcance a `finops_projects` o a la unión
  explícita de las allowlists existentes. `--projects` solo puede reducirlo.
- Cloud Asset Inventory y Recommender se consultan con conjuntos allowlisted.
  Las consultas de `INFORMATION_SCHEMA` y Billing Export son plantillas
  agregadas fijas ejecutadas en Workbench, con dry-run, máximo de bytes y
  salida acotada.
- Los artefactos FinOps retienen IDs de recursos, métricas agregadas,
  contexto resuelto, estado de fuentes y hashes. No retienen filas crudas,
  respuestas completas de proveedores, SQL remoto ni el mapa empresarial
  completo.

## Qué no duplica

[Gemini Enterprise](https://docs.cloud.google.com/gemini/enterprise/docs) está
orientado a búsqueda empresarial, conectores, asistentes y agentes
centralizados. [Gemini en BigQuery](https://docs.cloud.google.com/bigquery/docs/write-sql-gemini)
ayuda a generar, explicar y corregir SQL. QueryFlow no implementa esas
capacidades: administra el ciclo de vida, el diff, el control de concurrencia,
las allowlists, los digests, la auditoría y la lectura posterior de cambios
reales.

Las Shared Queries siguen siendo activos de BigQuery Studio/Dataform; QueryFlow
no crea un repositorio paralelo ni reemplaza el historial remoto.

## Fronteras de seguridad

Las excepciones estáticas están separadas del digest normal y desactivadas por
defecto. Los diagnósticos se redactan antes de almacenarse y no incluyen
tokens, filas ni SQL completo. La reescritura de rutas y el diccionario de
migración no pertenecen a este producto. Los hallazgos FinOps son siempre
`plan_only`; cualquier futura escritura debe pasar por una aprobación y una
política separadas.
