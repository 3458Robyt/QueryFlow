# Solución de problemas

QueryFlow detiene una operación cuando no puede demostrar que es segura. La
tarea y el diff deben conservarse para poder reintentar después de corregir el
acceso o el contenido.

## Diagnóstico inicial

```bash
queryflow doctor --config ~/.config/queryflow/config.toml --json
queryflow doctor --config ~/.config/queryflow/config.toml --probe-remote --json
gcloud auth list
queryflow config validate --json
queryflow context show --json
queryflow policy show --config ~/.config/queryflow/config.toml --json
```

No compartas tokens ni resultados completos de consultas. Comparte el estado,
`error_kind`, proyecto/ubicación, instancia Workbench, cuenta y hora del fallo.

## Acceso y red

| Mensaje/estado | Qué significa | Qué hacer |
| --- | --- | --- |
| `blocked_vpc` / `VPC Service Controls` | La solicitud no respetó el perímetro de la organización o salió por el entorno incorrecto. | Ejecutar desde la instancia Workbench aprobada y enviar al responsable de GCP el proyecto, ubicación, cuenta y hora. |
| `blocked_permission` / `PERMISSION_DENIED` / `Access Denied` | La identidad no tiene un permiso de IAM, Dataform, BigQuery o Workbench. | Confirmar `gcloud auth list`, cuenta explícita y proyectos permitidos; solicitar el rol mínimo necesario. |
| `authentication` | gcloud no pudo emitir un token para la cuenta elegida. | Renovar la autenticación aprobada y usar `--account`; nunca guardar el token en TOML. |
| `transport`, timeout o `ServerNotFoundError` | Cloud Shell no alcanza el servicio o el proxy Workbench. | Revisar `doctor`, instancia/ubicación/proyecto del job y reintentar; no cambiar al perímetro equivocado. |
| Faltan campos Workbench | El perfil moderno no puede construir el runner dentro del perímetro. | Configurar `--workbench-instance-project`, `--workbench-instance-location`, `--workbench-instance-name` y `--workbench-job-project` mediante `queryflow init`. |
| La cuenta cambia o desaparece | Se heredó un `CLOUDSDK_CONFIG` temporal de una sesión anterior. | Comprueba `queryflow doctor --json`; QueryFlow usa `~/.config/gcloud` de forma persistente. Reautentica allí si hace falta. |
| Dataform `NOT_FOUND` | El nombre es antiguo, ambiguo o pertenece a otro proyecto/ubicación. | Refrescar catálogo y volver a empezar usando el `name` canónico exacto. |

## Evaluaciones FinOps

| Mensaje/estado | Qué significa | Qué hacer |
| --- | --- | --- |
| `No hay proyectos FinOps permitidos` | El perfil no tiene `finops_projects` ni proyectos origen/destino utilizables. | Configurar una allowlist explícita con `queryflow init` o `config set`; no enumerar la organización automáticamente. |
| `--projects ... fuera de alcance` | El argumento intenta ampliar la allowlist. | Elegir solo proyectos ya permitidos y confirmar `queryflow config list --json`. |
| Fuente `unconfigured` | La fuente es opcional o falta una configuración (por ejemplo, Billing Export o Workbench). | Continuar con las fuentes disponibles y reportar la limitación; no asumir costo cero. |
| Fuente `unavailable` / `partial` | IAM, VPC, región, API o transporte impidió una o más consultas. | Revisar la advertencia y el `error_kind`, pedir el rol mínimo o corregir Workbench; repetir el assessment. |
| Digest de assessment no coincide | Se modificó un artefacto local después de generarlo. | No compartirlo; conserva el directorio para investigación y genera un assessment nuevo. |
| Cobertura de contexto baja | Faltan labels/tags o entradas del mapa para owner, unidad, centro de costo o criticidad. | Completar el contrato de contexto y repetir la evaluación; la ausencia no implica recurso huérfano. |
| Billing Export sin tendencia | La tabla no tiene datos suficientes para ambos periodos o el esquema no coincide. | Verificar tabla, región, permisos y ventana; presentar la fuente como parcial, no como cero. |

## SQL y estado de la tarea

| Mensaje/estado | Qué significa | Qué hacer |
| --- | --- | --- |
| `mutating`, `unknown` o SQL dinámico | QueryFlow no puede demostrar que es lectura pura. | Convertirla en una consulta literal de lectura o detenerse para revisión. |
| Varias sentencias | La tarea contiene más de una unidad ejecutable. | Separar tareas o seleccionar un fragmento específico del notebook. |
| `prechecked` sin digest | Se usó `--static-only`; aún no hay validación publicable. | Ejecutar validación real en el backend aprobado. |
| `changes_required`, `failed` o error SQL | El contenido actual no pasó análisis o dry-run. | Corregir la tarea, validar de nuevo y descartar digests anteriores. |
| Digest/archivo stale | El archivo cambió después de validar o aprobar. | Crear una nueva validación y solicitar un nuevo digest. |
| Remote head changed | Alguien modificó el recurso original mientras la tarea estaba abierta. | Refrescar el catálogo, crear una tarea nueva y reaplicar el cambio. |
| Destino no permitido | El perfil no incluye proyecto o ubicación solicitados. | Revisar `queryflow context show` y usar un destino aprobado; `--force-publish` no permite saltarse esta regla. |
| `--force-publish` rechazado | El perfil no es `full-access`, falta `allow_force_publish` o no se indicó motivo. | Cambiar el perfil con aprobación administrativa o usar el flujo normal con digest. |

## Muestra y Web Preview

| Mensaje/estado | Qué significa | Qué hacer |
| --- | --- | --- |
| Digest de muestra no coincide | Cambió el SQL, fragmento o límite. | Validar de nuevo, mostrar el digest nuevo y pedir aprobación explícita. |
| Se requiere `--fragment` | El notebook contiene varias consultas SQL. | Elegir el índice exacto; no muestrear la primera automáticamente. |
| Muestra rechazada por backend | El flujo no está dentro de Workbench o faltan sus parámetros. | Completar el perfil Workbench; no ejecutar filas desde Cloud Shell. |
| No aparece la URL | El servidor no se inició o el puerto está ocupado. | Ejecutar `queryflow review --task TASK --serve --port 8080` y abrir el enlace de Web Preview. |
| Preview stale | Cambió la tarea después de generar el HTML. | Regenerar `review`; si cambió el SQL, volver a validar. |
| Filas guardadas en un artefacto | Se rompió la retención de datos. | Detener la distribución, seguir el proceso de incidente y conservar solo metadatos sin filas. |

## Instalación y plugin

```bash
queryflow install --ref v0.3.0-beta.1 --dry-run --json
```

Si la CLI funciona pero Codex no reconoce QueryFlow, confirma que el
marketplace y el plugin estén instalados y reinicia la sesión de Codex. Si
GitHub devuelve un error de permisos, solicita acceso de lectura/escritura al
responsable del repositorio; nunca pongas una credencial en el plugin, skill,
README o task.

## Qué enviar al responsable de permisos

Envía un resumen corto y no sensible:

```text
Comando: queryflow validate --task ... --backend workbench
Cuenta: analyst@example.com
Proyecto/ubicación: source-project / us
Workbench: workbench-project / us-east1-b / workbench-instance
Estado/error_kind: blocked_vpc / vpc
Hora UTC: YYYY-MM-DDTHH:MM:SSZ
```

No envíes tokens, filas, SQL completo ni archivos de auditoría.
