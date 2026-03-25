#!/usr/bin/env bash
set -feuo pipefail

kubectl config use-context gke_cig-acctgateway-prod-1_us-east1_ent-acctgateway-1--prod--us-east1

echo "=== Tailing logs for fa-asset-xfer ==="
kubectl logs -f job/fa-asset-xfer -n accounting-gateway
