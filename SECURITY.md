# Política de seguridad

QueryFlow es una capa de guardrails y revisión; no reemplaza IAM ni VPC Service
Controls. Mantén limitado el acceso de origen/destino en GCP y usa perfiles
locales que reduzcan las allowlists.

Los diagnósticos conservan un `error_id`, categoría, etapa, recuperación y
identificadores de proveedor, pero no tokens, filas ni SQL completo. La
publicación requiere un digest exacto, auditoría y lectura posterior. La
excepción estática está deshabilitada por defecto y nunca autoriza ejecutar
SQL.

Reporta problemas de seguridad por el flujo privado de GitHub. No incluyas
credenciales, tokens, resultados ni datos de clientes en un issue.

El proyecto excluye intencionalmente diccionarios de migración, scripts de
reescritura de rutas, notebooks operativos, tareas y auditorías de las
publicaciones públicas.
