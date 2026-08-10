# Aceptación de QueryFlow v0.2.0-beta.1

Este documento deja reproducible la verificación realizada para la beta. La
publicación de la Release y la aceptación contra GCP real siguen requiriendo
la aprobación explícita del equipo y una cuenta/perímetro autorizados.

## Comandos ejecutados

```bash
python3 -m unittest discover -v
python3 -m compileall -q queryflow
uvx --from ruff==0.8.6 ruff check queryflow tests scripts
uvx --from ruff==0.8.6 ruff format --check \
  queryflow/errors.py scripts/check_version_consistency.py tests/test_v2_workflow.py
uvx --from mypy==1.14.1 mypy --ignore-missing-imports \
  queryflow
uv run --with coverage==7.6.10 coverage run --source=queryflow \
  -m unittest discover -q
uv run --with coverage==7.6.10 coverage report --fail-under=55
python3 scripts/check_version_consistency.py
python3 scripts/validate_plugin.py plugins/queryflow
python3 scripts/check_release_hygiene.py
uv lock --check
uv build
```

## Resultado local

- 157 pruebas pasaron en Python 3.12; la matriz CI repite el conjunto en
  Python 3.11 y 3.12.
- Compilación, Ruff, formato de los archivos nuevos, tipos de los módulos
  beta, coherencia de versión, higiene del release y plugin pasaron.
- La cobertura actual del repositorio es 60%; el gate beta se fija en 55%
  porque la CLI/adaptadores heredados de v0.1 aún tienen rutas no cubiertas.
- `uv build` produce wheel y paquete fuente; los archivos `dist/*` se deben
  adjuntar a la Release junto con `SHA256SUMS`.

## Escenarios cubiertos

La suite conserva el flujo v0.1 y añade comprobaciones de estado, diagnósticos
redactados (incluido `vpcServiceControlsUniqueIdentifier`), digest stale,
conflicto remoto, destino no permitido, auditoría/lectura posterior, notebook
con varias celdas, excepciones estáticas independientes, Web Preview oscuro y
`doctor --probe-remote` simulado sin escrituras.

## Aceptación manual pendiente en GCP

Con una cuenta y perímetro aprobados, el responsable debe ejecutar desde Cloud
Shell:

1. `queryflow catalog refresh` y `queryflow catalog search` para elegir una
   Shared Query y un notebook canónicos.
2. `queryflow start`, editar la tarea desde Codex y abrir
   `queryflow review --task TASK --serve`.
3. Ejecutar `queryflow validate --backend workbench --json`, revisar el diff y
   aprobar el digest mostrado por el agente.
4. Publicar una copia nueva con `queryflow publish --approved-digest ...` y
   comprobar el contenido remoto con una lectura posterior.
5. Repetir modificación de la misma query y creación/modificación de un
   notebook. No ejecutar SQL mutante ni copiar/pegar en BigQuery Studio.

Si el dry-run queda bloqueado, conservar el `error_id` y el
`vpcServiceControlsUniqueIdentifier` de `queryflow diagnose`; no usar la
excepción estática salvo que seguridad habilite el perfil `team` y entregue
razón y referencia.

## Evidencia y publicación

La versión del paquete, plugin y tag se normaliza como `0.2.0b1` / `v0.2.0-beta.1`.
Antes de crear la Release inmutable, el agente debe mostrar el digest de los
artefactos y recibir aprobación explícita. Sin esa aprobación no se realizan
push, tag ni publicación remota.
