# Referencia de comandos

Esta referencia resume la interfaz pública de QueryFlow. Para ver todas las
opciones de una versión instalada, añade `--help`. Usa `--json` cuando la
salida vaya a ser procesada por Codex u otra herramienta.

## Tabla rápida

| Comando | Propósito | Red | Escritura remota |
| --- | --- | ---: | ---: |
| `queryflow version` | Ver versión de CLI, configuración y plugin. | no | no |
| `queryflow init` | Crear o actualizar un perfil TOML sin secretos. | no | no |
| `queryflow config` | Leer/validar preferencias locales. | no | no |
| `queryflow context` | Seleccionar proyectos origen/destino y alias. | no | no |
| `queryflow permissions` | Cambiar el perfil de permisos local. | no | no |
| `queryflow policy` | Mostrar o probar filtros de seguridad. | no | no |
| `queryflow install` | Instalar CLI y plugin desde GitHub. | GitHub | no |
| `queryflow self-update` | Actualizar CLI y plugin desde una referencia. | GitHub | no |
| `queryflow doctor` | Diagnosticar binarios y configuración. | no | no |
| `queryflow status` | Mostrar el estado seguro de una tarea. | no | no |
| `queryflow diagnose` | Exportar el diagnóstico seguro de una tarea. | no | no |
| `queryflow exception prepare` | Preparar una excepción estática controlada. | no | no |
| `queryflow catalog refresh` | Actualizar inventario de recursos. | sí | no |
| `queryflow catalog search` | Buscar recursos en el catálogo local. | no | no |
| `queryflow catalog show` | Mostrar un recurso canónico. | no | no |
| `queryflow profile` | Obtener estadísticas agregadas de una tabla. | opcional | no |
| `queryflow finops assess` | Crear un snapshot FinOps/salud cloud. | sí | no |
| `queryflow finops show` | Verificar y mostrar un assessment. | no | no |
| `queryflow finops review` | Servir el informe FinOps de solo lectura. | no | no |
| `queryflow start` | Crear una tarea aislada. | opcional | no |
| `queryflow validate` | Clasificar y validar SQL/notebooks. | opcional | no |
| `queryflow review` | Crear o servir el diff Web Preview. | no | no |
| `queryflow sample` | Ejecutar una muestra aprobada dentro de Workbench. | sí | no |
| `queryflow publish` | Crear una copia aprobada o actualizar en modo team. | sí | sí |
| `queryflow migration dictionary` | Validar/renderizar el diccionario privado. | no | no |
| `queryflow migration rewrite` | Planificar/aplicar rutas en una tarea local. | no | no |
| `queryflow migration batch inventory` | Resolver y exportar una selección explícita sin escribir. | lectura | no |
| `queryflow migration batch prepare` | Crear tareas, reescrituras y diffs sin publicar. | lectura | no |
| `queryflow migration batch review` | Servir el Web Preview consolidado del lote. | no | no |
| `queryflow migration batch run` | Crear copias o actualizar notebooks en sitio con un digest aprobado. | sí | sí (según `operation`) |
| `queryflow migration batch resume` | Reanudar únicamente recursos pendientes del mismo tipo de operación. | sí | sí (según `operation`) |
| `scripts/build_migration_detail_report.py` | Generar el informe HTML autocontenido y los respaldos JSON/Markdown portátiles de todos los lotes, rutas y recursos. | no | no |
| `queryflow migration routines inventory` | Inventariar rutinas/procedimientos y accesos observados sin jobs SQL. | lectura | no |
| `queryflow migration routines prepare` | Refrescar propuestas, dependencias, hashes y diffs locales. | lectura | no |
| `queryflow migration routines review` | Crear o servir el Web Preview oscuro de procedimientos. | no | no |
| `queryflow migration routines run` | Insertar rutinas nuevas en `DESTINO.functions` con digest aprobado. | sí | sí (solo nuevas) |
| `queryflow migration routines resume` | Reanudar el mismo manifiesto/digest de rutinas. | sí | sí (solo nuevas) |
| `queryflow migration routines report` | Regenerar reportes sin contactar GCP. | no | no |
| `queryflow pilot inventory` | Seleccionar la muestra 10+10 sin escribir. | lectura | no |
| `queryflow pilot prepare` | Crear tareas y Web Previews locales sin publicar. | lectura | no |
| `queryflow pilot run` | Ejecutar el piloto y crear copias con autorización explícita. | sí | sí (solo copias) |
| `queryflow pilot review` | Revisar el resumen batch en HTML. | no | no |
| `queryflow pilot cleanup-plan` | Crear digest del conjunto de copias. | no | no |
| `queryflow pilot cleanup` | Limpiar copias exactas con digest aprobado. | sí | sí (borrado acotado) |

