# Piloto de migración de rutas (legado)

> Este documento describe el piloto 10+10 heredado. Para cualquier migración
> nueva usa [MIGRATION_BATCH.md](MIGRATION_BATCH.md) y el comando
> `queryflow migration batch`; `queryflow pilot` permanece como alias temporal.

Este flujo prueba primero una migración pequeña y reversible: 10 Shared
Queries y 10 notebooks. El objetivo es comprobar la reescritura de referencias
internas, el control de rutas no cubiertas y la publicación como copias nuevas.

## Alcance

- El catálogo es la fuente para descubrir recursos y conservar sus nombres
  canónicos.
- El diccionario privado es la fuente única de reemplazos. Se valida por
  `dictionary_sha256` y se conserva fuera de Git.
- Solo se reemplazan rutas en SQL y código de celdas. Markdown, metadatos y
  salidas del notebook quedan intactos.
- Las referencias no cubiertas se mantienen sin cambios y se registran con
  recurso, archivo, celda, ruta y posiciones. No se hacen suposiciones ni
  consultas de existencia de tablas.
- La selección usa una semilla guardada y tres estratos por tipo: 5 con
  mappings conocidos, 3 con incidentes o mappings mixtos y 2 sin rutas de
  origen.
- Las copias se nombran con el sufijo `_piloto_migracion`. El origen nunca se
  actualiza.

El perfil `migration-pilot` es una excepción explícita y acotada. No modifica
las reglas de `pilot`, `team` ni `full-access`, no ejecuta SQL y no habilita
programaciones.

## Diccionario privado

Usa un JSON con este esquema mínimo:

```json
{
  "schema_version": 1,
  "dictionary_id": "routes-version",
  "source": {"image": "evidence.png", "sha256": "..."},
  "scope": {"source_project": "SOURCE_PROJECT", "destination_project": "DESTINATION_PROJECT"},
  "mappings": [
    {"id": "raw-001", "zone": "raw", "old": "SOURCE_PROJECT.DATASET", "new": "DEST_PROJECT.DATASET", "active": true}
  ],
  "reference_targets": [],
  "out_of_scope": []
}
```

Valida y genera la versión legible sin modificar el JSON:

```bash
queryflow migration dictionary validate --dictionary PRIVATE/routes.json --json
queryflow migration dictionary render --dictionary PRIVATE/routes.json \
  --output PRIVATE/routes.md --json
```

No agregues a Git la imagen, el JSON, el Markdown generado ni snapshots de
código. El `.gitignore` ya excluye el estado local de QueryFlow.

## Inventario y selección

Actualiza el catálogo y confirma el contexto antes del inventario. La opción
normal lee el contenido de cada recurso mediante Dataform, solo para
clasificarlo y guardar el SHA del `head` para el control de conflictos. Para
pruebas sin red puede usarse un
directorio privado de snapshots; el nombre esperado es el SHA-256 del nombre
canónico (con extensión opcional `.sql`, `.ipynb` o `.txt`).

```bash
queryflow catalog refresh --account ACCOUNT --projects SOURCE_PROJECT DESTINATION_PROJECT --json
queryflow pilot inventory --dictionary PRIVATE/routes.json \
  --catalog ~/.queryflow/catalog.json \
  --source-project SOURCE_PROJECT --destination-project DESTINATION_PROJECT \
  --account ACCOUNT --request-timeout 30 \
  --dataform-requests-per-minute 180 \
  --output PRIVATE/manifest.json --json
```

La cuota confirmada de Dataform para este flujo es de 300 solicitudes por
minuto en `us-east1`. QueryFlow usa 180 solicitudes/minuto como límite local
(margen del 40 %) para no agotar la cuota con llamadas concurrentes o de otros
usuarios. Las respuestas `429 RESOURCE_EXHAUSTED` se reintentan solo en
lecturas, con espera indicada por `Retry-After` o backoff de 5, 10, 20, 40 y
60 segundos. El resultado registra los contadores y la política aplicada.

El inventario persiste `inventory-checkpoint.json` después de cada recurso. Si
se corta la red, se agota la cuota o se interrumpe la terminal, reanuda sin
releer las selecciones terminadas:

```bash
queryflow pilot inventory --dictionary PRIVATE/routes.json --catalog catalog.json \
  --source-project SOURCE_PROJECT --destination-project DESTINATION_PROJECT \
  --account ACCOUNT --dataform-requests-per-minute 180 \
  --output PRIVATE/manifest.json --resume-inventory --json
```

