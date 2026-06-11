{{- define "vllm-serving.fullname" -}}
{{- printf "vllm-%s" .Values.model.name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "vllm-serving.labels" -}}
app.kubernetes.io/name: vllm-serving
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app: vllm
model: {{ .Values.model.name }}
{{- end }}

{{- define "vllm-serving.selectorLabels" -}}
app: vllm
model: {{ .Values.model.name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
