{{- define "kubesight.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "kubesight.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "kubesight.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "kubesight.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/name: {{ include "kubesight.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.global.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "kubesight.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kubesight.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "kubesight.componentLabels" -}}
{{ include "kubesight.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "kubesight.image" -}}
{{- $root := .root -}}
{{- $image := .image -}}
{{- $registry := trimSuffix "/" $root.Values.global.imageRegistry -}}
{{- $repository := $image.repository -}}
{{- if $registry }}{{ printf "%s/%s" $registry $repository }}{{ else }}{{ $repository }}{{ end -}}
{{- if $image.digest }}@{{ $image.digest }}{{ else }}:{{ default $root.Chart.AppVersion $image.tag }}{{ end -}}
{{- end -}}

{{- define "kubesight.secretName" -}}
{{- if eq .Values.deploymentMode "production" -}}
{{- required "secrets.existingSecret is required in production mode" .Values.secrets.existingSecret -}}
{{- else -}}
{{- default (printf "%s-trial" (include "kubesight.fullname" .)) .Values.secrets.existingSecret -}}
{{- end -}}
{{- end -}}

{{- define "kubesight.serviceAccountName" -}}
{{- $root := .root -}}
{{- $component := .component -}}
{{- $configured := index $root.Values.serviceAccounts (printf "%sName" $component) -}}
{{- if $configured -}}
{{- $configured -}}
{{- else if $root.Values.serviceAccounts.create -}}
{{- printf "%s-%s" (include "kubesight.fullname" $root) $component -}}
{{- else -}}
default
{{- end -}}
{{- end -}}

{{- define "kubesight.backendEnv" -}}
- name: KUBESIGHT_ENV
  value: production
- name: FLASK_DEBUG
  value: "false"
- name: AUTH_REQUIRED
  value: "true"
- name: K8S_REAL_MODE
  value: "true"
- name: ALERT_POLICY_SCHEDULER
  value: "false"
- name: CORS_ORIGINS
  value: {{ .Values.config.corsOrigins | quote }}
- name: PUBLIC_BASE_URL
  value: {{ .Values.config.publicUrl | quote }}
- name: DB_POOL_SIZE
  value: {{ .Values.config.databasePoolSize | quote }}
- name: DB_MAX_OVERFLOW
  value: {{ .Values.config.databaseMaxOverflow | quote }}
- name: SESSION_ACCESS_MINUTES
  value: {{ .Values.config.sessionAccessMinutes | quote }}
- name: SESSION_REFRESH_DAYS
  value: {{ .Values.config.sessionRefreshDays | quote }}
{{- if .Values.objectStorage.enabled }}
- name: APPLICATION_ANALYSIS_ARTIFACT_ROOT
  value: {{ printf "%s/application-analysis" .Values.objectStorage.mountPath | quote }}
- name: MOBILE_ARTIFACT_DIR
  value: {{ printf "%s/mobile" .Values.objectStorage.mountPath | quote }}
{{- end }}
{{- with .Values.config.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "kubesight.imagePullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "kubesight.csiVolume" -}}
{{- if .Values.secrets.secretProviderClass }}
- name: secrets-store
  csi:
    driver: secrets-store.csi.k8s.io
    readOnly: true
    volumeAttributes:
      secretProviderClass: {{ .Values.secrets.secretProviderClass | quote }}
{{- end }}
{{- end -}}

{{- define "kubesight.csiMount" -}}
{{- if .Values.secrets.secretProviderClass }}
- name: secrets-store
  mountPath: /var/run/secrets/kubesight
  readOnly: true
{{- end }}
{{- end -}}

{{- define "kubesight.artifactClaimName" -}}
{{- if .Values.objectStorage.existingClaim -}}
{{- .Values.objectStorage.existingClaim -}}
{{- else -}}
{{- printf "%s-artifacts" (include "kubesight.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "kubesight.waitForMigrations" -}}
{{- if .Values.migrations.enabled }}
initContainers:
  - name: wait-for-migrations
    image: {{ include "kubesight.image" (dict "root" . "image" .Values.images.backend) | quote }}
    imagePullPolicy: {{ .Values.images.backend.pullPolicy }}
    command: ["sh", "-ec"]
    args:
      - until python manage.py status >/dev/null 2>&1; do sleep 5; done
    securityContext:
      {{- toYaml .Values.containerSecurityContext | nindent 6 }}
    envFrom:
      - secretRef:
          name: {{ include "kubesight.secretName" . }}
    resources:
      requests: {cpu: 25m, memory: 64Mi}
      limits: {cpu: 250m, memory: 256Mi}
    volumeMounts:
      - {name: tmp, mountPath: /tmp}
      {{- include "kubesight.csiMount" . | nindent 6 }}
{{- end }}
{{- end -}}
