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
| `queryflow start` | Crear una tarea aislada. | opcional | no |
| `queryflow validate` | Clasificar y validar SQL/notebooks. | opcional | no |
| `queryflow review` | Crear o servir el diff Web Preview. | no | no |
| `queryflow sample` | Ejecutar una muestra aprobada dentro de Workbench. | sí | no |
| `queryflow publish` | Crear una copia aprobada o actualizar en modo team. | sí | sí |

## Instalación y configuración

### `queryflow version`

Muestra versiones. No requiere configuración:

```bash
queryflow version --json
```

### `queryflow init`

Crea un perfil local. Opciones principales: `--profile pilot|team`,
`--account`, `--source-projects`, `--destination-projects`, los cuatro campos
`--workbench-*`, `--validation-backend` y `--max-bytes`.

```bash
queryflow init --profile pilot \
  --source-projects source-project \
  --destination-projects destination-project \
  --workbench-project workbench-project \
  --workbench-location us-east1-b \
  --workbench-instance workbench-instance \
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
queryflow install --ref v0.2.0-beta.1 --dry-run --json
queryflow self-update --ref v0.2.0-beta.1 --dry-run --json
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

Requiere `--task`, el digest completo, `--destination-project` y `--account`.
También necesita una validación real, auditoría configurada y lectura posterior
coincidente:

```bash
queryflow publish --task TASK --approved-digest DIGEST \
  --destination-project destination-project --account analyst@example.com \
  --config ~/.config/queryflow/config.toml --json
```

En `pilot` crea una copia nueva. La actualización de un recurso existente está
restringida al perfil `team` y no se habilita mediante un flag aislado.

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
