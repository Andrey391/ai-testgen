{{- define "testgen.name" -}}{{ .Chart.Name }}{{- end -}}

{{- define "testgen.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}{{ .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else -}}{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- end -}}

{{- define "testgen.labels" -}}
app.kubernetes.io/name: {{ include "testgen.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "testgen.selector" -}}
app.kubernetes.io/name: {{ include "testgen.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "testgen.secretName" -}}
{{- if .Values.existingSecret -}}{{ .Values.existingSecret }}{{- else -}}{{ include "testgen.fullname" . }}{{- end -}}
{{- end -}}

{{/* Where the pods get their settings: the chart's ConfigMap and the Secret. */}}
{{- define "testgen.envFrom" -}}
- configMapRef:
    name: {{ include "testgen.fullname" . }}-env
- secretRef:
    name: {{ include "testgen.secretName" . }}
{{- end -}}

{{/* Local folders are only a cache of the database and S3; /dev/shm for Chromium; /tmp and the home
folder are all the browsers, npx and Python write besides - so the root filesystem may be read-only. */}}
{{- define "testgen.volumes" -}}
- name: cache
  emptyDir: {}
- name: dshm
  emptyDir:
    medium: Memory
    sizeLimit: {{ .Values.shmSize }}
- name: tmp
  emptyDir: {}
- name: home
  emptyDir: {}
{{- end -}}

{{- define "testgen.mounts" -}}
- name: cache
  mountPath: /cache
- name: dshm
  mountPath: /dev/shm
- name: tmp
  mountPath: /tmp
- name: home
  mountPath: /home/testgen
{{- end -}}

{{- define "testgen.scheduling" -}}
{{- with .Values.podSecurityContext }}
securityContext:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
