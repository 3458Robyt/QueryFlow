# Guía de uso de QueryFlow

QueryFlow permite que un analista y un agente de IA trabajen sobre SQL y
notebooks sin editar directamente el recurso remoto. Cada cambio vive en una
tarea aislada, se valida, se revisa en un diff tipo Pull Request y solo se
publica después de aprobar un digest exacto.

## 1. Instalación

Desde Cloud Shell:

```bash
uvx --from git+https://github.com/3458Robyt/QueryFlow.git@v0.3.0-beta.1 queryflow install
queryflow init
```

Reinicia Codex después de instalar el plugin. Comprueba el entorno:

```bash
queryflow version --json
queryflow doctor --json
# Diagnóstico remoto opcional, siempre no mutante:
queryflow doctor --probe-remote --json
```

La identidad se mantiene en gcloud. No pegues tokens en la configuración, en
la tarea ni en una conversación.

## 2. Configuración inicial

El perfil `pilot` es el punto de partida recomendado. La configuración se
guarda en `~/.config/queryflow/config.toml` y puede contener proyectos,
ubicación, instancia Workbench y límites, pero no secretos.

Ejemplo sintético:

```bash
queryflow init --profile pilot \
  --gcloud-config-dir ~/.config/gcloud \
  --source-projects source-project \
  --destination-projects destination-project \
  --workbench-instance-project workbench-instance-project \
  --workbench-instance-location us-east1-b \
  --workbench-instance-name workbench-instance \
  --workbench-job-project workbench-project
queryflow config validate --json
queryflow policy show --json
```

El perfil `team` solo debe usarse con una configuración aprobada por el
responsable de permisos. No convierte automáticamente el piloto en modo de
actualización.

Para una migración, registra una sola vez el origen y destino y comprueba el
contexto antes de iniciar la tarea:

```bash
queryflow context alias set replication replication-project
queryflow context alias set analytics analytics-project
queryflow context set --source replication --destination analytics
queryflow context show --json
```

Puedes cambiar el perfil local con `queryflow permissions use pilot|team|full-access|migration-pilot`.
`full-access` solo está pensado para una orden explícita de publicación de un
notebook o Shared Query cuando el dry-run no está disponible; no ejecuta SQL ni
elimina recursos.

Para probar la migración de rutas usa el perfil independiente
`migration-pilot` y sigue la guía de [Piloto de migración](MIGRATION_PILOT.md).

## 3. Crear o modificar una query

### Recurso existente

Primero actualiza/busca el catálogo y copia el nombre canónico:

```bash
queryflow catalog refresh --account analyst@example.com --json
queryflow catalog search "sales" --kind notebook
```

Después crea la tarea:

```bash
queryflow start --resource CANONICAL_RESOURCE \
  --account analyst@example.com \
  --destination-project destination-project --json
```

Edita únicamente el archivo de la tarea. El recurso original no cambia durante
la edición.

### Query nueva

```bash
queryflow start --mode new --kind shared_query \
  --name monthly_sales --project destination-project --location us \
  --content-file query.sql --task-id monthly-sales-001 --json
```

La prueba piloto solo acepta SQL de lectura. Las consultas dinámicas,
mutantes, con varias sentencias o que no puedan clasificarse de forma segura
se detienen para revisión.

## 4. Validar y revisar

La prevalidación local ayuda a encontrar errores de extracción, pero no genera
un digest publicable:

```bash
queryflow validate --task TASK --backend local --static-only --json
```

La validación normal usa el backend configurado, normalmente Workbench:

```bash
queryflow validate --task TASK \
  --account analyst@example.com \
  --config ~/.config/queryflow/config.toml --json
```

Abre el diff visual:

```bash
queryflow review --task TASK --serve --watch
```

El analista debe verificar las líneas verdes agregadas, rojas eliminadas,
archivos/celdas, estado de validación, advertencias y digest. El Web Preview
es de solo lectura.

### Estado y diagnósticos

Para seguir el trabajo sin inspeccionar archivos internos:

```bash
queryflow status --task TASK --json
queryflow diagnose --task TASK --format markdown --output diagnostic.md
```

Si el error es de VPC o permisos, comparte el `error_id`, la categoría, la
etapa, el proyecto/ubicación y el identificador VPC que aparezca. Nunca
compartas tokens, filas ni el SQL completo.

