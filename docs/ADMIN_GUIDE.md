# Guía del administrador GCP

## Entorno recomendado

- QueryFlow se instala en Cloud Shell con Python 3.11+ y `uvx` o un wheel de
  la GitHub Release.
- Workbench no necesita instalar QueryFlow. Solo debe estar habilitado para el
  dry-run/muestra remoto y configurarse con proyecto, zona, instancia y
  proyecto de jobs.
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

El modo `--probe-remote` consulta las APIs habilitadas y describe la instancia
Workbench con operaciones de solo lectura. Para el flujo principal se esperan
`bigquery.googleapis.com`, `cloudasset.googleapis.com`,
`dataform.googleapis.com` y, si el backend es Workbench,
`notebooks.googleapis.com`. No habilita APIs ni ejecuta SQL.

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