## Instalación y configuración

### `queryflow version`

Muestra versiones. No requiere configuración:

```bash
queryflow version --json
```

### `queryflow init`

Crea un perfil local. Opciones principales: `--profile pilot|team|full-access|migration-pilot|migration-batch`,
`--account`, `--gcloud-config-dir`, los proyectos y alias, los cuatro campos
explícitos de Workbench (`--workbench-instance-project`,
`--workbench-instance-location`, `--workbench-instance-name` y
`--workbench-job-project`), `--validation-backend` y `--max-bytes`.

```bash
queryflow init --profile pilot \
  --source-projects source-project \
  --destination-projects destination-project \
  --workbench-instance-project workbench-instance-project \
  --workbench-instance-location us-east1-b \
  --workbench-instance-name workbench-instance \
  --workbench-job-project workbench-project --json
```

### `queryflow config`

Usa `--path PATH` para operar sobre otro TOML:

```bash
queryflow config path --json
queryflow config list --json
queryflow config get profiles.pilot.mode --json
queryflow config set preferences.review_mode unified --json
queryflow config validate --json
```

No guardes tokens, contraseñas, claves privadas o valores de autorización.

### `queryflow context` y `queryflow permissions`

El contexto permite modelar migraciones sin repetir IDs y se copia al
`manifest.json` de cada tarea:

```bash
queryflow context alias set replication replication-project
queryflow context alias set analytics analytics-project
queryflow context set --source replication --destination analytics
queryflow context show --json
```

Usa `permissions show` para ver el perfil activo y cambia entre `pilot`,
`team`, `full-access`, `migration-pilot` y `migration-batch` con `permissions use`. El perfil `full-access` no
ejecuta SQL ni elimina recursos; únicamente habilita una publicación explícita
de notebooks o Shared Queries sin digest cuando el analista proporciona un
motivo auditable:

```bash
queryflow permissions use full-access
queryflow publish --task TASK --force-publish \
  --reason "Aprobación explícita del analista" \
  --account analyst@example.com --config ~/.config/queryflow/config.toml --json
```

La publicación force todavía exige destino permitido, control de conflicto,
auditoría, `force-authorization.json` y lectura remota de comprobación.

`migration-pilot` es un perfil separado para la campaña 10+10. Solo acepta el
manifest generado por `pilot inventory` y el interruptor explícito
`--execute-migration` más el digest aprobado; crea copias nuevas y no ejecuta SQL. Consulta
[MIGRATION_PILOT.md](MIGRATION_PILOT.md) para el flujo completo.

`migration-batch` es el flujo oficial para selecciones explícitas. Consulta
[MIGRATION_BATCH.md](MIGRATION_BATCH.md): no ejecuta SQL/dry-run, conserva los
nombres visibles, acepta advertencias de rutas no cubiertas y requiere un
digest global antes de crear copias o actualizaciones. Las actualizaciones
requieren `operation=update`, el mismo proyecto en origen/destino y
`allow_update_existing=true` en modo `team` o `full-access`.

