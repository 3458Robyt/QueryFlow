# Actualización desde v0.1

La beta `v0.2.0-beta.1` conserva los comandos, tareas y perfiles existentes.
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
deshabilitada. Las consultas programadas, la migración de rutas y el diccionario
continúan fuera del flujo principal.
