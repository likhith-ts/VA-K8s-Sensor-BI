#!/usr/bin/env bash
# Copy locally built images into the cluster's containerd.
#
# Docker Desktop's kubeadm cluster shares the Docker image store, but its
# kind-based cluster (node providerID kind://...) does not, and its node
# containers are hidden from `docker ps`. This starts a short-lived privileged
# pod that uses the node's own `ctr` binary to import `docker save` output.
# With plain kind, `kind load docker-image` does the same job.
set -euo pipefail
IMAGES=("$@")
NODE=$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')

kubectl -n kube-system delete pod envbi-image-loader --ignore-not-found --wait=true >/dev/null
kubectl -n kube-system apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: envbi-image-loader
spec:
  nodeName: ${NODE}
  restartPolicy: Never
  tolerations: [{ operator: Exists }]
  containers:
    - name: loader
      image: debian:trixie-slim
      command: ["sleep", "600"]
      securityContext: { privileged: true }
      volumeMounts:
        - { name: bin, mountPath: /hostbin, readOnly: true }
        - { name: containerd, mountPath: /run/containerd }
  volumes:
    - { name: bin, hostPath: { path: /usr/local/bin } }
    - { name: containerd, hostPath: { path: /run/containerd } }
YAML
kubectl -n kube-system wait --for=condition=Ready pod/envbi-image-loader --timeout=180s >/dev/null

for img in "${IMAGES[@]}"; do
  echo "loading $img"
  docker save "$img" | kubectl -n kube-system exec -i envbi-image-loader -- \
    /hostbin/ctr -n k8s.io images import --all-platforms=false -
done
kubectl -n kube-system delete pod envbi-image-loader --wait=false >/dev/null
