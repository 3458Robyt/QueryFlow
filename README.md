# QueryFlow

QueryFlow es una CLI de Python y un plugin de Codex para crear o modificar
consultas SQL y notebooks en Google Cloud con revisión humana. Cada cambio se
trabaja en una tarea aislada, se valida, se muestra en un diff tipo Pull
Request y solo se publica después de aprobar el digest exacto.

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

La beta disponible para el equipo es `v0.2.0-beta.1`:

```bash
uvx --from git+https://github.com/3458Robyt/QueryFlow.git@v0.2.0-beta.1 \
  queryflow install --ref v0.2.0-beta.1
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
  --source-projects SOURCE_PROJECT \
  --destination-projects DESTINATION_PROJECT \
  --workbench-project WORKBENCH_PROJECT \
  --workbench-location us-east1-b \
  --workbench-instance WORKBENCH_INSTANCE \
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

## Límites de seguridad

- Las credenciales permanecen en `gcloud`; nunca se guardan tokens en TOML,
  tareas, skills o auditorías.
- Las consultas mutantes, dinámicas, ambiguas o con varias sentencias se
  detienen para revisión.
- Las muestras reales deben ejecutarse dentro del perímetro Workbench
  configurado y requieren confirmación explícita.
- El diccionario de migración y la reescritura de rutas son procesos separados;
  no forman parte de QueryFlow.

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
- [Guía del administrador GCP](docs/ADMIN_GUIDE.md).
- [Informe de aceptación beta](docs/BETA_ACCEPTANCE.md).
- [Seguridad](SECURITY.md) y [contribución](CONTRIBUTING.md).

## Desarrollo

```bash
uv run --frozen python -m unittest test_queryflow.py tests
uv build
```

El paquete requiere Python 3.11+ y las integraciones con GCP deben ser
explícitas y de solo lectura.
