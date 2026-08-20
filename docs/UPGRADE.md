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
`permissions` para seleccionar `pilot`, `team` o `full-access`, y declarar la
instancia Workbench con campos explícitos. Las consultas programadas, la
migración de rutas y el diccionario continúan fuera del flujo principal.

Si el entorno hereda `CLOUDSDK_CONFIG` desde una sesión temporal, QueryFlow usa
`~/.config/gcloud` por defecto. Comprueba la ruta efectiva con
`queryflow doctor --json`.
