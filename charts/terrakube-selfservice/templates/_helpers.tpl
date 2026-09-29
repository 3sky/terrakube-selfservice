{{- define "tss.name" -}}{{ .Release.Name }}{{- end }}

{{- define "tss.secretName" -}}
{{- .Values.existingSecret | default (include "tss.name" .) -}}
{{- end }}

{{- define "tss.catalogConfigMap" -}}
{{- .Values.existingCatalogConfigMap | default (printf "%s-catalog" (include "tss.name" .)) -}}
{{- end }}

{{- define "tss.openbaoAddr" -}}
{{- .Values.openbao.addr | default .Values.token.openbao.addr -}}
{{- end }}

{{- define "tss.openbaoRole" -}}
{{- .Values.token.openbao.role | default .Values.openbao.role -}}
{{- end }}

{{- define "tss.validate" -}}
{{- if not .Values.terrakube.uiUrl }}{{ fail "terrakube.uiUrl is required" }}{{ end }}
{{- if not .Values.terrakube.organization }}{{ fail "terrakube.organization is required" }}{{ end }}
{{- if not (or .Values.token.existingSecret.name .Values.token.value (include "tss.openbaoAddr" .)) }}
{{- fail "set token.existingSecret.name, token.value or openbao.addr" }}
{{- end }}
{{- if and (not .Values.existingCatalogConfigMap) (not .Values.catalog.templates) }}
{{- fail "catalog.templates is empty; configure at least one template or set existingCatalogConfigMap" }}
{{- end }}
{{- end }}
