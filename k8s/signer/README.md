# Android re-signing job

Shielding (SafeCore) strips the code signature from an AAB/APK. Google Play
rejects an unsigned bundle, so the shielded binary has to be signed again before
it can be published. This image does that signing as a short-lived Kubernetes
Job, driven by KubeSight.

The upload keystore stays in a Kubernetes Secret that only this Job mounts. It
is never uploaded to KubeSight, never written to its database, and never
appears in the Job spec.

## 1. Build and push

```sh
docker build -t <registry>/kubesight-android-signer:1 k8s/signer
docker push  <registry>/kubesight-android-signer:1
```

## 2. Create the keystore Secret

In the namespace the Job will run in (`kubesight` unless configured otherwise):

```sh
kubectl -n kubesight create secret generic android-upload-keystore \
  --from-file=upload.jks=/path/to/upload.jks \
  --from-literal=store-password='…' \
  --from-literal=key-password='…'
```

The three key names are configurable per app (`keystoreKey`, `storePassKey`,
`keyPassKey`) — these are just the defaults.

> The keystore to use is the **upload key**, not an app signing key. With Play
> App Signing, Google re-signs with the real app signing key after upload, and
> an upload key can be reset if it is ever compromised.

## 3. Point the app at it

On the mobile application's registration, set `resignConfig.android`:

```json
{
  "executor": "k8s_job",
  "cluster": "prod",
  "namespace": "kubesight",
  "image": "<registry>/kubesight-android-signer:1",
  "keystoreSecret": "android-upload-keystore",
  "keystoreKey": "upload.jks",
  "keyAlias": "upload",
  "storePassKey": "store-password",
  "keyPassKey": "key-password"
}
```

Optional: `serviceAccount`, `imagePullSecret`, and `callbackUrl` (defaults to
`http://backend-service:5000`, i.e. in-cluster service DNS — the Job talks to
the API directly, not through the ingress).

## What the Job does

1. Pulls the unsigned binary from `/api/mobile-apps/resigns/<id>/source`.
2. `.aab` → `jarsigner` then `jarsigner -verify -strict`.
   `.apk` → `zipalign -p 4` **then** `apksigner sign`, then `apksigner verify`.
3. POSTs the signed binary to `/api/mobile-apps/resigns/<id>/result`.

KubeSight registers the result as a new build linked to the shielded original,
re-checks that a signature is actually present, and only then allows a publish.

Both calls carry a token minted for that one run: it names exactly one resign
and one build, expires in 30 minutes (`RESIGN_TOKEN_MINUTES`), carries no user
identity, and can do nothing else.

## Notes on the tooling

- `apksigner` **cannot** sign an `.aab` — app bundles are JAR-format and take
  `jarsigner`. Using the wrong one is the most common failure here.
- `zipalign` must run **before** `apksigner`, never after; aligning a signed APK
  invalidates the signature.
