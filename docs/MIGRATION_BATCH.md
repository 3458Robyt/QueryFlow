# Lote de migración de QueryFlow

`queryflow migration batch` es el flujo oficial para migrar una lista explícita
de Shared Queries y notebooks. En `operation=copy` crea una copia en otro
proyecto; en `operation=update` edita el repositorio actual en el proyecto
destino, conservando su nombre, región e historial. En ambos casos reescribe
las rutas con el diccionario privado y deja un informe de rutas no cubiertas.
Admite orígenes en varias regiones y no ejecuta SQL ni dry-run.

El perfil `migration-batch` tiene una excepción acotada: permite copiar código
que requiere revisión humana (rutas no cubiertas, SQL dinámico, SQL mutante o
SQL no clasificable). Esa excepción no habilita ejecución, dry-run ni tablas;
los recursos quedan identificados como `queryflow_review=required`
y `queryflow_state=pending`.

El diccionario JSON privado es la transcripción estructurada de la imagen de
rutas aprobada; su versión Markdown (`routes.md`) sirve para revisión humana y
se genera con `migration dictionary render`.

## Flujo recomendado

1. Configura el perfil y el contexto una sola vez:

   ```bash
   queryflow init --profile migration-batch \
     --account analyst@example.com \
     --source-projects SOURCE_PROJECT \
     --destination-projects DESTINATION_PROJECT \
     --workbench-instance-project WORKBENCH_INSTANCE_PROJECT \
     --workbench-instance-location us-east1-b \
     --workbench-instance-name WORKBENCH_INSTANCE_NAME \
     --workbench-job-project WORKBENCH_JOB_PROJECT
   ```

2. Actualiza el catálogo (operación de solo lectura) y revisa los nombres:

   ```bash
   queryflow catalog refresh --account analyst@example.com --projects \
     SOURCE_PROJECT DESTINATION_PROJECT --json
   queryflow catalog search "cotizador" --kind shared_query
   ```

3. Valida el diccionario privado y crea el inventario. El cliente Dataform se
   limita a 180 solicitudes/minuto frente a una cuota de 300 en `us-east1`.

   ```bash
   queryflow migration dictionary validate --dictionary PRIVATE/routes.json --json
   queryflow migration dictionary render --dictionary PRIVATE/routes.json \
     --output PRIVATE/routes.md --json
   queryflow migration batch inventory \
     --selection-file examples/analytics-migration-batch.selection.json \
     --dictionary PRIVATE/routes.json --catalog ~/.queryflow/catalog.json \
     --account analyst@example.com --expected-shared-queries 29 \
     --expected-notebooks 7 --json
   ```

   El inventario guarda solo identificadores, hashes, mappings, clasificación
   estática, celdas y advertencias. Si una ruta no está cubierta, se conserva
   sin adivinarla y se registra con archivo, celda y línea cuando es posible.

4. Prepara las tareas aisladas y abre el Web Preview consolidado:

   ```bash
   queryflow migration batch prepare --manifest PRIVATE/manifest.json \
     --dictionary PRIVATE/routes.json --account analyst@example.com --json
   queryflow migration batch review --manifest PRIVATE/manifest.json --serve
   ```

   Cada enlace abre el diff rojo/verde del recurso. El informe está en
   `migration-report.md` (lectura humana) y `migration-report.json` (auditoría).
   El resumen indica qué recursos requieren revisión y por qué; todos aparecen
   como no ejecutables.

5. Presenta el `publication_digest`. Solo después de una aprobación explícita
   se crean copias nuevas:

   ```bash
   queryflow permissions use migration-batch
   queryflow migration batch run --manifest PRIVATE/manifest.json \
     --dictionary PRIVATE/routes.json --execute-migration \
     --approved-digest PUBLICATION_DIGEST --account analyst@example.com --json
   ```

   El comando comprueba de nuevo cada origen, no sobrescribe repositorios con
   el mismo nombre visible, archiva la tarea y verifica el contenido remoto con
   un read-back. Un error de autenticación, VPC, transporte, colisión o cambio
   del origen detiene la campaña. Un fallo aislado deja el lote en `partial` y
   se reanuda con el mismo digest:

   ```bash
   queryflow migration batch resume --manifest PRIVATE/manifest.json \
     --dictionary PRIVATE/routes.json --execute-migration \
     --approved-digest PUBLICATION_DIGEST --account analyst@example.com --json
   ```

   Si una colisión u otra decisión operativa deja un recurso explícitamente en
   estado `pending`, puedes publicar los demás sin sobrescribirlo usando
   `--skip-pending`. El recurso pendiente queda registrado en el manifest y en
   el informe para retomarlo después con el mismo digest.

### Actualización en sitio

