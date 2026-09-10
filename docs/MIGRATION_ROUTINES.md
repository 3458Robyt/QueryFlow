# Migración de procedimientos almacenados

Este es el flujo de QueryFlow para copiar procedimientos GoogleSQL y sus
dependencias de BigQuery desde un proyecto origen a un dataset consolidado de
funciones en el proyecto destino. Está diseñado para campañas completas, no
para ejecutar ni probar los procedimientos.

## Decisiones del flujo

- Origen y destino se declaran explícitamente. La configuración recomendada
  para cada campaña se pasa explícitamente `SOURCE_PROJECT` →
  `DESTINATION_PROJECT`.
- El dataset destino por defecto es `functions`; QueryFlow no crea el dataset.
- Se lee el inventario por la API REST de BigQuery (`datasets`, `routines` y
  `routines.get`). No se envía un job SQL, no se hace `CALL` y no se ejecuta
  dry-run.
- Las llamadas a procedimientos/UDFs internos que estén inventariados se
  reescriben a `DESTINO.functions.nombre`. Las rutas de tablas se reescriben
  con el diccionario privado aprobado. Una ruta no cubierta se deja intacta y
  se registra con línea y advertencia.
- Se conserva el nombre del procedimiento. Si ya existe en destino, una
  definición distinta se marca como conflicto; nunca se sobrescribe, renombra
  ni elimina.
- SQL dinámico, DML/DDL y rutas no cubiertas se pueden copiar como código para
  revisión humana, pero quedan etiquetados. Secretos, dependencias ausentes,
  tipos no soportados, cambios del origen y fallos de lectura posterior son
  bloqueos.
- Los accesos observados en el dataset se guardan como evidencia informativa.
  QueryFlow no concede, copia ni modifica IAM o autorizaciones de rutinas.

## Configuración inicial

Para el backend directo basta con `bigquery.googleapis.com` y permisos de
lectura de datasets/rutinas en el origen y de creación de rutinas en el
dataset destino. Para el fallback Workbench también se necesita acceso de
lectura a la instancia y su proxy (`notebooks.googleapis.com`). La migración
no depende de Dataform API, no solicita permisos de ejecución (`CALL`) y no
intenta cambiar IAM.

Usa el directorio persistente de gcloud, nunca un `CLOUDSDK_CONFIG` temporal:

```bash
gcloud auth list
gcloud config set account CUENTA
queryflow init --profile migration-batch \
  --account CUENTA \
  --gcloud-config-dir ~/.config/gcloud \
  --source-projects SOURCE_PROJECT \
  --destination-projects DESTINATION_PROJECT \
  --allow-routine-migration \
  --routine-backend auto \
  --routine-destination-dataset functions \
  --routine-batch-size 20 \
  --routine-requests-per-minute 120 \
  --json
queryflow config validate --json
queryflow permissions show --json
```

`auto` intenta la API directa y solo cambia al gateway Workbench si la lectura
falla por perímetro/VPC. Si se fuerza `workbench`, deben estar configurados los
cuatro valores: proyecto de la instancia, zona (`us-east1-b`), nombre de la
instancia (`sbs-analytics-python-notebook`) y proyecto de jobs. La cuenta se
obtiene siempre del perfil o de `--account`; no se seleccionan archivos de
credenciales temporales.

## Inventario y preparación

Valida el diccionario privado antes del inventario. El archivo no debe entrar
al repositorio:

```bash
queryflow migration dictionary validate --dictionary PRIVATE/routes.json --json
queryflow migration routines inventory \
  --source-project SOURCE_PROJECT \
  --destination-project DESTINATION_PROJECT \
  --destination-dataset functions \
  --dictionary PRIVATE/routes.json \
  --campaign-id routines-CAMPAIGN_ID \
  --backend auto --account CUENTA --json
```

El comando genera un `manifest.json`, `report.json`, `report.md` y
`review.html` bajo `~/.queryflow/migrations/<campaign-id>/` (o en `--output`).
El manifiesto incluye hashes de origen y propuesta, rutas aplicadas y no
cubiertas, dependencias, clasificación estática, estado, lotes, digest y la
evidencia de acceso. El código normal de la propuesta se guarda en
`proposals/`; las rutinas selladas solo contienen un marcador redactado.

Antes de solicitar aprobación, refresca las propuestas contra BigQuery y
vuelve a calcular el digest:

```bash
queryflow migration routines prepare \
  --manifest PRIVATE/routines-CAMPAIGN_ID/manifest.json \
  --dictionary PRIVATE/routes.json --account CUENTA --json
queryflow migration routines review \
  --manifest PRIVATE/routines-CAMPAIGN_ID/manifest.json --serve
```

El Web Preview es local y oscuro, con filtro por rutina/estado y diff rojo
(origen) / verde (propuesta). No es necesario copiar el SQL a BigQuery Studio.

## Aprobación y publicación

El resultado de `inventory` o `prepare` muestra `publication_digest` y, si
corresponde, `sealed_publication_digest`. El analista debe revisar el preview,
los bloqueos y el reporte, y entregar el digest completo. Para publicar:

```bash
queryflow migration routines run \
  --manifest PRIVATE/routines-CAMPAIGN_ID/manifest.json \
  --dictionary PRIVATE/routes.json \
  --execute-migration \
  --approved-digest DIGEST_COMPLETO \
  --account CUENTA --json
```

Los lotes pueden publicarse de forma independiente con `--lot N`; en ese caso
se aprueba el digest del lote mostrado en `lots`. `run` vuelve a leer cada
origen, comprueba su hash, verifica que el destino no exista, hace un `POST` `routines.insert`
únicamente en `DESTINATION_PROJECT.functions` y lee de nuevo la rutina para comparar todos
los campos semánticos. Solo después de esa lectura
posterior registra `published_verified`. Las rutinas ya idénticas quedan como
`already_present_identical`; los conflictos y bloqueos no se escriben.

No se acepta un digest abreviado, no se usa `--force-publish` y no existe una
opción para actualizar o eliminar una rutina. Cada ejecución deja
`routine-audit.json` junto al manifiesto, sin cuerpos SQL, filas, tokens ni
credenciales.

## Rutinas selladas y recuperación

Si una rutina contiene un literal que parece secreto, el modo predeterminado
es `block`. Solo una autorización de seguridad puede usar `--secret-handling
sealed_copy` durante el inventario. La publicación exige, además del digest
normal, el digest sellado y una referencia de ticket:

```bash
queryflow migration routines run \
  --manifest PRIVATE/routines-CAMPAIGN_ID/manifest.json \
  --dictionary PRIVATE/routes.json --execute-migration \
  --approved-digest DIGEST_COMPLETO \
  --approved-sealed-digest DIGEST_SELLADO \
  --security-reference SEC-0000 --account CUENTA --json
```

Si se interrumpe una campaña, `resume` es un alias de `run` que conserva el
mismo manifiesto y digest; no reconstruye ni publica una definición distinta.
Para regenerar solamente los informes sin tocar GCP:

```bash
queryflow migration routines report \
  --manifest PRIVATE/routines-CAMPAIGN_ID/manifest.json --json
```

Un error de autenticación, VPC, cuota, transporte, conflicto, integridad o
lectura posterior debe quedar en el reporte y resolverse antes de reintentar.
