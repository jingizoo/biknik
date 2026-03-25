#!/usr/bin/env bash
set -feuo pipefail

kubectl config use-context gke_cig-acctgateway-prod-1_us-east1_ent-acctgateway-1--prod--us-east1
kubectl config get-contexts

kubectl get jobs -l app=fa-asset-xfer -n accounting-gateway
echo
echo
kubectl describe job fa-asset-xfer -n accounting-gateway
