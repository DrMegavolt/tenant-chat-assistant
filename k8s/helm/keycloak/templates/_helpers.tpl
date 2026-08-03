{{/*
Names and labels. `tenantchat.keycloak.*` selectors are the stable identity the
NetworkPolicies in this chart and in k8s/network-policies.yaml select on.
*/}}

{{- define "tenantchat.keycloak.name" -}}
{{- default "keycloak" .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "tenantchat.keycloak.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- include "tenantchat.keycloak.name" . -}}
{{- end -}}
{{- end -}}

{{- define "tenantchat.keycloak.postgresName" -}}
{{- printf "%s-postgres" (include "tenantchat.keycloak.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Labels every resource carries. `app.kubernetes.io/instance` is deliberately
absent: it belongs to the selector label sets, and emitting it from both would
produce a duplicate YAML key wherever the two are used together.
*/}}
{{- define "tenantchat.keycloak.commonLabels" -}}
app.kubernetes.io/part-of: tenant-chat
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "tenantchat.keycloak.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tenantchat.keycloak.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "tenantchat.keycloak.postgresSelectorLabels" -}}
app.kubernetes.io/name: {{ include "tenantchat.keycloak.postgresName" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "tenantchat.keycloak.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "tenantchat.keycloak.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
The digest-pinned image reference. A tag alone is mutable, so the chart refuses
to render without a digest rather than deploying an image nobody reviewed.
*/}}
{{- define "tenantchat.keycloak.image" -}}
{{- $image := .Values.keycloak.image -}}
{{- $digest := required "keycloak.image.digest is required: record the digest for keycloak.image.tag and set it" $image.digest -}}
{{- printf "%s:%s@%s" $image.repository $image.tag $digest -}}
{{- end -}}

{{- define "tenantchat.keycloak.postgresImage" -}}
{{- $image := .Values.database.image -}}
{{- $digest := required "database.image.digest is required" $image.digest -}}
{{- printf "%s:%s@%s" $image.repository $image.tag $digest -}}
{{- end -}}

{{/*
The browser-facing Keycloak base URL, with any trailing slash removed so the
issuer this chart configures is byte-identical to the one oauth2-proxy compares
against the `iss` claim.
*/}}
{{- define "tenantchat.keycloak.publicUrl" -}}
{{- $url := required "keycloak.publicUrl is required (for example https://auth.tenantchat.local)" .Values.keycloak.publicUrl -}}
{{- trimSuffix "/" $url -}}
{{- end -}}

{{- define "tenantchat.keycloak.gatewayUrl" -}}
{{- $url := required "realm.gatewayUrl is required (the origin serving /admin/)" .Values.realm.gatewayUrl -}}
{{- trimSuffix "/" $url -}}
{{- end -}}

{{- define "tenantchat.keycloak.issuerUrl" -}}
{{- printf "%s/realms/%s" (include "tenantchat.keycloak.publicUrl" .) .Values.keycloak.realm -}}
{{- end -}}

{{/*
In-cluster base URL. oauth2-proxy redeems codes and fetches JWKS here, so the
backchannel never leaves the cluster or depends on split-horizon DNS.
*/}}
{{- define "tenantchat.keycloak.internalUrl" -}}
{{- printf "http://%s.%s.svc.cluster.local:8080" (include "tenantchat.keycloak.fullname" .) .Release.Namespace -}}
{{- end -}}

{{- define "tenantchat.keycloak.databaseHost" -}}
{{- if .Values.database.embedded -}}
{{- printf "%s:5432" (include "tenantchat.keycloak.postgresName" .) -}}
{{- else -}}
{{- $host := required "database.external.host is required when database.embedded is false" .Values.database.external.host -}}
{{- printf "%s:%v" $host .Values.database.external.port -}}
{{- end -}}
{{- end -}}
