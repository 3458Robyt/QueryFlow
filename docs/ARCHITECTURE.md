# Arquitectura y frontera del producto

QueryFlow es una capa determinista de control de cambios para activos de
BigQuery Studio. Codex coordina la conversación y edita la tarea; QueryFlow
decide qué recurso canónico se puede leer, qué política aplica, cómo se calcula
el digest y cuándo una publicación puede llegar a GCP.

## Flujo

```text
Catálogo → tarea aislada → edición → diff Web Preview → validación
       → diagnóstico/estado → aprobación de digest → publicación → lectura posterior
```

- Cloud Shell aloja la CLI, el workspace Git local, la auditoría y el Web
  Preview.
- Workbench es el límite remoto para dry-run y muestras acotadas.
- Dataform/BigQuery son adaptadores de lectura y publicación; ningún modelo
  generativo está embebido en la CLI.
- El Web Preview es una proyección de solo lectura. No tiene endpoints de
  escritura ni botones que salten la aprobación conversacional.

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

## Piloto de migración opcional

El piloto 10+10 vive detrás de comandos separados (`migration` y `pilot`). El
catálogo y Dataform aportan únicamente el inventario/código; el diccionario
privado y el reescritor son entradas aisladas y no cambian el flujo normal de
QueryFlow. La campaña conserva hashes, semilla, rutas aplicadas e incidentes,
pero no guarda SQL en la manifest. Solo el perfil `migration-pilot` junto con
`--execute-migration` y un `publication_digest` aprobado puede crear copias
nuevas; no hay `update`, SQL ejecutado ni dry-run implícito. La limpieza exige
un plan y digest propios.

## Fronteras de seguridad

Las excepciones estáticas están separadas del digest normal y desactivadas por
defecto. Los diagnósticos se redactan antes de almacenarse y no incluyen
tokens, filas ni SQL completo. El diccionario de rutas continúa siendo un
artefacto privado fuera del repositorio; el adaptador de campaña solo valida su
hash y aplica reemplazos locales explícitos.
