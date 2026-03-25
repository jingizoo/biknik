#!/usr/bin/env bash
set -feuo pipefail

kubectl config use-context gke_cig-acctgateway-dev-1_us-east1_ent-acctgateway-1--dev--us-east1

POD=$(kubectl get pods -l app=fa-asset-xfer -n accounting-gateway -o jsonpath='{.items[0].metadata.name}')
echo "Connecting to pod: ${POD}"
kubectl exec -it "${POD}" -n accounting-gateway -- /bin/bash
