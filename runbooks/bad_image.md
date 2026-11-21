category: bad_image
# Runbook: image pull failure

Signals: ImagePullBackOff / ErrImagePull, "manifest unknown" or "not found" in events, a recent revision
with a new image tag.

1. Confirm the failing tag in events and the rollout history.
2. **Roll back** to the previous revision whose image is known to run.
3. Ask the owning team to publish the tag before rolling forward again.
