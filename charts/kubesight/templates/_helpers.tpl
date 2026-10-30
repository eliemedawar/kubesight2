{{/* Prefix for cluster-wide objects and the generated Secret. */}}
{{- define "kubesight.fullname" -}}
{{- if contains "kubesight" .Release.Name -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-kubesight" .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* Labels for every object (dict "root" . "module" name). */}}
{{- define "kubesight.labels" -}}
app.kubernetes.io/part-of: kubesight
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version }}
{{- with include "kubesight.podLabels" . }}
{{ . }}
{{- end }}
{{- range $key, $value := .root.Values.commonLabels }}
{{ $key }}: {{ $value | quote }}
{{- end }}
{{- end -}}

{{/*
The module label alone, for pod templates too (dict "root" . "module" name).
commonLabels stay off pods: a new change ID would otherwise restart them all.
*/}}
{{- define "kubesight.podLabels" -}}
{{- with .root.Values.moduleLabelKey -}}
{{ . }}: {{ $.module }}
{{- end -}}
{{- end -}}

{{/* The backend's namespace: the one the release is installed into. */}}
{{- define "kubesight.namespace" -}}
{{- .Release.Namespace -}}
{{- end -}}

{{- define "kubesight.ciEnabled" -}}
{{- if .Values.ci.enabled }}true{{ end -}}
{{- end -}}

{{- define "kubesight.buildkitEnabled" -}}
{{- if and .Values.ci.enabled .Values.buildkit.enabled }}true{{ end -}}
{{- end -}}

{{/* registry/repository:tag for a KubeSight image (dict "root" . "repo" name). */}}
{{- define "kubesight.image" -}}
{{- $tag := required "image.tag is required: the KubeSight release to run, e.g. --set image.tag=1.2.0" .root.Values.image.tag -}}
{{- printf "%s/%s:%s" .root.Values.image.registry .repo $tag -}}
{{- end -}}

{{- define "kubesight.hermesImage" -}}
{{- printf "%s/%s:%s" .Values.image.registry .Values.hermes.image.repository .Values.hermes.image.tag -}}
{{- end -}}

{{- define "kubesight.secretName" -}}
{{- .Values.secrets.existingSecret | default (printf "%s-secrets" (include "kubesight.fullname" .)) -}}
{{- end -}}

{{- define "kubesight.publicUrl" -}}
{{- $host := required "publicHost is required: the host name people open, e.g. kubesight.areeba.com" .Values.publicHost -}}
{{- $ingressTls := and .Values.ingress.enabled (ne .Values.ingress.tls.mode "none") -}}
{{- $frontendTls := and .Values.frontend.serveApi .Values.frontend.tls.secretName -}}
{{- if .Values.publicScheme -}}
{{- printf "%s://%s" .Values.publicScheme $host -}}
{{- else if or $ingressTls $frontendTls -}}
{{- printf "https://%s" $host -}}
{{- else -}}
{{- printf "http://%s" $host -}}
{{- end -}}
{{- end -}}

{{- define "kubesight.backendUrl" -}}
{{- printf "http://backend-service.%s.svc.cluster.local:5000" (include "kubesight.namespace" .) -}}
{{- end -}}

{{- define "kubesight.hermesUrl" -}}
{{- printf "http://kubesight-hermes.%s.svc.cluster.local:8642/v1/chat/completions" (include "kubesight.namespace" .) -}}
{{- end -}}

{{- define "kubesight.imagePullSecrets" -}}
{{- if .Values.imagePullSecret.name }}
imagePullSecrets:
  - name: {{ .Values.imagePullSecret.name }}
{{- end }}
{{- end -}}

{{/* The claim a volume block resolves to (dict "cfg" block "name" default). */}}
{{- define "kubesight.claimName" -}}
{{- .cfg.existingClaim | default .name -}}
{{- end -}}

{{/*
A PersistentVolumeClaim for a volume block, plus an NFS PersistentVolume when
the block names an export. Nothing when the block names an existing claim.
Both are kept on uninstall: they hold data.
(dict "root" . "cfg" block "name" claim "namespace" ns "purpose" label)
*/}}
{{- define "kubesight.volume" -}}
{{- $cfg := .cfg -}}
{{- if not $cfg.existingClaim }}
{{- $nfs := and $cfg.nfs $cfg.nfs.server $cfg.nfs.path }}
{{- $pvName := printf "%s-%s" (include "kubesight.fullname" .root) .name }}
{{- if $nfs }}
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: {{ $pvName }}
  labels:
    {{- include "kubesight.labels" (dict "root" .root "module" .module) | nindent 4 }}
    kubesight.io/purpose: {{ .purpose }}
  annotations:
    helm.sh/resource-policy: keep
spec:
  capacity:
    storage: {{ $cfg.size }}
  volumeMode: Filesystem
  accessModes: [{{ $cfg.accessMode }}]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  {{- with $cfg.nfs.mountOptions }}
  mountOptions:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  claimRef:
    namespace: {{ .namespace }}
    name: {{ .name }}
  nfs:
    server: {{ $cfg.nfs.server }}
    path: {{ $cfg.nfs.path }}
{{- end }}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ .name }}
  namespace: {{ .namespace }}
  labels:
    {{- include "kubesight.labels" (dict "root" .root "module" .module) | nindent 4 }}
    kubesight.io/purpose: {{ .purpose }}
  annotations:
    helm.sh/resource-policy: keep
spec:
  accessModes: [{{ $cfg.accessMode }}]
  resources:
    requests:
      storage: {{ $cfg.size }}
  {{- if $nfs }}
  storageClassName: ""
  volumeName: {{ $pvName }}
  {{- else if eq $cfg.storageClass "-" }}
  storageClassName: ""
  {{- else if $cfg.storageClass }}
  storageClassName: {{ $cfg.storageClass }}
  {{- end }}
{{- end }}
{{- end -}}