La semilla, el hash del diccionario, los proyectos y la versión del catálogo
deben coincidir con el checkpoint. Los recursos con error se reintentan antes
que los nuevos. El flujo intercala Shared Queries y notebooks para evitar que
un solo tipo consuma todo el presupuesto de solicitudes.

Para catálogos grandes puedes limitar el pool que se exporta antes de
clasificarlo; el orden es aleatorio pero reproducible con `--seed`:

```bash
queryflow pilot inventory ... --max-resources-per-kind 40 --seed pilot-2026
```

El límite es por tipo y debe contener suficientes recursos en los tres
estratos; si no, aumenta el valor y repite el inventario.

El inventario se detiene antes de escribir si no existen al menos 10
selecciones posibles de cada tipo o si algún estrato requerido no tiene cupo.
Si el nombre visible ya existe en el destino, se omite para evitar colisiones.
Junto a la manifest (o al reporte de faltantes si aún no hay manifest) se genera
`route-incidents.json`, sin SQL ni filas, para revisar las rutas que no cubre
el diccionario. En notebooks incluye la celda y
la ruta editable (`cells/bXXXX.sql` o `.py`) donde se encontró el incidente.
Si alguna lectura de Dataform falla, se omite sin inventar evidencia y se
registra en `inventory-errors.json`; `--request-timeout` limita cada solicitud.
Si no se alcanza un cupo, no se crea manifest y se genera
`inventory-shortfall.json` con los disponibles y faltantes por estrato.

## Reescritura y revisión

Para una tarea individual, el reescritor es local y separado del script
histórico de notebooks:

```bash
queryflow migration rewrite plan --task TASK --dictionary PRIVATE/routes.json --json
queryflow migration rewrite apply --task TASK --dictionary PRIVATE/routes.json \
  --plan-digest PLAN_DIGEST --json
queryflow review --task TASK --serve --watch
```

`rewrite-report.json` contiene mappings aplicados, rutas desconocidas, hashes y
los checks deliberadamente no ejecutados (`sql_executed: false`,
`table_existence_checked: false`).

Para revisar la campaña completa sin exponer filas:

```bash
queryflow pilot review --manifest PRIVATE/manifest.json
```

Para generar los 20 diffs locales y sus Web Previews individuales sin publicar
ni ejecutar SQL:

```bash
queryflow pilot prepare --manifest PRIVATE/manifest.json \
  --dictionary PRIVATE/routes.json --account ACCOUNT \
  --dataform-requests-per-minute 180 --json
```

El comando crea una carpeta de tarea por recurso y una revisión batch. Las
líneas eliminadas aparecen en rojo y las agregadas en verde; cada incidente
conserva su ubicación. La salida incluye `publication_digest`, que representa
la campaña (proyectos, diccionario, semilla y selección) y no contiene filas,
SQL ni credenciales. `prepare` nunca habilita publicación.

## Publicación de copias

Sin el interruptor explícito, `run` no escribe en GCP:

```bash
queryflow pilot run --manifest PRIVATE/manifest.json \
  --dictionary PRIVATE/routes.json --json
```

La publicación de la campaña requiere tres señales intencionales: perfil
`migration-pilot`, `--execute-migration` y el `publication_digest` aprobado.

```bash
queryflow permissions use migration-pilot
queryflow pilot run --manifest PRIVATE/manifest.json \
  --dictionary PRIVATE/routes.json --execute-migration \
  --account ACCOUNT --approved-digest PUBLICATION_DIGEST --json
```

El proceso vuelve a leer cada recurso, comprueba que su `head` y hash coincidan
con el inventario, crea una tarea, aplica el diccionario y crea una copia
nueva. Continúa con fallos aislados; ante autenticación, permisos, VPC,
transporte o resultado remoto incierto detiene la campaña y guarda el estado
para `resume`. El informe final puede quedar en `published_with_incidents` si
hubo rutas desconocidas.

## Limpieza separada

La limpieza no forma parte de `run`. El plan lista únicamente los repositorios
creados por la campaña y calcula un digest sobre ese conjunto exacto:

```bash
queryflow pilot cleanup-plan --manifest PRIVATE/manifest.json --json
queryflow pilot cleanup --manifest PRIVATE/manifest.json \
  --approved-digest CLEANUP_DIGEST --account ACCOUNT --json
```

Se omite cualquier copia cuyo `head` haya cambiado desde la publicación. La
llamada de borrado siempre usa `force=false`; nunca elimina el repositorio
original ni recursos fuera del destino de la campaña.

## Ampliaciones posteriores

Views, rutinas, procedimientos y consultas programadas quedan como adaptadores
futuros. No se incluyen en este piloto porque requieren contratos distintos de
lectura, escritura, dependencias y concurrencia.
