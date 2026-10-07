{{- define "agent-sandbox-bench.labels" -}}
app: {{ .Values.app }}
test_id: {{ .Values.testId }}
runtime_class: {{ .Values.runtimeClassName }}
variant: {{ .Values.variant }}
{{- end }}

{{- define "agent-sandbox-bench.podTemplate" -}}
metadata:
  labels:
    {{- include "agent-sandbox-bench.labels" . | nindent 4 }}
spec:
  runtimeClassName: {{ .Values.runtimeClassName }}
  containers:
    - name: {{ .Values.containerName }}
      image: {{ .Values.image }}
      imagePullPolicy: IfNotPresent
      ports:
        - containerPort: {{ .Values.containerPort }}
      {{- with .Values.resources }}
      resources:
        {{- toYaml . | nindent 8 }}
      {{- end }}
{{- end }}
