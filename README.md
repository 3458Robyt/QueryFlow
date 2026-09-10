# QueryFlow

QueryFlow es una CLI de Python y un plugin de Codex para trabajar con SQL,
notebooks y decisiones FinOps en Google Cloud con revisión humana. El flujo
original de edición sigue intacto: cada cambio vive en una tarea aislada, se
valida, se muestra en un diff tipo Pull Request y solo se publica después de
aprobar el digest exacto. El nuevo flujo de evaluación genera snapshots de
salud cloud y oportunidades para negocio, finanzas y plataforma sin escribir
recursos remotos.

## Inicio rápido

### Requisitos

- Cloud Shell o un entorno autorizado por la organización.
- Python 3.11 o superior, `uv`/`uvx`, Git y `gcloud`.
- `gcloud` autenticado con la cuenta que tiene acceso a los proyectos y, para
  validaciones o muestras reales, a la instancia Workbench aprobada.

Comprueba la identidad antes de comenzar:

```bash
gcloud auth list
```

No pegues tokens en el README, en la configuración ni en una conversación.

### Instalación

La beta disponible para el equipo es `v0.4.0-beta.1`:

```bash
uvx --from git+https://github.com/3458Robyt/QueryFlow.git@v0.4.0-beta.1 \
  queryflow install --ref v0.4.0-beta.1
queryflow init --profile pilot
```

Reinicia Codex para que cargue el plugin. Cuando se publique un tag estable,
reemplaza `feat/queryflow-v010` por ese tag en ambos lugares. La instalación
usa GitHub y las herramientas locales autenticadas; no almacena credenciales.

### Configuración inicial

Configura el perfil con los proyectos y la instancia Workbench aprobados. Los
valores siguientes son ejemplos; sustitúyelos por los de tu equipo:

```bash
queryflow init --profile pilot \
  --account analyst@example.com \
  --gcloud-config-dir ~/.config/gcloud \
  --source-projects SOURCE_PROJECT \
  --destination-projects DESTINATION_PROJECT \
  --workbench-instance-project WORKBENCH_INSTANCE_PROJECT \
  --workbench-instance-location us-east1-b \
  --workbench-instance-name WORKBENCH_INSTANCE \
  --workbench-job-project WORKBENCH_JOB_PROJECT \
  --validation-backend workbench \
  --json

queryflow config validate --json
queryflow doctor --json
queryflow policy show --json
```

La revisión usa por defecto tema oscuro, diff unificado y solo cambios. Se
puede personalizar sin tocar credenciales:

```bash
queryflow config set preferences.review_theme dark --json
queryflow config set preferences.review_mode unified --json
```

La configuración se guarda en `~/.config/queryflow/config.toml`. Contiene
preferencias y límites, nunca secretos.

Para habilitar el alcance FinOps, usa `--finops-projects` o conserva los
`source_projects`/`destination_projects` existentes. Un Billing Export y un
mapa empresarial son opcionales:

```bash
queryflow init --profile pilot \
  --finops-projects FINOPS_PROJECT \
  --billing-export-table BILLING_PROJECT.DATASET.TABLE \
  --business-context-path business-context.toml \
  --finops-window-days 30
```

### Contexto y permisos

Guarda alias para no repetir IDs y selecciona un flujo de migración sin tocar
los recursos remotos:

```bash
queryflow context alias set replication REPLICATION_PROJECT
queryflow context alias set analytics ANALYTICS_PROJECT
queryflow context set --source replication --destination analytics
queryflow context show --json
```

Los perfiles disponibles son `pilot` (solo copias), `team` (actualizaciones
aprobadas), `full-access`, `migration-pilot` (compatibilidad) y
`migration-batch` (lotes explícitos).
`full-access` no ejecuta SQL ni elimina recursos;
permite publicar un notebook o Shared Query con una orden explícita cuando el
dry-run no está disponible:

```bash
queryflow permissions use full-access --config ~/.config/queryflow/config.toml
queryflow publish --task TASK --force-publish \
  --reason "Aprobación explícita del analista: incidencia VPC" \
  --account analyst@example.com --config ~/.config/queryflow/config.toml
```

La publicación force exige recurso canónico, destino permitido, control de
conflictos, auditoría y lectura de comprobación. Genera
`force-authorization.json`; el flujo normal continúa usando digest.

