#!/usr/bin/env bash
set -feuo pipefail

kubectl config use-context gke_cig-acctgateway-dev-1_us-east1_ent-acctgateway-1--dev--us-east1
kubectl config get-contexts

# this one gives output on why things are failing.
# kubectl get deployment acctgateway-userdata -o yaml
kubectl get jobs -l app=fa-asset-xfer
echo
echo
kubectl describe job fa-asset-xfer
