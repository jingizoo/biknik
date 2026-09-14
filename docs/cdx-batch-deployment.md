# CDX Batch deployment: where the container comes from

> **State of the world.** Today the prod path is the **GKE Job** under
> `k8s/fa-asset-xfer/{prod,botora}/`. CDX Batch is a parallel deployment
> surface that lives under `src/main/batch/<env>/` and is being added in a
> companion change. This note explains how the Batch flow is wired so the
> two paths can co-exist (and so the Batch path can be brought up cleanly).

## Short answer

CDX Batch does **not** build your container by itself. In this project,
Gradle / CIG tooling builds and publishes the image, and Batch jobs
reference that published image when runs are launched.

## Build vs run: two flows

There are two distinct flows. Confusing them is the most common cause of
"image pull failed" errors on a first Batch run.

1. **Build / publish container** (CI / Gradle / deployer side).
2. **Run job using container** (CDX Batch side).

Batch only does #2. If the image key/tag is missing or the Batch runtime
principal cannot pull it, the run fails immediately.

## How image creation is wired here

1. `build.gradle` enables Docker image creation via the CIG plugin:
   `cigdocker { createDockerImage = true }`.
2. The Python entrypoint binary is declared in `build.gradle` via
   `startScripts.pythonBinary("fa_book_xfer") { main = "batch_entrypoint.py" }`.
   That declaration is what links the gradle build artifact to the Python
   module the container will execute.
3. Batch deployment config exists under
   `deployments { ... batch("fa-asset-xfer-batch") ... }`.
4. The Batch job YAML (once added at `src/main/batch/<env>/jobs.yaml`) sets
   a `dockerImage` key. The placeholder `<DOCKER_IMAGE_KEY>` must be
   resolved to a real, published image before the YAML is deployed.

> The `src/main/batch/<env>/jobs.yaml` files referenced above are added
> in the companion PR for the Batch templates. Until that lands, the
> deploy steps below are forward-looking.

## Deploying to CDX Batch (practical sequence)

1. Build and publish the image from this repo (CI pipeline or the local
   Gradle path used by your org).
2. Update the Batch YAML to use the actual published image key/tag in
   place of `<DOCKER_IMAGE_KEY>`.
3. Validate and diff Batch config:
   - `./gradlew batchValidate`
   - `./gradlew batchDiff`
4. Generate the publish command:
   - `./gradlew batchPublish`
5. Run the printed deploy command (or use the Batch UI Deployer sync).
6. Trigger a run in the Batch UI and confirm logs show your container +
   `batch_entrypoint.py` launching.

## Where env / inputs come from

- **GKE Job path (current prod):** env vars come from
  `k8s/fa-asset-xfer/<env>/configmap.yaml` and the Holocron-synced
  `fa-asset-xfer` Secret. The job command-line is fixed in
  `deployment.yaml`.
- **CDX Batch path:** env / inputs are declared as `inputVariables` in
  `src/main/batch/<env>/jobs.yaml` and resolved at run-trigger time
  (Batch UI or API). The same secrets (Fusion JWT, inet-proxy creds) need
  to be available to the Batch runtime principal.

## Minimal checklist before the first prod Batch run

- Service account has deploy permission for the target Batch environment.
- Batch sync exists and points at `src/main/batch/<env>/`.
- Docker registry permissions allow the Batch runtime principal to pull
  the image.
- YAML placeholders (`<TEAM>`, `<COMPUTE_GROUP>`, `<DOCKER_IMAGE_KEY>`)
  are replaced with the real values for that environment.
- Secrets (`FUSION_JWT`, `INETPROXY_USER`, `INETPROXY_PASSWD`) are
  reachable from the Batch runtime — same source as the GKE path.
