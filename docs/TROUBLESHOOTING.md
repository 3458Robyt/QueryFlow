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
| Faltan campos Workbench | El perfil moderno no puede construir el runner dentro del perímetro. | Configurar proyecto, ubicación, instancia y proyecto de jobs mediante `queryflow init`. |
| Dataform `NOT_FOUND` | El nombre es antiguo, ambiguo o pertenece a otro proyecto/ubicación. | Refrescar catálogo y volver a empezar usando el `name` canónico exacto. |

## SQL y estado de la tarea

| Mensaje/estado | Qué significa | Qué hacer |
| --- | --- | --- |
| `mutating`, `unknown` o SQL dinámico | QueryFlow no puede demostrar que es lectura pura. | Convertirla en una consulta literal de lectura o detenerse para revisión. |
| Varias sentencias | La tarea contiene más de una unidad ejecutable. | Separar tareas o seleccionar un fragmento específico del notebook. |
| `prechecked` sin digest | Se usó `--static-only`; aún no hay validación publicable. | Ejecutar validación real en el backend aprobado. |
| `changes_required`, `failed` o error SQL | El contenido actual no pasó análisis o dry-run. | Corregir la tarea, validar de nuevo y descartar digests anteriores. |
| Digest/archivo stale | El archivo cambió después de validar o aprobar. | Crear una nueva validación y solicitar un nuevo digest. |
| Remote head changed | Alguien modificó el recurso original mientras la tarea estaba abierta. | Refrescar el catálogo, crear una tarea nueva y reaplicar el cambio. |
| Destino no permitido | El perfil no incluye proyecto o ubicación solicitados. | Usar un destino aprobado o pedir cambio de política; no forzar otro flag. |

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
queryflow install --ref v0.2.0-beta.1 --dry-run --json
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
