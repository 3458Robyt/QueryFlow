# Actualización desde v0.1/v0.2/v0.3

La beta `v0.4.0-beta.1` conserva los comandos, tareas y perfiles existentes.
Los manifiestos y validaciones antiguas se leen con compatibilidad hacia atrás;
las nuevas validaciones escriben sus esquemas versionados.

Después de instalar:

```bash
queryflow version --json
queryflow config validate --json
queryflow doctor --json
```

Los nuevos comandos `status`, `diagnose` y `exception prepare` son opcionales.
El perfil `pilot` sigue siendo el predeterminado y la excepción estática está
deshabilitada. Ahora puedes usar `context` para fijar origen/destino,
`permissions` para seleccionar `pilot`, `team`, `full-access`,
`migration-pilot` (compatibilidad) o `migration-batch`, y declarar la instancia
Workbench con campos explícitos. Las consultas programadas continúan fuera del
lote. Las nuevas migraciones usan una selección explícita, un informe de rutas
un digest global y clasificación de revisión documentados en
[MIGRATION_BATCH.md](MIGRATION_BATCH.md). `migration-batch` puede copiar
incidencias de rutas/SQL para revisión humana, pero mantiene bloqueados
secretos, contenido vacío, conflictos y fallos de integridad; los flujos
normales siguen exigiendo SQL de lectura. El manifest 10+10 queda como
compatibilidad en [MIGRATION_PILOT.md](MIGRATION_PILOT.md);
el manifest 10+10 queda como compatibilidad en [MIGRATION_PILOT.md](MIGRATION_PILOT.md).

Si el entorno hereda `CLOUDSDK_CONFIG` desde una sesión temporal, QueryFlow usa
`~/.config/gcloud` por defecto. Comprueba la ruta efectiva con
`queryflow doctor --json`.
