{{- define "tss.name" -}}{{ .Release.Name }}{{- end }}

{{- define "tss.secretName" -}}
{{- .Values.existingSecret | default (include "tss.name" .) -}}
{{- end }}

{{- define "tss.catalogConfigMap" -}}
{{- .Values.existingCatalogConfigMap | default (printf "%s-catalog" (include "tss.name" .)) -}}
{{- end }}

{{- define "tss.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end }}

{{- define "tss.openbaoAddr" -}}
{{- .Values.openbao.addr | default .Values.token.openbao.addr -}}
{{- end }}

{{- define "tss.openbaoRole" -}}
{{- .Values.token.openbao.role | default .Values.openbao.role -}}
{{- end }}

{{- define "tss.validate" -}}
{{- if not .Values.terrakube.uiUrl }}{{ fail "terrakube.uiUrl is required" }}{{ end }}
{{- if not .Values.terrakube.apiUrl }}{{ fail "terrakube.apiUrl is required" }}{{ end }}
{{- if not .Values.allowInsecureTransport }}
{{- range $name, $url := dict "terrakube.apiUrl" .Values.terrakube.apiUrl "openbao.addr" (include "tss.openbaoAddr" .) "users.token.issuer" .Values.users.token.issuer "users.token.jwksUrl" .Values.users.token.jwksUrl "ui.oidc.issuer" .Values.ui.oidc.issuer }}
{{- if and $url (not (hasPrefix "https://" $url)) }}
{{- fail (printf "%s must be an https:// URL (or set allowInsecureTransport for development)" $name) }}
{{- end }}
{{- end }}
{{- if and (not $.Values.existingSecret) (not (hasPrefix "verify-" .Values.database.sslmode)) }}
{{- fail "database.sslmode must be verify-full or verify-ca (or set allowInsecureTransport for development)" }}
{{- end }}
{{- end }}
{{- if and .Values.users.token.issuer (not .Values.users.token.audience) }}
{{- fail "users.token.audience is required with users.token.issuer" }}
{{- end }}
{{- if not .Values.terrakube.organization }}{{ fail "terrakube.organization is required" }}{{ end }}
{{- if not (or .Values.token.existingSecret.name .Values.token.value (include "tss.openbaoAddr" .)) }}
{{- fail "set token.existingSecret.name, token.value or openbao.addr" }}
{{- end }}
{{- if and .Values.ui.enabled .Values.ui.oidc.issuer (not .Values.existingSecret) (lt (len .Values.ui.sessionSecret) 32) }}
{{- fail "ui.sessionSecret must be at least 32 characters" }}
{{- end }}
{{- if and (not .Values.existingCatalogConfigMap) (not .Values.catalog.templates) }}
{{- fail "catalog.templates is empty; configure at least one template or set existingCatalogConfigMap" }}
{{- end }}
{{- end }}
