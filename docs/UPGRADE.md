# Actualización desde v0.1/v0.2

La beta `v0.3.0-beta.1` conserva los comandos, tareas y perfiles existentes.
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
`permissions` para seleccionar `pilot`, `team`, `full-access` o el perfil
independiente `migration-pilot`, y declarar la instancia Workbench con campos
explícitos. Las consultas programadas continúan fuera del piloto. La migración
de rutas solo se ejecuta mediante el manifest 10+10 y los comandos documentados
en [MIGRATION_PILOT.md](MIGRATION_PILOT.md).

Si el entorno hereda `CLOUDSDK_CONFIG` desde una sesión temporal, QueryFlow usa
`~/.config/gcloud` por defecto. Comprueba la ruta efectiva con
`queryflow doctor --json`.
