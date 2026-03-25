# fa-asset-xfer — botora (dev) k8s config

## Files
- `deployment.yaml` — K8s Job definition (batch, runs with --dry-run)
- `configmap.yaml` — Non-secret env vars (proxy URLs, cert paths, log level)
- `config.sh` — Set kubectl context and check job status
- `joblookup.sh` — List jobs, pods, and recent logs
- `taillog.sh` — Tail job logs in real-time
- `directssh.sh` — Exec into a running pod

## Cluster
- Project: `cig-acctgateway-dev-1`
- Cluster: `ent-acctgateway-1--dev--us-east1`
- Namespace: `accounting-gateway`

## Secrets
Secrets are synced from Holocron Vault (`service-accounts/fa-asset-xfer/dev`) into K8s Secret `fa-asset-xfer`:
- `FUSION_JWT`
- `INETPROXY_USER`
- `INETPROXY_PASSWD`