Para corregir rutas de notebooks ya existentes en Analytics, crea una selección
con `operation: "update"` y el mismo `source_project` y
`destination_project`. El `name` de cada recurso debe ser su referencia
canónica actual (no el nombre visible). El inventario exporta el repositorio
actual y lo usa como baseline; no crea un repositorio alterno ni cambia el
nombre visible. Antes de publicar vuelve a comprobar el `head_commit` y el
hash del contenido. Si otra persona editó el notebook desde el inventario, el
lote se detiene para evitar perder trabajo.

Ejemplo mínimo:

```json
{
  "schema_version": 1,
  "campaign_id": "notebook-routes-update-2026-09",
  "operation": "update",
  "source_project": "ANALYTICS_PROJECT",
  "destination_project": "ANALYTICS_PROJECT",
  "source_location": "us-east1",
  "destination_location": "us-east1",
  "resources": [
    {
      "kind": "notebook",
      "name": "projects/ANALYTICS_PROJECT/locations/us-east1/repositories/REPOSITORY_ID",
      "display_name": "Nombre visible del notebook"
    }
  ]
}
```

La preparación exige `mode=team` o `mode=full-access` y
`allow_update_existing=true`:

```bash
queryflow permissions use full-access
queryflow migration batch inventory --selection-file update.selection.json \
  --dictionary PRIVATE/routes.json --catalog ~/.queryflow/catalog.json \
  --output PRIVATE/update/manifest.json --account analyst@example.com --json
queryflow migration batch prepare --manifest PRIVATE/update/manifest.json \
  --dictionary PRIVATE/routes.json --account analyst@example.com --json
queryflow migration batch review --manifest PRIVATE/update/manifest.json --serve
```

Después de revisar el Web Preview y aprobar el digest exacto, publica en sitio:

```bash
queryflow migration batch run --manifest PRIVATE/update/manifest.json \
  --dictionary PRIVATE/routes.json --execute-migration \
  --approved-digest PUBLICATION_DIGEST --account analyst@example.com --json
```

Los estados `ready_to_update` y `already_compliant` distinguen,
respectivamente, un notebook que necesita cambios y uno que ya no tiene rutas
antiguas. `already_compliant` no genera commit. El modo update nunca admite
destinos cruzados, colisiones o una escritura cuyo `head_commit` haya cambiado.

## Qué se copia y qué se bloquea

Se aceptan para copia, siempre con revisión humana posterior:

- `unknown_route`: una referencia no aparece en el diccionario; permanece sin cambios.
- `dynamic_sql`: la consulta se construye en tiempo de ejecución; se marca la celda.
- `mutating`: DML/DDL, procedimientos, transacciones u otra operación mutante.
- `unknown`: el analizador no puede demostrar que sea lectura.

Se bloquean antes de publicar: secretos embebidos, contenido vacío, notebook o
JSON malformado, colisiones en destino, duplicados/ambigüedades, cambio del
origen, errores de autenticación/VPC/transporte, fallo de auditoría o un
read-back inconsistente. `publish_incidents=false` en la selección vuelve a
bloquear también las cuatro categorías de revisión y conserva el modo estricto.

### Copia sellada de notebooks con secretos

El modo normal (`secret_handling=block`) nunca publica un secreto detectado.
Cuando existe una autorización interna para conservar un notebook legado tal
cual, la selección puede declarar explícitamente `secret_handling=sealed_copy`.
La preparación genera una tarea y un Web Preview con `[REDACTED]`; el valor
real no queda en el diff, informe, logs ni auditoría. La publicación sellada
requiere dos aprobaciones independientes: el `publication_digest` del lote y
el `sealed_publication_digest`, además de una referencia auditable (ticket o
control de seguridad):

```bash
queryflow migration batch run --manifest PRIVATE/manifest.json \
  --dictionary PRIVATE/routes.json --execute-migration \
  --approved-digest PUBLICATION_DIGEST \
  --approved-sealed-digest SEALED_PUBLICATION_DIGEST \
  --security-reference SEC-1234 --account analyst@example.com --json
```

La copia sellada se recalcula desde el origen verificado en memoria y se lee
de vuelta byte a byte. Una colisión de destino, cambio del origen, secreto
adicional o cualquier otro bloqueo sigue deteniendo el lote.

La clasificación y la decisión de revisión forman parte del
`publication_digest`; cambiar una etiqueta, razón o clase invalida la
aprobación. Las etiquetas son metadatos para organizar la revisión, no un
bloqueo técnico de BigQuery Studio: el equipo humano debe revisar antes de
ejecutar.

## Selección y nombres

