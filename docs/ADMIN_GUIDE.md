# Guía del administrador GCP

## Entorno recomendado

- QueryFlow se instala en Cloud Shell con Python 3.11+ y `uvx` o un wheel de
  la GitHub Release.
- Workbench no necesita instalar QueryFlow. Solo debe estar habilitado para el
  dry-run/muestra remoto y configurarse con proyecto de instancia, zona,
  nombre de instancia y proyecto de jobs.
- La identidad se obtiene de `gcloud`; no se guardan tokens en TOML ni en
  tareas.

## Preparación

1. Conceder al usuario únicamente el acceso de lectura al catálogo y el acceso
   de publicación aprobado en los proyectos destino.
2. Permitir las APIs y roles que ya requiere la organización para BigQuery,
   Dataform, Workbench y el transporte usado por la instancia.
3. Si existe VPC Service Controls, incluir la ruta Cloud Shell/Workbench
   aprobada y conservar el identificador VPC de los errores.
4. Configurar las allowlists de proyectos y ubicaciones en el perfil; una
   allowlist vacía no amplía permisos GCP, pero una allowlist explícita sí
   restringe QueryFlow.

Comprobaciones no mutantes:

```bash
gcloud auth list
queryflow doctor --config ~/.config/queryflow/config.toml --json
queryflow doctor --config ~/.config/queryflow/config.toml --probe-remote --json
queryflow policy show --config ~/.config/queryflow/config.toml --json
```

La configuración usa por defecto `~/.config/gcloud`; una variable
`CLOUDSDK_CONFIG` temporal heredada no debe cambiar la cuenta sin una decisión
explícita. Declara la instancia Workbench con sus cuatro campos:

```bash
queryflow init --profile pilot \
  --gcloud-config-dir ~/.config/gcloud \
  --workbench-instance-project WORKBENCH_INSTANCE_PROJECT \
  --workbench-instance-location us-east1-b \
  --workbench-instance-name WORKBENCH_INSTANCE \
  --workbench-job-project JOB_PROJECT
```

El modo `--probe-remote` consulta las APIs habilitadas y describe la instancia
Workbench con operaciones de solo lectura. Para el flujo principal se esperan
`bigquery.googleapis.com`, `cloudasset.googleapis.com` y
`dataform.googleapis.com`. Para Workbench se acepta
`notebooks.googleapis.com` o `aiplatform.googleapis.com`, según el tipo de
instancia. No habilita APIs ni ejecuta SQL.

## Evaluación FinOps y salud cloud

El assessment requiere una allowlist de proyectos administrada por el equipo
(`finops_projects` o las allowlists de origen/destino). El usuario puede
reducirla con `--projects`, pero nunca ampliarla. Concede únicamente acceso de
lectura a Cloud Asset Inventory y Recommender en esos proyectos.

Las métricas de BigQuery y Billing Export se ejecutan dentro de la instancia
Workbench aprobada, no desde el equipo local. Configura los cuatro campos de
Workbench y, si aplica, una tabla `project.dataset.table` de Billing Export:

```bash
queryflow init --profile pilot \
  --finops-projects FINOPS_PROJECT \
  --billing-export-table BILLING_PROJECT.DATASET.TABLE \
  --business-context-path business-context.toml
```

El mapa empresarial debe ser un archivo TOML administrado por el equipo y no
debe contener credenciales. Revisa la cobertura de `owner`, `business_unit`,
`cost_center` y `criticality` en el informe; la herramienta guarda solo los
campos resueltos y un fingerprint del mapa. Billing Export es opcional: si no
está disponible, el assessment lo informa y conserva las otras señales.

La operación es siempre de solo lectura y `plan_only`. El equipo de plataforma
debe validar cada hallazgo y usar un proceso de cambio independiente para
mutaciones futuras.

## Perfiles y publicación force

`pilot` permite copias nuevas, `team` permite actualizaciones aprobadas y
`full-access` permite además una publicación explícita de un notebook o Shared
Query cuando no hay dry-run disponible. No es un permiso IAM ilimitado: no
ejecuta SQL, no activa schedules y no elimina recursos.

```bash
queryflow permissions use full-access
queryflow publish --task TASK --force-publish \
  --reason "Ticket y aprobación del analista" \
  --account analyst@example.com --config ~/.config/queryflow/config.toml --json
```

La ruta force exige recurso canónico, destino permitido, control de `head`,
auditoría y read-back. Conserva `force-authorization.json` como evidencia.

## Diagnósticos para soporte

Pedir al analista el `error_id`, categoría, etapa, hora UTC, proyecto,
ubicación, instancia Workbench y, si existe,
`vpcServiceControlsUniqueIdentifier`. No pedir tokens, filas ni el SQL
completo. El archivo generado por `queryflow diagnose --format markdown` es el
bundle recomendado.

## Excepción estática

`allow_static_exception = true` solo puede aparecer en el perfil `team` que el
responsable de seguridad haya revisado. La excepción exige ticket/razón y un
digest separado; no autoriza ejecución de SQL, activación de schedules ni
saltos de política, conflicto, integridad o sintaxis.
