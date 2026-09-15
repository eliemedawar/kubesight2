"""An in-memory repository, for testing everything above the source port.

Registering a provider rather than mocking ``evidence.collect`` means the tests
exercise the real thing: the tree walk, the file-priority budget, the monorepo
scoping, the redaction, and the deterministic facts are all computed from an
actual (fake) repository. Only the network is absent.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from api.services.ci.source import (
    CheckoutSpec,
    RepositoryRef,
    RevisionOption,
    SourceError,
    TreeListing,
    register_provider,
)

PROVIDER = "fake"


class FakeSourceProvider:
    """A repository that is just a dict of path -> content."""

    provider = PROVIDER

    def __init__(self) -> None:
        self.files: Dict[str, str] = {}
        self.truncated = False
        self.fail_with: Optional[str] = None
        self.reads: List[str] = []

    def load(self, files: Dict[str, str], *, truncated: bool = False) -> None:
        self.files = dict(files)
        self.truncated = truncated
        self.fail_with = None
        self.reads = []

    def parse_repository_url(self, url: str) -> RepositoryRef:
        cleaned = str(url or "").rstrip("/")
        parts = [p for p in cleaned.split("/") if p][-2:]
        if len(parts) != 2:
            raise ValueError("A fake repository URL needs a workspace and a name.")
        name = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
        return RepositoryRef(provider=PROVIDER, url=cleaned, workspace=parts[0], name=name)

    def list_revisions(self, ref, credential) -> List[RevisionOption]:
        return [RevisionOption(value="main", label="Branch — main", kind="branch")]

    def list_tree(self, ref, credential, revision: str) -> TreeListing:
        if self.fail_with:
            raise SourceError(self.fail_with)
        return TreeListing(
            revision=revision or "main",
            paths=sorted(self.files),
            truncated=self.truncated,
        )

    def read_file(self, ref, credential, revision: str, path: str) -> str:
        self.reads.append(path)
        if path not in self.files:
            raise SourceError(f"'{path}' was not found.")
        return self.files[path]

    def verify_access(self, ref, credential) -> dict:
        return {"ok": True, "repository": ref.full_name, "branchCount": 1}

    def checkout_spec(self, ref, credential, revision, working_directory=None) -> CheckoutSpec:
        return CheckoutSpec(url=ref.url, revision=revision, working_directory=working_directory)


FAKE = FakeSourceProvider()
register_provider(FAKE)


# ---------------------------------------------------------------------------
# Repositories the stack tests are built from
# ---------------------------------------------------------------------------

JAVA_GRADLE = {
    "gradlew": "#!/bin/sh\nexec gradle \"$@\"\n",
    "gradlew.bat": "@echo off\n",
    "settings.gradle": "rootProject.name = 'payment-service'\n",
    "build.gradle": (
        "plugins {\n"
        "  id 'java'\n"
        "  id 'org.springframework.boot' version '3.3.2'\n"
        "}\n"
        "java { toolchain { languageVersion = JavaLanguageVersion.of(17) } }\n"
        "repositories { maven { url 'https://nexus.areeba.com/repository/maven-public/' } }\n"
    ),
    "gradle/wrapper/gradle-wrapper.properties": (
        "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.7-bin.zip\n"
    ),
    "Dockerfile": "FROM eclipse-temurin:17-jre\nCOPY app.jar /app.jar\n",
    "src/main/java/com/acme/Application.java": "package com.acme;\n",
    "src/test/java/com/acme/ApplicationTest.java": "package com.acme;\n",
}

JAVA_MAVEN = {
    "mvnw": "#!/bin/sh\n",
    ".mvn/wrapper/maven-wrapper.properties": (
        "distributionUrl=https://repo.maven.apache.org/maven2/org/apache/maven/"
        "apache-maven/3.9.6/apache-maven-3.9.6-bin.zip\n"
    ),
    "pom.xml": (
        "<project>\n"
        "  <parent><artifactId>spring-boot-starter-parent</artifactId>"
        "<version>3.2.5</version></parent>\n"
        "  <properties><java.version>21</java.version></properties>\n"
        "</project>\n"
    ),
    "Dockerfile": "FROM eclipse-temurin:21-jre\n",
    "src/main/java/com/acme/App.java": "package com.acme;\n",
    "src/test/java/com/acme/AppTest.java": "package com.acme;\n",
}

NODE_NPM = {
    "package.json": (
        '{"name":"web","engines":{"node":">=22"},'
        '"scripts":{"build":"next build","test":"jest"},'
        '"dependencies":{"next":"14.2.3","react":"18.3.1"}}'
    ),
    "package-lock.json": '{"lockfileVersion":3}',
    "next.config.js": "module.exports = {};\n",
    "Dockerfile": "FROM node:22-alpine\n",
    "src/app/page.tsx": "export default function Page() { return null; }\n",
    "__tests__/page.test.tsx": "test('x', () => {});\n",
}

PYTHON_POETRY = {
    "pyproject.toml": (
        "[tool.poetry]\nname = 'billing'\n"
        "[tool.poetry.dependencies]\npython = '^3.12'\nfastapi = '^0.111.0'\n"
    ),
    "poetry.lock": "# lock\n",
    "tests/test_api.py": "def test_ok(): assert True\n",
    "billing/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
}

ANDROID = {
    "gradlew": "#!/bin/sh\n",
    "settings.gradle": "include ':app'\n",
    "build.gradle": "plugins { id 'com.android.application' version '8.4.0' apply false }\n",
    "app/build.gradle": (
        "plugins { id 'com.android.application'; id 'org.jetbrains.kotlin.android' }\n"
        "android { compileSdk 34\n  defaultConfig { applicationId 'com.acme.pay' } }\n"
    ),
    "gradle/wrapper/gradle-wrapper.properties": (
        "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.6-bin.zip\n"
    ),
    "app/src/main/AndroidManifest.xml": "<manifest/>\n",
}

FLUTTER = {
    "pubspec.yaml": (
        "name: wallet\nenvironment:\n  sdk: '>=3.4.0 <4.0.0'\n  flutter: '>=3.22.0'\n"
    ),
    "pubspec.lock": "# lock\n",
    "lib/main.dart": "void main() {}\n",
    "android/build.gradle": "// android\n",
    "ios/Runner.xcodeproj/project.pbxproj": "// pbxproj\n",
    "test/widget_test.dart": "void main() {}\n",
}

IOS = {
    "Podfile": "platform :ios, '15.0'\n",
    "Wallet.xcodeproj/project.pbxproj": "// pbxproj\n",
    "Wallet/AppDelegate.swift": "import UIKit\n",
}

DOCKER_ONLY = {
    "Dockerfile": "FROM nginx:1.27-alpine\nCOPY site/ /usr/share/nginx/html\n",
    "site/index.html": "<h1>hello</h1>\n",
    ".dockerignore": ".git\n",
}

UNKNOWN = {
    "README": "internal tooling\n",
    "run.sh": "#!/bin/sh\necho hello\n",
    "data/notes.txt": "nothing to build\n",
}

MONOREPO = {
    "services/payments/build.gradle": "plugins { id 'java' }\n",
    "services/payments/gradlew": "#!/bin/sh\n",
    "services/payments/Dockerfile": "FROM eclipse-temurin:17-jre\n",
    "services/shipping/build.gradle": "plugins { id 'java' }\n",
    "services/shipping/gradlew": "#!/bin/sh\n",
    "README.md": "monorepo\n",
}