### Lote de migración

Para migraciones nuevas usa [Lote de migración](docs/MIGRATION_BATCH.md). La
selección de Analytics está en
[`examples/analytics-migration-batch.selection.json`](examples/analytics-migration-batch.selection.json):

```bash
queryflow permissions use migration-batch
queryflow migration batch inventory \
  --selection-file examples/analytics-migration-batch.selection.json \
  --dictionary PRIVATE/routes.json --catalog ~/.queryflow/catalog.json \
  --account analyst@example.com \
  --expected-shared-queries EXPECTED_SHARED_QUERIES \
  --expected-notebooks EXPECTED_NOTEBOOKS --json
queryflow migration batch prepare --manifest PRIVATE/manifest.json \
  --dictionary PRIVATE/routes.json --account analyst@example.com --json
queryflow migration batch review --manifest PRIVATE/manifest.json --serve
```

El lote conserva nombres visibles, muestra el diff
consolidado y genera `migration-report.md`/`migration-report.json`. En el modo
`operation=copy` crea copias nuevas; para corregir notebooks existentes usa
`operation=update` con el mismo proyecto origen/destino y
`allow_update_existing=true` en modo `team` o `full-access`. El modo update
actualiza el repositorio canónico en sitio después de comprobar su commit y no
crea una segunda copia. En el perfil `migration-batch` se pueden copiar como
código para revisión humana las
rutas no cubiertas, SQL dinámico, mutante o no clasificable. Esas copias llevan
las etiquetas `queryflow_review=required` y `queryflow_state=pending`; no son
ejecutables desde QueryFlow. Secretos, contenido vacío/malformado, conflictos,
drift y fallos de integridad siguen bloqueados. Publicar requiere una
aprobación única del `publication_digest`; nunca se ejecuta SQL ni dry-run.

Cada campaña se divide en lotes de hasta 25 recursos (y hasta 5 para copias
selladas). Cada lote tiene su propia selección, manifest, preview y digest; los
artefactos privados quedan fuera del repositorio para no exponer código ni
credenciales. En la campaña multirregional de notebooks preparada actualmente
son 119 recursos con nombre en 9 lotes (8 normales y 1 sellado); 54 notebooks
sin nombre y 6 duplicados quedaron registrados como excluidos o reemplazados.

Para generar un reporte único con el detalle de todos los recursos de una
campaña y de cada ruta reemplazada o no encontrada:

```bash
python3 scripts/build_migration_detail_report.py \
  --campaign-root PRIVATE/notebook-campaign \
  --expected-count EXPECTED_COUNT \
  --campaign-prefix CAMPAIGN_PREFIX --json
```

El comando crea `campaign-detail-report.html` (entrega principal para analistas),
`campaign-detail-report.md` (lectura lineal) y `campaign-detail-report.json`
(consulta automatizada). El HTML es autocontenido: incluye la evidencia de cada
recurso y el diff rojo/verde sin requerir servidor, dependencias ni internet.
Los tres archivos omiten rutas locales de la máquina y conservan referencias
relativas por lote. No incluyen filas de BigQuery ni resultados de consultas;
el HTML contiene el código de los cambios normales, mientras que los recursos
`sealed_copy` se muestran únicamente con metadatos y preview redactado. Todo
debe compartirse únicamente por canales internos autorizados. La opción
`--json` imprime un resumen JSON de la generación; el archivo completo queda en
`campaign-detail-report.json`.

### Piloto de migración legado (10 + 10)

La migración de rutas es un flujo separado. El diccionario se guarda en una
ruta privada fuera de Git y se aplica únicamente sobre tareas locales. El
piloto selecciona de forma reproducible 10 Shared Queries y 10 notebooks,
clasificados por rutas conocidas, incidentes y ausencia de rutas de origen.
No ejecuta SQL, no consulta tablas y nunca modifica el recurso original.

```bash
queryflow migration dictionary validate --dictionary PRIVATE/routes.json --json
queryflow pilot inventory --dictionary PRIVATE/routes.json --catalog catalog.json \
  --source-project SOURCE_PROJECT --destination-project DESTINATION_PROJECT \
  --account analyst@example.com --request-timeout 30 \
  --max-resources-per-kind 40 --dataform-requests-per-minute 180 \
  --output PRIVATE/manifest.json --json
```