## 5. Muestra opcional

La muestra no es automática. El agente primero muestra el digest de ejecución y
el límite solicitado; el analista debe aprobarlo explícitamente:

```bash
queryflow sample --task TASK --limit 3 \
  --approved-digest SAMPLE_DIGEST \
  --account analyst@example.com \
  --config ~/.config/queryflow/config.toml --json
```

El valor predeterminado es tres filas y el máximo es cinco. En notebooks con
varias consultas se requiere `--fragment CELL_INDEX`. Workbench ejecuta la
muestra dentro del perímetro y QueryFlow conserva únicamente metadatos sin
filas.

## 6. Publicar

Solo después de que el analista apruebe el digest de publicación exacto:

```bash
queryflow publish --task TASK \
  --approved-digest DIGEST \
  --destination-project destination-project \
  --account analyst@example.com \
  --config ~/.config/queryflow/config.toml --json
```

El piloto crea una copia nueva, comprueba el destino permitido, archiva la
auditoría y verifica la lectura posterior. No elimina, no habilita schedules y
no actualiza el recurso original.

### Excepción estática controlada

Solo el perfil `team` puede preparar una excepción y debe tener
`allow_static_exception = true` en una configuración revisada. Se usa cuando
la sintaxis local es correcta, pero el dry-run remoto está temporalmente
bloqueado. Requiere una razón y referencia aprobables:

```bash
queryflow exception prepare --task TASK \
  --reason "Bloqueo VPC documentado" --reference SEC-1234 \
  --config ~/.config/queryflow/config.toml --json
```

El agente muestra el digest de excepción y espera una aprobación explícita.
La publicación con ese digest solo guarda el código y mantiene las
comprobaciones de concurrencia, política, auditoría y lectura posterior.

### Publicación explícita en `full-access`

Si el analista ordena publicar sin validación/dry-run, usa el motivo auditable:

```bash
queryflow permissions use full-access
queryflow publish --task TASK --force-publish \
  --reason "Aprobación explícita del analista" \
  --account analyst@example.com --config ~/.config/queryflow/config.toml --json
```

QueryFlow mantiene la búsqueda canónica, el proyecto destino de la tarea, el
control de `head`, la auditoría, el hash del contenido y el read-back. Deja
`force-authorization.json` para revisión posterior.

## 7. Evaluar FinOps para negocio y plataforma

El flujo FinOps es una evaluación bajo demanda y no cambia el ciclo de edición
de consultas. Configura una allowlist explícita en el perfil:

```bash
queryflow init --profile pilot \
  --finops-projects finance-project,analytics-project \
  --finops-window-days 30 \
  --business-context-path business-context.toml
```

Ejecuta la evaluación desde Cloud Shell o un entorno autorizado:

```bash
queryflow finops assess --config ~/.config/queryflow/config.toml \
  --projects analytics-project --json
```

El resultado combina inventario de Cloud Asset Inventory, recomendaciones de
GCP y, cuando Workbench/Billing Export están disponibles, métricas agregadas
de jobs, almacenamiento y costo. La salida separa una lectura ejecutiva de un
apéndice técnico. Las fuentes tienen estado y limitaciones explícitos; una
fuente no disponible no se convierte en cero.

Abre el informe verificado sin endpoints de escritura:

```bash
queryflow finops show --assessment ASSESSMENT_ID --json
queryflow finops review --assessment ASSESSMENT_ID --serve
```

Los hallazgos identifican proyecto/recurso, evidencia, confianza, supuestos y
un plan `plan_only`. Antes de proponer cualquier cambio, confirma propietario,
criticidad, tratamiento contable y aprobación del equipo responsable. No
compartas filas crudas, respuestas completas de proveedores ni el mapa
empresarial.

## 8. Qué debe recibir el analista

El agente debe entregar siempre:

- estado y ruta de la tarea;
- recurso, archivos/celdas y conteo de cambios;
- backend y resultado de validación;
- URL del Web Preview;
- digest de muestra, si se solicitó;
- digest de publicación o bloqueo exacto;
- auditoría y lectura posterior cuando haya publicación.

Para comandos completos, consulta [COMMAND_REFERENCE.md](COMMAND_REFERENCE.md).
Para errores, consulta [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