`migration routines` es el flujo copy-only para procedimientos almacenados y
dependencias. Consulta [MIGRATION_ROUTINES.md](MIGRATION_ROUTINES.md): usa la
API REST de BigQuery o Workbench como gateway, no crea jobs SQL, consolida las
rutinas en `functions`, registra ACL observadas sin modificar IAM y exige un
digest exacto antes de insertar. Los lotes se limitan localmente a 120
peticiones/minuto (máximo configurable: 300).

### `queryflow finops`

La evaluación requiere una allowlist FinOps en el perfil (`finops_projects`) o
reutiliza los proyectos origen/destino. `--projects` nunca puede ampliarla.
El perfil puede declarar opcionalmente `billing_export_table`,
`business_context_path` y `finops_window_days`.

```bash
queryflow finops assess --config ~/.config/queryflow/config.toml \
  --projects FINOPS_PROJECT --window-days 30 \
  --billing-table BILLING_PROJECT.DATASET.TABLE \
  --business-context business-context.toml --json
queryflow finops show --assessment ASSESSMENT_ID --json
queryflow finops review --assessment ASSESSMENT_ID --serve --port 8080
```

`assess` consulta solo Cloud Asset Inventory, los recommenders revisados y
agregados fijos de BigQuery/Billing Export dentro de Workbench. Devuelve la
ubicación de los artefactos y un informe con estado `complete`, `partial` o
`failed`. `show` y `review` vuelven a calcular hashes y rechazan un snapshot
alterado. El servidor Web Preview solo acepta `GET`; todos los planes son
`plan_only`.

### `queryflow policy`

```bash
queryflow policy show --config ~/.config/queryflow/config.toml --json
queryflow policy check --config ~/.config/queryflow/config.toml \
  --operation publish --resource-kind shared_query --mode copy \
  --source-project source-project --destination-project destination-project \
  --location us --json
```

Una decisión denegada devuelve código de salida `2`; no debe sortearse con
otra combinación de flags.

### `queryflow install` y `queryflow self-update`

Usa `--dry-run` para ver las instrucciones antes de ejecutar cambios locales:

```bash
queryflow install --ref v0.4.0-beta.1 --dry-run --json
queryflow self-update --ref v0.4.0-beta.1 --dry-run --json
```

Ambos comandos usan la misma referencia para el paquete Python y el plugin.
Reinicia Codex después de instalar o actualizar el plugin.

## Catálogo

### `queryflow catalog refresh`

Consulta los recursos permitidos y guarda el catálogo local:

```bash
queryflow catalog refresh --account analyst@example.com --json
queryflow catalog refresh --account analyst@example.com \
  --projects source-project destination-project --json
```

### `queryflow catalog search`

Busca por texto y opcionalmente filtra por `--kind`:

```bash
queryflow catalog search "monthly sales" --kind shared_query
queryflow catalog search "claims" --kind notebook
```

### `queryflow catalog show`

Muestra el registro completo del nombre canónico:

```bash
queryflow catalog show CANONICAL_RESOURCE
```

## Tareas y validación

### `queryflow start`

Para un recurso existente usa `--resource`, `--account` y el destino. Para
crear contenido local usa `--mode new`, `--kind`, `--name`, `--project`,
`--location` y opcionalmente `--content-file`. `--open-editor` abre el archivo
de la tarea en Cloud Shell Editor.

```bash
queryflow start --resource CANONICAL_RESOURCE \
  --account analyst@example.com --destination-project destination-project --json
queryflow start --mode new --kind shared_query --name monthly_sales \
  --project destination-project --location us --content-file query.sql --json
```

El modo `update` requiere el recurso canónico, cuenta, perfil team y controles
de concurrencia. El piloto no usa `update`.

### `queryflow validate`

Valida la tarea. `--static-only` no consulta servicios ni crea un digest
publicable. `--backend workbench` usa la instancia configurada. `--max-bytes`
solo puede reducir el límite. `--execute-read-only` requiere además
`--confirm-execution`, y nunca habilita SQL mutante.