El inventario remoto respeta la cuota de Dataform de `us-east1` (300
solicitudes/minuto) con un límite local de 180 solicitudes/minuto y reintentos
controlados para `429`. Cada lectura se guarda en un checkpoint privado; si se
interrumpe, repite el mismo comando con `--resume-inventory`.

Después prepara las tareas locales y abre el diff batch, sin ejecutar SQL ni
publicar nada:

```bash
queryflow pilot prepare --manifest PRIVATE/manifest.json \
  --dictionary PRIVATE/routes.json --account analyst@example.com \
  --dataform-requests-per-minute 180 --json
queryflow pilot review --manifest PRIVATE/manifest.json
queryflow pilot run --manifest PRIVATE/manifest.json --dictionary PRIVATE/routes.json
```

`prepare` crea un Web Preview por recurso y devuelve un `publication_digest`.
El último comando solo muestra el plan. Para crear las copias nuevas, el
analista debe cambiar explícitamente al perfil de campaña y aprobar ese digest:

```bash
queryflow permissions use migration-pilot
queryflow pilot run --manifest PRIVATE/manifest.json \
  --dictionary PRIVATE/routes.json --execute-migration \
  --account analyst@example.com --approved-digest PUBLICATION_DIGEST --json
```

Las copias llevan el sufijo `_piloto_migracion`. Las rutas no cubiertas se
conservan sin cambios y quedan en `route-incidents.json` (o en el estado final
como `published_with_incidents`).
La limpieza nunca es automática: primero genera un plan y un digest separados
con `queryflow pilot cleanup-plan`; solo una aprobación explícita de ese digest
permite `queryflow pilot cleanup`.

### Procedimientos almacenados

La migración completa de procedimientos BigQuery usa un flujo independiente de
Dataform. Lee las rutinas por REST (o por el gateway Workbench si el perímetro
VPC lo exige), reescribe las rutas de tablas y las llamadas a dependencias, y
prepara un diff oscuro con digest. No ejecuta SQL, `CALL` ni dry-run; solo crea
rutinas nuevas en el dataset destino `functions`, sin sobrescribir las que ya
existan. Consulta [Migración de procedimientos](docs/MIGRATION_ROUTINES.md)
para configurar el perfil, revisar lotes, interpretar bloqueos y publicar con
aprobación explícita.

## Flujo de trabajo

### 1. Modificar una consulta o notebook existente

Primero localiza el nombre canónico en el catálogo:

```bash
queryflow catalog refresh --account analyst@example.com --json
queryflow catalog search "siniestros" --kind notebook
```

Crea una tarea aislada. El recurso original no se modifica durante la edición:

```bash
queryflow start --resource CANONICAL_RESOURCE \
  --account analyst@example.com \
  --destination-project DESTINATION_PROJECT \
  --open-editor --json
```

Edita únicamente el archivo de la tarea (`TASK`). Después valida y abre el
Web Preview:

```bash
queryflow validate --task TASK \
  --account analyst@example.com \
  --config ~/.config/queryflow/config.toml \
  --backend workbench --json

queryflow review --task TASK --serve --watch --port 8080
```

El preview es de solo lectura y marca en verde las líneas agregadas y en rojo
las eliminadas. Revisa el SQL, las celdas afectadas, las advertencias, el
resultado de validación y el digest antes de continuar.

Para conocer el estado o preparar evidencia para permisos:

```bash
queryflow status --task TASK --json
queryflow diagnose --task TASK --format markdown --output diagnostic.md
```

### 2. Crear una consulta nueva

Guarda el SQL en un archivo y crea una tarea nueva:

```bash
queryflow start --mode new --kind shared_query \
  --name monthly_sales \
  --project DESTINATION_PROJECT \
  --location us \
  --content-file query.sql \
  --task-id monthly-sales-001 --json
```

Valida y revisa esta tarea con los mismos comandos anteriores.

### 3. Muestra opcional

La muestra no es automática. El agente debe mostrar primero el digest de
ejecución y el límite; el analista aprueba ese digest explícitamente:

```bash
queryflow sample --task TASK --limit 3 \
  --approved-digest SAMPLE_DIGEST \
  --account analyst@example.com \
  --config ~/.config/queryflow/config.toml \
  --backend workbench --json
```

