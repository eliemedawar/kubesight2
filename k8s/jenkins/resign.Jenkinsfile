// KubeSight signing job — signs a shielded binary and archives it. No deploy.
//
// This is the old release pipeline with the deployment removed. SafeCore strips
// the code signature, so the shielded binary has to be signed again before any
// store will take it. Publishing now happens in KubeSight, so this job's only
// output is an archived, signed artifact.
//
// KubeSight uploads the unsigned binary with the trigger, as the `apkfile`
// FILE parameter — Jenkins drops it into the workspace under that name. Nothing
// here calls back to KubeSight; it only archives the signed file, which
// KubeSight then pulls off the build.
//
// Point one KubeSight app at this job per platform (Edit application → Signing).

pipeline {
    agent none

    parameters {
        // The unsigned binary, uploaded by KubeSight with the trigger.
        // stashedFile comes from the File Parameters plugin — the classic
        // `file` parameter does not reach a Pipeline workspace (JENKINS-27413).
        // Each stage recovers it with `unstash 'apkfile'`, which is how the
        // existing areeba release pipeline already receives its binary.
        stashedFile(name: 'apkfile', description: 'Unsigned binary (set by KubeSight)')
        choice(name: 'PLATFORM', choices: ['android', 'ios'], description: 'Which signer to run')
        string(name: 'ANDROID_EXT', defaultValue: 'aab', description: 'aab or apk')
        string(name: 'KEY_ALIAS', defaultValue: 'upload', description: 'Android keystore alias')
        string(name: 'PROV_PROFILE', defaultValue: 'comareebazakyapplb_AppStore.mobileprovision', description: 'iOS provisioning profile name')
        text(name: 'fastenvANDROID', defaultValue: '', description: 'Android env file contents')
        text(name: 'fastenvIOS', defaultValue: '', description: 'iOS env file contents')
    }

    options {
        timeout(time: 30, unit: 'MINUTES')
        disableConcurrentBuilds()
    }

    stages {

        stage('Sign Android') {
            when { equals expected: 'android', actual: params.PLATFORM }
            // Android signing is pure Java — it does not need the Mac.
            agent { label 'master' }
            steps {
                cleanWs()
                unstash 'apkfile'
                withCredentials([
                    file(credentialsId: 'android-upload-keystore', variable: 'KEYSTORE'),
                    string(credentialsId: 'android-keystore-password', variable: 'STORE_PASS'),
                    string(credentialsId: 'android-key-password', variable: 'KEY_PASS')
                ]) {
                    sh '''
                        set -eu
                        export ANDROID_HOME=${ANDROID_HOME:-/opt/android-sdk}
                        mkdir -p signed

                        echo "==> Binary uploaded by KubeSight"
                        [ -f apkfile ] || { echo "apkfile missing from workspace" >&2; exit 1; }
                        mv apkfile "unsigned.${ANDROID_EXT}"
                        echo "    $(wc -c < unsigned.${ANDROID_EXT}) bytes"

                        if [ "${ANDROID_EXT}" = "aab" ]; then
                            # App bundles are JAR-format: apksigner CANNOT sign them.
                            echo "==> Signing app bundle (jarsigner)"
                            jarsigner -verbose:summary \
                                -keystore "${KEYSTORE}" \
                                -storepass:env STORE_PASS \
                                -keypass:env KEY_PASS \
                                -digestalg SHA-256 -sigalg SHA256withRSA \
                                -signedjar "signed/app-release.aab" "unsigned.aab" "${KEY_ALIAS}"

                            echo "==> Verifying"
                            jarsigner -verify -strict "signed/app-release.aab"
                        else
                            # zipalign MUST run before apksigner — aligning a signed
                            # APK invalidates the signature.
                            echo "==> Aligning"
                            "${ANDROID_HOME}/build-tools/34.0.0/zipalign" -p -f 4 \
                                "unsigned.apk" "aligned.apk"

                            echo "==> Signing APK (apksigner)"
                            printf '%s' "${STORE_PASS}" > .storepass
                            printf '%s' "${KEY_PASS}"  > .keypass
                            "${ANDROID_HOME}/build-tools/34.0.0/apksigner" sign \
                                --ks "${KEYSTORE}" \
                                --ks-key-alias "${KEY_ALIAS}" \
                                --ks-pass "file:.storepass" \
                                --key-pass "file:.keypass" \
                                --out "signed/app-release.apk" "aligned.apk"
                            rm -f .storepass .keypass

                            echo "==> Verifying"
                            "${ANDROID_HOME}/build-tools/34.0.0/apksigner" verify --print-certs \
                                "signed/app-release.apk"
                        fi
                    '''
                }
                archiveArtifacts artifacts: 'signed/*.aab, signed/*.apk', fingerprint: true, onlyIfSuccessful: true
            }
        }

        stage('Sign iOS') {
            when { equals expected: 'ios', actual: params.PLATFORM }
            // codesign and the keychain are macOS-only — this stage cannot move.
            agent { label 'mac' }
            steps {
                cleanWs()
                unstash 'apkfile'
                withCredentials([
                    usernamePassword(
                        credentialsId: 'MacDevops',
                        usernameVariable: 'devopsUser',
                        passwordVariable: 'DevopsPass'
                    )
                ]) {
                    writeFile file: 'ios_env', text: params.fastenvIOS
                    sh '''
                        set -euo pipefail

                        export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
                        export LC_ALL=en_US.UTF-8
                        export LANG=en_US.UTF-8

                        security unlock-keychain -p "$DevopsPass" login.keychain

                        set -a
                        . ./ios_env
                        set +a

                        echo "==> IPA uploaded by KubeSight"
                        [ -f apkfile ] || { echo "apkfile missing from workspace" >&2; exit 1; }
                        mv apkfile shielded.ipa
                        echo "    $(wc -c < shielded.ipa) bytes"

                        PROV_PROFILE="$HOME/Library/MobileDevice/Provisioning Profiles/${PROV_PROFILE}"
                        if [ ! -f "$PROV_PROFILE" ]; then
                            echo "Provisioning profile not found at: $PROV_PROFILE" >&2
                            exit 1
                        fi

                        WORKDIR="resign_workspace"
                        rm -rf "$WORKDIR" && mkdir -p "$WORKDIR"
                        cp shielded.ipa "$WORKDIR/" && cd "$WORKDIR"

                        echo "[+] Extract entitlements from the provisioning profile"
                        security cms -D -i "$PROV_PROFILE" > provision.plist
                        /usr/libexec/PlistBuddy -x -c 'Print :Entitlements' provision.plist > entitlements.plist

                        echo "[+] Force NFC entitlement to TAG only"
                        /usr/libexec/PlistBuddy -c "Delete :com.apple.developer.nfc.readersession.formats" entitlements.plist 2>/dev/null || true
                        /usr/libexec/PlistBuddy -c "Add :com.apple.developer.nfc.readersession.formats array" entitlements.plist
                        /usr/libexec/PlistBuddy -c "Add :com.apple.developer.nfc.readersession.formats:0 string TAG" entitlements.plist

                        echo "[+] Unzip IPA"
                        unzip -q shielded.ipa
                        APP_NAME=$(ls Payload | grep ".app$" | sed 's/.app//')
                        APP_PATH="Payload/${APP_NAME}.app"
                        [ -d "$APP_PATH" ] || { echo "App bundle not found in Payload" >&2; exit 1; }

                        echo "[+] Remove old code signature"
                        rm -rf "$APP_PATH/_CodeSignature"

                        echo "[+] Add required privacy keys"
                        INFO_PLIST="$APP_PATH/Info.plist"
                        for KEY in NSHealthShareUsageDescription NSHealthUpdateUsageDescription; do
                            DESC="This app does not access health data. This permission is required by an SDK dependency."
                            /usr/libexec/PlistBuddy -c "Add :$KEY string '$DESC'" "$INFO_PLIST" 2>/dev/null || \
                            /usr/libexec/PlistBuddy -c "Set :$KEY '$DESC'" "$INFO_PLIST"
                        done

                        echo "[+] Embed provisioning profile"
                        cp "$PROV_PROFILE" "$APP_PATH/embedded.mobileprovision"

                        EXEC_NAME=$(/usr/libexec/PlistBuddy -c "Print :CFBundleExecutable" "$INFO_PLIST")
                        EXEC_PATH="$APP_PATH/$EXEC_NAME"
                        [ -f "$EXEC_PATH" ] || { echo "Main executable not found: $EXEC_PATH" >&2; exit 1; }

                        echo "[+] Re-sign frameworks and dylibs"
                        if [ -d "$APP_PATH/Frameworks" ]; then
                            shopt -s nullglob
                            for fw in "$APP_PATH/Frameworks/"*.framework "$APP_PATH/Frameworks/"*.dylib; do
                                [ -e "$fw" ] && /usr/bin/codesign --verbose --force --sign "$ZAKY_CODE_SIGN_ID" "$fw"
                            done
                            shopt -u nullglob
                        fi

                        echo "[+] Re-sign executable and bundle"
                        /usr/bin/codesign --verbose --force --sign "$ZAKY_CODE_SIGN_ID" --entitlements "entitlements.plist" "$EXEC_PATH"
                        /usr/bin/codesign --verbose --force --sign "$ZAKY_CODE_SIGN_ID" --entitlements "entitlements.plist" "$APP_PATH"

                        echo "[+] Verify signature"
                        codesign --verify --deep --strict --verbose=2 "$APP_PATH"

                        echo "[+] Repackage"
                        mkdir -p "${WORKSPACE}/signed"
                        RESIGNED="${WORKSPACE}/signed/app-release.ipa"
                        rm -f "$RESIGNED"
                        ZIP_ITEMS=("Payload")
                        for d in SwiftSupport Symbols Signatures; do
                            [ -d "$d" ] && ZIP_ITEMS+=("$d")
                        done
                        zip -qry "$RESIGNED" "${ZIP_ITEMS[@]}"
                        [ -f "$RESIGNED" ] || { echo "Failed to create resigned IPA" >&2; exit 1; }

                        echo "==> Signed IPA ready: $RESIGNED"
                    '''
                }
                archiveArtifacts artifacts: 'signed/*.ipa', fingerprint: true, onlyIfSuccessful: true
            }
        }
    }

    post {
        // The binary is archived and KubeSight has pulled it — do not leave a
        // signed store-ready artifact sitting in a workspace.
        cleanup {
            node('master') { cleanWs() }
        }
    }
}
