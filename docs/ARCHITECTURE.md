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

## Migraciones de rutas

El flujo oficial es `migration batch`: recibe una selección explícita, resuelve
los recursos contra el catálogo, exporta solo código, crea tareas aisladas y
calcula un digest global. Cada tarea reutiliza el diff rojo/verde del Web
Preview y el lote produce un informe Markdown/JSON con archivo, celda y línea
de cada ruta no cubierta. En `migration-batch`, las rutas desconocidas, SQL
dinámico, mutante o no clasificable son incidencias de revisión humana: se
copian como código, se etiquetan `queryflow_review=required` y
`queryflow_state=pending`, y no se ejecutan. Una colisión, carrera del origen,
secreto probable, contenido vacío o fallo de lectura posterior es un bloqueo.
`run` solo crea copias nuevas después de la aprobación; `resume` conserva el
digest y reintenta pendientes.

El piloto 10+10 vive detrás de comandos separados (`migration` y `pilot`) como
compatibilidad temporal.

## Procedimientos almacenados

`migration routines` es un adaptador independiente de Dataform: lee los
recursos `Routine` de BigQuery por REST, conserva una instantánea con hashes y
construye una propuesta en el dataset destino `functions`. Primero normaliza
las llamadas a dependencias inventariadas y después aplica el diccionario de
rutas de tablas. El manifiesto ordena dependencias, divide lotes y fija un
digest; la publicación solo usa `routines.insert` sobre nombres inexistentes y
comprueba la lectura posterior. No hay jobs SQL, `CALL`, dry-run, actualización,
eliminación ni cambios de IAM. Para perímetros VPC, el mismo REST se puede
transportar dentro de un kernel efímero de Workbench; el backend `auto` solo
lo selecciona después de una lectura directa fallida por perímetro.

## Piloto de migración opcional (legado)

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
tokens, filas ni SQL completo. La reescritura de rutas y el diccionario de
migración viven en el piloto opcional, no en el flujo normal del producto; el
diccionario continúa siendo un artefacto privado fuera del repositorio y el
adaptador de campaña solo valida su hash y aplica reemplazos locales explícitos.
Los hallazgos FinOps son siempre `plan_only`; cualquier futura escritura debe
pasar por una aprobación y una política separadas.