El límite permitido es de 1 a 5 filas. En un notebook con varias consultas
debes indicar `--fragment CELL_INDEX`. QueryFlow conserva metadatos de la
muestra, no las filas.

### 4. Publicar

Publica únicamente después de que el analista apruebe el digest completo de la
tarea:

```bash
queryflow publish --task TASK \
  --approved-digest DIGEST \
  --destination-project DESTINATION_PROJECT \
  --account analyst@example.com \
  --config ~/.config/queryflow/config.toml --json
```

En el perfil `pilot`, la publicación crea una copia nueva. No actualiza ni
elimina el recurso original, no habilita programaciones y no ejecuta SQL
mutante automáticamente.

### 5. Evaluar FinOps y salud cloud

La evaluación es independiente de la edición de SQL, pero reutiliza el mismo
contexto autenticado y las allowlists. Ejecuta fuentes por capas sobre los
proyectos permitidos:

```bash
queryflow finops assess \
  --config ~/.config/queryflow/config.toml \
  --projects FINOPS_PROJECT --window-days 30 --json
```

Cloud Asset Inventory y Recommender aportan señales de inventario,
desperdicio, confiabilidad y rendimiento. Las métricas agregadas de BigQuery y
Billing Export (si están configuradas) se ejecutan dentro del Workbench
aprobado con dry-run, límite de bytes y salida acotada. La ausencia de una
fuente se declara como `partial`, `unavailable` o `unconfigured`; nunca se
interpreta como costo cero ni como ahorro inventado.

El comando genera `manifest.json`, evidencia y un informe doble (ejecutivo y
técnico) bajo `~/.queryflow/assessments/`. Verifica el digest antes de
compartirlo:

```bash
queryflow finops show --assessment ASSESSMENT_ID --json
queryflow finops review --assessment ASSESSMENT_ID --serve --port 8080
```

Todos los hallazgos son `plan_only`: contienen evidencia, confianza,
limitaciones y pasos propuestos, pero no cambian recursos GCP.

## Límites de seguridad

- Las credenciales permanecen en `gcloud`; nunca se guardan tokens en TOML,
  tareas, skills o auditorías.
- Las consultas mutantes, dinámicas, ambiguas o con varias sentencias se
  detienen para revisión.
- La ruta de credenciales de gcloud es persistente (`~/.config/gcloud`); una
  variable `CLOUDSDK_CONFIG` temporal heredada no reemplaza esa configuración.
- Las muestras reales deben ejecutarse dentro del perímetro Workbench
  configurado y requieren confirmación explícita.
- El diccionario de migración permanece privado y la reescritura de rutas está
  separada del flujo normal; el piloto acotado la usa únicamente con su
  manifest y sus controles propios.

## Documentación

- [Guía de uso](docs/USER_GUIDE.md): instalación y flujo completo para
  analistas.
- [Referencia de comandos](docs/COMMAND_REFERENCE.md): opciones, artefactos y
  límites de escritura.
- [Solución de problemas](docs/TROUBLESHOOTING.md): VPC, permisos,
  autenticación, Workbench, SQL y digest.
- [Skill de Codex](plugins/queryflow/skills/queryflow/SKILL.md): instrucciones
  que usa el agente para operar QueryFlow.
- [Arquitectura y frontera con Gemini](docs/ARCHITECTURE.md).
- [Evaluación FinOps y salud cloud](plugins/queryflow/skills/queryflow-finops/SKILL.md).
- [Guía del administrador GCP](docs/ADMIN_GUIDE.md).
- [Informe de aceptación beta](docs/BETA_ACCEPTANCE.md).
- [Lote de migración](docs/MIGRATION_BATCH.md): selección explícita, diff e
  informe de la campaña Analytics.
- [Piloto de migración legado](docs/MIGRATION_PILOT.md): diccionario privado,
  selección 10+10, publicación y limpieza separada.
- [Seguridad](SECURITY.md) y [contribución](CONTRIBUTING.md).

## Desarrollo

```bash
uv run --frozen python -m unittest discover -q
python3 scripts/build_release.py
```

El paquete requiere Python 3.11+ y las integraciones con GCP deben ser
explícitas y de solo lectura.
