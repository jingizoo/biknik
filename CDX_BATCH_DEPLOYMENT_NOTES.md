# CDX Batch deployment in this repo: where the container comes from

## Short answer

CDX Batch does **not** build your container by itself. In this project, Gradle/CIG tooling builds and publishes the image, and Batch jobs reference that published image when runs are launched.

## How image creation is wired here

1. `build.gradle` enables Docker image creation:
   - `cigdocker { createDockerImage = true }`
2. The Python entrypoint binary is defined as `fa_book_xfer` and points to `batch_entrypoint.py`.
3. Batch deployment config exists under `deployments { ... batch("fa-asset-xfer-batch") ... }`.
4. Batch job YAML (`src/main/batch/dev/jobs.yaml` and `src/main/batch/prod/jobs.yaml`) sets a `dockerImage` key (currently a placeholder `<DOCKER_IMAGE_KEY>`). That key must resolve to a real, published image.

## What “Simon is talking about” likely means

There are two distinct flows:

1. **Build/publish container** (CI/Gradle/deployer side)
2. **Run job using container** (CDX Batch side)

Batch only does #2. If image key/tag is missing or not accessible, Batch run fails with image pull errors.

## Deploying to CDX Batch (practical sequence)

1. Build and publish image from this repo (CI pipeline or local Gradle path used by your org).
2. Update Batch YAML to use the actual published image key/tag instead of `<DOCKER_IMAGE_KEY>`.
3. Validate and diff Batch config:
   - `./gradlew batchValidate`
   - `./gradlew batchDiff`
4. Generate publish command:
   - `./gradlew batchPublish`
5. Run the printed deploy command (or use Batch UI Deployer sync).
6. Trigger a run in Batch UI and confirm logs show your container + `batch_entrypoint.py` launch.

## Minimal checklist before first prod run

- Service account has deploy permission.
- Batch sync exists and points at `src/main/batch/<env>/`.
- Docker registry permissions allow Batch runtime principal to pull the image.
- YAML placeholders (`<TEAM>`, `<COMPUTE_GROUP>`, `<DOCKER_IMAGE_KEY>`) are replaced.

