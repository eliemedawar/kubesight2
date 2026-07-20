#!/bin/sh
# Re-sign an Android binary that lost its signature to shielding.
#
# Pulls the unsigned artifact from KubeSight, signs it with the mounted upload
# keystore, verifies the result, and posts it back. The keystore and its
# passwords come from a Kubernetes Secret mounted into this pod — they are
# never sent to KubeSight and never appear in the Job spec.
#
# A .aab is JAR-signed with jarsigner; apksigner cannot sign app bundles. A
# .apk is zipaligned first and then signed with apksigner, which is the only
# order that produces a valid v2/v3 signature.

set -eu

: "${KUBESIGHT_URL:?}" "${RESIGN_ID:?}" "${RESIGN_TOKEN:?}" "${ARTIFACT_TYPE:?}"
: "${KEYSTORE_PATH:?}" "${KEY_ALIAS:?}" "${STORE_PASS:?}" "${KEY_PASS:?}"

AUTH="Authorization: Bearer ${RESIGN_TOKEN}"
IN="/work/in.${ARTIFACT_TYPE}"
OUT="/work/out.${ARTIFACT_TYPE}"

echo "==> Fetching unsigned ${ARTIFACT_TYPE} from KubeSight"
curl -fsSL -H "${AUTH}" \
  "${KUBESIGHT_URL}/api/mobile-apps/resigns/${RESIGN_ID}/source" -o "${IN}"
echo "    $(wc -c < "${IN}") bytes"

case "${ARTIFACT_TYPE}" in
  aab)
    echo "==> Signing app bundle (jarsigner)"
    # -storepass:env / -keypass:env keep the passwords out of the process
    # arguments, where any other container in the namespace could read them.
    STORE_PASS="${STORE_PASS}" KEY_PASS="${KEY_PASS}" \
      jarsigner -verbose:summary \
        -keystore "${KEYSTORE_PATH}" \
        -storepass:env STORE_PASS \
        -keypass:env KEY_PASS \
        -digestalg SHA-256 -sigalg SHA256withRSA \
        -signedjar "${OUT}" "${IN}" "${KEY_ALIAS}"

    echo "==> Verifying"
    jarsigner -verify -strict "${OUT}"
    ;;

  apk)
    echo "==> Aligning (must precede signing)"
    zipalign -p -f 4 "${IN}" /work/aligned.apk

    echo "==> Signing APK (apksigner)"
    printf '%s' "${STORE_PASS}" > /work/.storepass
    printf '%s' "${KEY_PASS}" > /work/.keypass
    apksigner sign \
      --ks "${KEYSTORE_PATH}" \
      --ks-key-alias "${KEY_ALIAS}" \
      --ks-pass "file:/work/.storepass" \
      --key-pass "file:/work/.keypass" \
      --out "${OUT}" /work/aligned.apk
    rm -f /work/.storepass /work/.keypass

    echo "==> Verifying"
    apksigner verify --print-certs "${OUT}"
    ;;

  *)
    echo "Unsupported artifact type: ${ARTIFACT_TYPE}" >&2
    exit 2
    ;;
esac

echo "==> Returning signed binary to KubeSight"
curl -fsS -X POST -H "${AUTH}" \
  -F "file=@${OUT};filename=app-release.${ARTIFACT_TYPE}" \
  "${KUBESIGHT_URL}/api/mobile-apps/resigns/${RESIGN_ID}/result" > /dev/null

echo "==> Done"