```bash
queryflow validate --task TASK --backend local --static-only --json
queryflow validate --task TASK --account analyst@example.com \
  --config ~/.config/queryflow/config.toml --json
```

### `queryflow review`

Genera el HTML o sirve un Web Preview de solo lectura:

```bash
queryflow review --task TASK --json
queryflow review --task TASK --serve --watch --port 8080
```

La interfaz muestra cambios por archivo/celda, líneas añadidas/eliminadas,
validación, digest y metadatos sin filas.

### `queryflow status`

Resume el estado actual, hashes, validación, digest y diagnóstico de una tarea
sin exponer el contenido SQL:

```bash
queryflow status --task TASK --json
```

Los estados de bloqueo conservan código de salida `2`. El resultado incluye
`status`, `content_sha256`, `validated_sha256`, `approval_digest` y, si existe,
el sobre de diagnóstico seguro.

### `queryflow diagnose`

Lee el último fallo de la tarea. El JSON es el formato para Codex y Markdown es
el formato para enviar al responsable de permisos:

```bash
queryflow diagnose --task TASK --format json
queryflow diagnose --task TASK --format markdown --output diagnostic.md
```

El diagnóstico tiene `error_id`, categoría, etapa, recuperación, si es
reintentable e identificadores de proveedor como
`vpcServiceControlsUniqueIdentifier`. No contiene tokens, filas ni SQL
completo.

## Muestra y publicación

### `queryflow sample`

Requiere `--task`, `--approved-digest`, validación publicable y configuración
Workbench. `--limit` va de 1 a 5 y el valor recomendado es 3. En notebooks
multiconsulta se requiere `--fragment`.

```bash
queryflow sample --task TASK --limit 3 \
  --approved-digest SAMPLE_DIGEST --account analyst@example.com \
  --config ~/.config/queryflow/config.toml --json
```

El resultado transitorio puede contener filas; `sample-receipt.json` solo
contiene digest, conteo, columnas, límite, timestamp, truncación y errores.

### `queryflow publish`

El flujo normal requiere `--task`, el digest completo, `--account`, auditoría
configurada y lectura posterior coincidente. El destino se toma del argumento
o del contexto guardado en la tarea:

```bash
queryflow publish --task TASK --approved-digest DIGEST \
  --destination-project destination-project --account analyst@example.com \
  --config ~/.config/queryflow/config.toml --json
```

En `pilot` crea una copia nueva. La actualización de un recurso existente está
restringida a `team` o `full-access` y no se habilita mediante un flag aislado.

Cuando el analista ha seleccionado `full-access`, puede ordenar una publicación
sin validación/dry-run con autorización explícita:

```bash
queryflow publish --task TASK --force-publish \
  --reason "Aprobación explícita del analista" \
  --account analyst@example.com --config ~/.config/queryflow/config.toml --json
```

Esta ruta solo aplica a notebooks y Shared Queries. Conserva control de
recurso canónico, destino, conflicto de `head`, auditoría y read-back, y deja
`force-authorization.json` junto a la tarea.

### `queryflow exception prepare`

Es una ruta controlada para el perfil `team` cuando la validación estática es
correcta pero el dry-run remoto está bloqueado por una dependencia aprobada.
Debe estar habilitada explícitamente con `allow_static_exception = true`,
requiere razón y referencia/ticket, y genera un digest independiente:

```bash
queryflow exception prepare --task TASK \
  --reason "Perímetro temporalmente no disponible" \
  --reference SEC-1234 --config ~/.config/queryflow/config.toml --json
queryflow publish --task TASK --approved-exception-digest EXCEPTION_DIGEST \
  --destination-project destination-project --account analyst@example.com \
  --config ~/.config/queryflow/config.toml --json
```

