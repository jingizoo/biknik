#!/usr/bin/env bash
set -feuo pipefail

kubectl config use-context gke_cig-acctgateway-dev-1_us-east1_ent-acctgateway-1--dev--us-east1

echo "=== Jobs ==="
kubectl get jobs -l app=fa-asset-xfer -n accounting-gateway
echo
echo "=== Pods ==="
kubectl get pods -l app=fa-asset-xfer -n accounting-gateway
echo
echo "=== Recent Pod Logs ==="
kubectl logs -l app=fa-asset-xfer -n accounting-gateway --tail=50