La selección es un contrato explícito: `kind` debe ser `shared_query` o
`notebook` y `display_name` es el nombre que verá el analista en el destino.
La resolución usa el catálogo canónico y tolera únicamente diferencias de
mayúsculas, acentos y los marcadores `*` que algunos inventarios muestran; no
elige coincidencias parciales. El archivo de ejemplo es deliberadamente
pequeño; para campañas completas usa una selección generada desde un catálogo
fresco.

El ID interno del repositorio se deriva del nombre de forma segura para
Dataform (minúsculas, guiones, sin guion bajo). Si el ID ya existe, se añade un
hash corto; el nombre visible nunca se cambia. Si el nombre visible ya existe
en el destino, la campaña se bloquea para evitar sobrescrituras.

## Lotes y recuperación

Para campañas grandes, genera lotes deterministas de hasta 25 recursos (y de
hasta 5 para selecciones selladas). El número de lotes depende del catálogo y
de la región. Cada lote tiene su propio inventario, preview, informe y digest;
se pueden aprobar y publicar serialmente sin perder la trazabilidad del
conjunto. En la campaña multirregional de notebooks preparada el 7 de
septiembre de 2026 se generaron 8 lotes normales y 1 lote `sealed_copy`.

Para un informe único y exhaustivo de toda la campaña (una entrada por recurso,
rutas aplicadas con ocurrencias, rutas no encontradas con archivo/celda/línea,
estado, hashes, destino, auditoría y enlaces al diff), ejecuta:

```bash
python3 scripts/build_migration_detail_report.py \
  --campaign-root PRIVATE/notebook-campaign \
  --expected-count EXPECTED_COUNT --campaign-prefix CAMPAIGN_PREFIX --json
```

Genera `campaign-detail-report.html` como entrega principal para los analistas,
además de `campaign-detail-report.md` para lectura lineal y
`campaign-detail-report.json` para búsquedas o controles automatizados. El HTML
es un archivo único, oscuro y de solo lectura, con búsqueda, filtros, detalle por
recurso, rutas no cubiertas y diff rojo/verde embebido. Se abre localmente sin
servidor ni dependencias. Los tres formatos omiten rutas locales de la máquina y
conservan referencias relativas por lote. No copia filas ni resultados de
BigQuery; el HTML contiene el código de cambios normales. Los recursos
`sealed_copy` aparecen sin código y apuntan al preview redactado. Comparte los
artefactos solo por canales internos. `--json` solo controla el resumen impreso
en consola; el archivo JSON completo siempre se escribe en disco.

Si ya existe un inventario privado de una campaña anterior, los ayudantes del
repositorio generan las selecciones sin volver a leer GCP:

```bash
python3 scripts/prepare_remaining_migration.py \
  --inventory-root INVENTORY_ROOT \
  --output-root CAMPAIGN_ROOT \
  --batch-size 25 --expected-count EXPECTED_COUNT
python3 scripts/summarize_migration_campaign.py \
  --campaign-root CAMPAIGN_ROOT --expected-count EXPECTED_COUNT
```

El primer comando incluye únicamente rutas no cubiertas, SQL dinámico sin
secreto, SQL mutante y SQL no clasificable; excluye secretos y genera una
selección por lote. El segundo valida todos los manifests, comprueba sus
digests y produce el informe agregado sin guardar filas ni SQL completo.

Para preparar la campaña completa de notebooks en todas las regiones del
catálogo, usa el generador de selecciones. Excluye nombres vacíos, resuelve un
duplicado solo cuando hay un commit más reciente inequívoco y separa las
regiones para que cada lote conserve su origen canónico:

```bash
python3 scripts/prepare_notebook_migration.py \
  --catalog ~/.queryflow/catalog.json \
  --output-root PRIVATE/notebook-campaign \
  --source-project SOURCE_PROJECT --destination-project DESTINATION_PROJECT \
  --source-locations us-east1,us-central1,us-west1,northamerica-northeast1 \
  --destination-location us-east1 \
  --sealed-name "Geocoding API" --json
```

El resultado incluye `notebook-campaign.json` y `notebook-campaign.md`, una
selección por lote y un registro de exclusiones. Los nombres sellados son
opcionales y deben ser revisados por seguridad; un secreto no declarado se
mantiene bloqueado durante el inventario normal.

- La política de lote fija la operación declarada (`copy` o `update`),
  `no_sql_execution=true` y `dry_run.skipped`.
- Las advertencias aceptadas se conservan en el destino y en el informe; no se
  inventan rutas ni se ejecuta código para resolverlas.
- No se guardan filas, resultados de BigQuery, tokens ni el SQL completo en el
  informe consolidado.
- `queryflow pilot` permanece como alias de compatibilidad deprecado durante
  una versión; no lo uses para nuevos lotes.