La excepción solo guarda el activo de código. No ejecuta SQL, no habilita
programaciones y no puede saltarse sintaxis inválida, política denegada,
conflicto remoto, integridad o recurso inexistente.

## Diagnóstico y perfilado

### `queryflow doctor`

Comprueba Python, Git, gcloud, bq, Cloud Shell, uv, Codex, websockets,
configuración y perfil:

```bash
queryflow doctor --config ~/.config/queryflow/config.toml --json
# Añade --probe-remote para consultar APIs habilitadas y describir Workbench.
queryflow doctor --config ~/.config/queryflow/config.toml --probe-remote --json
```

`--probe-remote` solo ejecuta lecturas (`gcloud services list` y `describe`);
no habilita APIs, no inicia jobs y no cambia recursos. El resultado enumera
APIs requeridas/faltantes y clasifica el error de conectividad sin copiar la
respuesta cruda del proveedor.

### `queryflow profile`

Recibe `--table` y `--schema-file`; acepta `--location`, `--max-bytes`,
`--output` y `--json`. `--execute` requiere `--confirm-profile` y sigue siendo
una operación de lectura agregada:

```bash
queryflow profile --table source-project.dataset.table \
  --schema-file schema.json --location us --json
```

Para diagnóstico de errores, consulta [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Piloto de migración

`queryflow migration dictionary validate` comprueba el esquema y genera un hash
del JSON privado. `render` crea una tabla Markdown para revisión.

`queryflow migration rewrite plan` calcula un digest local de las rutas
conocidas y de los incidentes; `apply` exige ese digest y solo escribe SQL o
celdas de código. No consulta tablas ni ejecuta SQL.

`queryflow pilot inventory` lee el catálogo y el código para clasificar y
seleccionar 10 Shared Queries y 10 notebooks de forma determinista. Omite
colisiones de nombre en el destino y detiene el proceso si no hay cupos. En
catálogos grandes, `--max-resources-per-kind N` limita el pool aleatorio por
tipo; `--request-timeout SECONDS` evita quedar bloqueado por un recurso.
Dataform usa 180 solicitudes/minuto por defecto (la cuota del flujo es 300 en
`us-east1`), reintenta `429` solo en lecturas y deja un
`inventory-checkpoint.json` para `--resume-inventory`.

`queryflow pilot prepare` vuelve a leer los recursos, comprueba sus hashes y
crea una tarea y un Web Preview por selección. Es local, no ejecuta SQL y
devuelve el `publication_digest` que debe aprobarse antes de publicar.

`queryflow pilot run` sin `--execute-migration` es solo plan. Para publicar
copias nuevas se requieren el perfil `migration-pilot` y el flag explícito:

```bash
queryflow pilot run --manifest PRIVATE/manifest.json \
  --dictionary PRIVATE/routes.json --execute-migration \
  --approved-digest PUBLICATION_DIGEST --account ACCOUNT --json
```

Las rutas no cubiertas permanecen intactas y quedan en el reporte. La limpieza
separada exige primero `cleanup-plan` y después el digest exacto en `cleanup`;
las copias con `head` cambiado se omiten.

Para nuevos trabajos, `queryflow migration batch` reemplaza al piloto.
`inventory` resuelve únicamente los nombres de `SELECTION.json` contra el
catálogo canónico; `prepare` genera una tarea y diff por recurso;
`review --serve` expone el resumen oscuro; `run`/`resume` exigen
`migration-batch`, `--execute-migration` y el mismo digest. Las rutas
desconocidas, SQL dinámico, SQL mutante y SQL no clasificable se conservan para
revisión humana y quedan en `migration-report.md` con su clasificación. Las
copias se etiquetan `queryflow_review=required` y
`queryflow_state=pending`; nunca se ejecuta SQL ni dry-run. Secretos,
contenido vacío/malformado, colisiones, drift y errores de integridad siguen
siendo bloqueos. `publish_incidents=false` en la selección recupera el modo
estricto para las cuatro categorías de advertencia.
