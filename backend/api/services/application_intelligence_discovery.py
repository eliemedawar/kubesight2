"""Deterministic repository discovery and scanner adapter normalization."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .application_intelligence_security import EXCLUDED_DIRECTORIES, redact_structure, safe_error

MAX_FILES = int(os.getenv("APPLICATION_ANALYSIS_MAX_FILES", "12000"))
MAX_FILE_BYTES = int(os.getenv("APPLICATION_ANALYSIS_MAX_FILE_BYTES", "1048576"))
MAX_REPOSITORY_BYTES = int(os.getenv("APPLICATION_ANALYSIS_MAX_REPOSITORY_BYTES", "524288000"))

ECOSYSTEM_MARKERS = {
    "pom.xml": ("Java", "Maven", "Spring Boot"),
    "build.gradle": ("Java", "Gradle", None),
    "build.gradle.kts": ("Kotlin", "Gradle", None),
    "package.json": ("JavaScript/TypeScript", "npm", None),
    "requirements.txt": ("Python", "pip", None),
    "pyproject.toml": ("Python", "Python packaging", None),
    "go.mod": ("Go", "Go modules", None),
    "*.csproj": (".NET", "MSBuild", None),
    "composer.json": ("PHP", "Composer", None),
    "Gemfile": ("Ruby", "Bundler", None),
}
SPECIAL_FILES = {
    "dockerfiles": ("Dockerfile",),
    "compose_files": ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"),
    "ci_files": ("Jenkinsfile", "bitbucket-pipelines.yml", ".gitlab-ci.yml"),
    "api_specs": ("openapi.yaml", "openapi.yml", "openapi.json", "swagger.yaml", "swagger.json"),
}

# Runtime configuration is where connection evidence lives — broker addresses,
# datasource URLs, downstream service hosts, and ports. These files must be
# prioritized explicitly: they sort late alphabetically (`src/main/resources`
# after `src/main/java`) and would otherwise be crowded out of the evidence
# budget by source files in any sizeable repository.
CONFIGURATION_FILE_PATTERNS = (
    re.compile(r"^application(-[\w.]+)?\.(ya?ml|properties)$", re.IGNORECASE),
    re.compile(r"^bootstrap(-[\w.]+)?\.(ya?ml|properties)$", re.IGNORECASE),
    re.compile(r"^appsettings(\.[\w.]+)?\.json$", re.IGNORECASE),
    re.compile(r"^(config|settings)(-[\w.]+)?\.(ya?ml|json|toml|properties)$", re.IGNORECASE),
    re.compile(r"^\.env(\.[\w.]+)?$", re.IGNORECASE),
    re.compile(r"^settings\.py$", re.IGNORECASE),
)

# Deterministic connection evidence. Every pattern must capture a literal value
# written in the repository; nothing here infers a conventional default port.
CONNECTION_URI_PATTERN = re.compile(
    r"(?P<scheme>jdbc:[a-z0-9]+|kafka|redis|rediss|amqps?|mongodb(?:\+srv)?|https?|grpcs?|postgresql|mysql|ldaps?)"
    r"://(?P<host>[A-Za-z0-9_.\-]+)(?::(?P<port>\d{2,5}))?",
    re.IGNORECASE,
)
# `bootstrap-servers: broker-a:9092,broker-b:9092` has no URI scheme at all.
BROKER_LIST_PATTERN = re.compile(
    r"(?P<key>bootstrap[._-]servers|brokers?[._-]list|cluster[._-]nodes)"
    r"\s*[:=]\s*[\"']?(?P<value>[A-Za-z0-9_.\-]+:\d{2,5}(?:\s*,\s*[A-Za-z0-9_.\-]+:\d{2,5})*)",
    re.IGNORECASE,
)
URI_SCHEME_TECHNOLOGY = {
    "kafka": ("Apache Kafka", "Message broker", "Kafka"),
    "redis": ("Redis", "Cache", "Redis"),
    "rediss": ("Redis", "Cache", "Redis"),
    "amqp": ("RabbitMQ/AMQP broker", "Message broker", "AMQP"),
    "amqps": ("RabbitMQ/AMQP broker", "Message broker", "AMQPS"),
    "mongodb": ("MongoDB", "Database", "MongoDB"),
    "mongodb+srv": ("MongoDB", "Database", "MongoDB"),
    "postgresql": ("PostgreSQL", "Database", "PostgreSQL"),
    "mysql": ("MySQL", "Database", "MySQL"),
    "ldap": ("LDAP directory", "Directory", "LDAP"),
    "ldaps": ("LDAP directory", "Directory", "LDAPS"),
}
# Integration clients declared in source. A service that externalizes its
# configuration (Spring Cloud Config, ConfigMaps, deploy-time environment) has
# no broker address in the repository at all, but the client usage is still
# right there in the code — this is what "it talks to Kafka" looks like on disk.
SOURCE_INTEGRATION_MARKERS = (
    (re.compile(r"@KafkaListener|@KafkaHandler|@EnableKafka|KafkaTemplate|KafkaProducer|KafkaConsumer|ProducerFactory|ConsumerFactory"),
     ("Apache Kafka", "Message broker", "Kafka")),
    (re.compile(r"@RabbitListener|RabbitTemplate|AmqpTemplate|ConnectionFactory\s*\(\s*[\"']amqp"),
     ("RabbitMQ/AMQP broker", "Message broker", "AMQP")),
    (re.compile(r"@JmsListener|JmsTemplate|ActiveMQConnectionFactory"),
     ("ActiveMQ/JMS broker", "Message broker", "JMS")),
    (re.compile(r"RedisTemplate|StringRedisTemplate|JedisPool|LettuceConnectionFactory"),
     ("Redis", "Cache", "Redis")),
    (re.compile(r"MongoTemplate|MongoRepository|ReactiveMongoTemplate"),
     ("MongoDB", "Database", "MongoDB")),
    (re.compile(r"ElasticsearchRepository|RestHighLevelClient|ElasticsearchClient"),
     ("Elasticsearch", "Search engine", "HTTP")),
)
# Topic/queue names make the broker edge concrete for a reviewer.
TOPIC_NAME_PATTERN = re.compile(
    r"(?:topics|queues|destination)\s*=\s*[\{\s]*[\"']([\w.\-${}]+)[\"']"
)
SOURCE_SCAN_SUFFIXES = {".java", ".kt", ".py", ".js", ".ts", ".cs", ".go", ".rb", ".php"}

# A declared HTTP client names its destination even when the address is a
# placeholder resolved at runtime. Recording the property name answers "where
# does this host come from?" instead of leaving the reader with "not stated".
FEIGN_CLIENT_PATTERN = re.compile(
    r"@(?:FeignClient|RegisterRestClient)\s*\(\s*(?P<body>[^)]*)\)", re.DOTALL
)
CLIENT_ATTRIBUTE_PATTERN = re.compile(
    r"(?P<key>name|value|url|serviceId|configKey)\s*=\s*[\"'](?P<val>[^\"']+)[\"']"
)
PLACEHOLDER_PATTERN = re.compile(r"\$\{([^:}]+)(?::[^}]*)?\}")

# Declaring the client library proves the dependency exists even when the broker
# address is injected at deploy time and never appears in the repository.
DEPENDENCY_TECHNOLOGY_MARKERS = (
    (re.compile(r"spring-kafka|kafka-clients|org\.apache\.kafka|confluent-kafka|kafkajs", re.IGNORECASE),
     ("Apache Kafka", "Message broker", "Kafka")),
    (re.compile(r"spring-boot-starter-amqp|amqp-client|rabbitmq", re.IGNORECASE),
     ("RabbitMQ/AMQP broker", "Message broker", "AMQP")),
    (re.compile(r"spring-boot-starter-data-redis|jedis|lettuce-core|ioredis", re.IGNORECASE),
     ("Redis", "Cache", "Redis")),
    (re.compile(r"spring-boot-starter-data-mongodb|mongodb-driver|mongoose", re.IGNORECASE),
     ("MongoDB", "Database", "MongoDB")),
    (re.compile(r"elasticsearch-rest|spring-boot-starter-data-elasticsearch", re.IGNORECASE),
     ("Elasticsearch", "Search engine", "HTTP")),
    (re.compile(r"spring-boot-starter-activemq|activemq-client", re.IGNORECASE),
     ("ActiveMQ", "Message broker", "JMS")),
)

API_ROUTE_PATTERNS = (
    (
        "Flask/FastAPI",
        re.compile(
            r"@(?:app|router|blueprint|bp)\.(?P<method>get|post|put|patch|delete|options|head)"
            r"\(\s*[\"'](?P<path>/[^\"']*)[\"']",
            re.IGNORECASE,
        ),
    ),
    (
        "Express",
        re.compile(
            r"(?:app|router)\.(?P<method>get|post|put|patch|delete|options|head)"
            r"\(\s*[\"'`](?P<path>/[^\"'`]*)[\"'`]",
            re.IGNORECASE,
        ),
    ),
    (
        "Spring",
        re.compile(
            r"@(?P<method>Get|Post|Put|Patch|Delete)Mapping"
            r"\(\s*(?:value\s*=\s*)?[\"'](?P<path>/[^\"']*)[\"']",
        ),
    ),
    (
        "ASP.NET",
        re.compile(
            r"\.Map(?P<method>Get|Post|Put|Patch|Delete)"
            r"\(\s*[\"'](?P<path>/[^\"']*)[\"']",
        ),
    ),
    (
        "Go net/http",
        re.compile(
            r"(?:http\.)?HandleFunc\(\s*[\"'](?P<path>/[^\"']*)[\"']",
        ),
    ),
)


# A Spring Feign client interface declares the routes it *calls* with the same
# annotations a controller uses to declare the routes it *serves*. Without this
# distinction an outbound dependency is reported as an endpoint the service
# exposes, which overstates its attack surface.
OUTBOUND_CLIENT_MARKERS = (
    re.compile(r"@FeignClient\b"),
    re.compile(r"@HttpExchange\b"),
    re.compile(r"@RegisterRestClient\b"),
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _route_direction(content: str) -> str:
    """Whether a source file declares served routes or consumed ones."""
    if any(marker.search(content) for marker in OUTBOUND_CLIENT_MARKERS):
        return "Outbound"
    return "Inbound"


def _is_configuration_file(filename: str) -> bool:
    return any(pattern.match(filename) for pattern in CONFIGURATION_FILE_PATTERNS)


def _line_of(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def _connections_in_content(content: str, relative: str) -> list[dict[str, Any]]:
    """Extract literal connection targets from one configuration file.

    Only values actually written in the file are reported. A port is emitted
    solely when it appears next to its host, so a reader can never mistake a
    conventional default for an observed value.
    """
    found: list[dict[str, Any]] = []
    for match in BROKER_LIST_PATTERN.finditer(content):
        line = _line_of(content, match.start())
        brokers = []
        for entry in match.group("value").split(","):
            host, _, port = entry.strip().rpartition(":")
            if host and port.isdigit():
                brokers.append((host, int(port)))
        if not brokers:
            continue
        # A broker list is one clustered dependency, not one dependency per
        # node. Report a single port only when every broker agrees on it.
        ports = {port for _, port in brokers}
        found.append(
            {
                "destination": "Apache Kafka",
                "destination_type": "Message broker",
                "protocol": "Kafka",
                "port": ports.pop() if len(ports) == 1 else None,
                "endpoint": ", ".join(f"{host}:{port}" for host, port in brokers),
                "file": relative,
                "line": line,
                "configuration_key": match.group("key"),
                "evidence": (
                    f"{relative}:{line} declares {match.group('key')} = {match.group('value')}"
                ),
            }
        )
    for match in CONNECTION_URI_PATTERN.finditer(content):
        scheme = match.group("scheme").lower()
        host = match.group("host")
        port = match.group("port")
        # A bare http(s) URL is only interesting as a downstream dependency when
        # it names a real host; localhost and schema/namespace URLs are noise.
        if scheme in {"http", "https"} and (
            host.lower() in {"localhost", "127.0.0.1"}
            or "w3.org" in host
            or "springframework.org" in host
            or "xmlns" in content[max(0, match.start() - 40):match.start()]
        ):
            continue
        if scheme.startswith("jdbc:"):
            engine = scheme.split(":", 1)[1]
            name, kind, protocol = URI_SCHEME_TECHNOLOGY.get(
                engine, (engine.title(), "Database", engine.upper())
            )
        else:
            name, kind, protocol = URI_SCHEME_TECHNOLOGY.get(
                scheme, (host, "External service", scheme.upper())
            )
        line = _line_of(content, match.start())
        found.append(
            {
                "destination": name,
                "destination_type": kind,
                "protocol": protocol,
                "port": int(port) if port else None,
                "endpoint": f"{match.group('scheme')}://{host}" + (f":{port}" if port else ""),
                "file": relative,
                "line": line,
                "evidence": f"{relative}:{line} references {match.group(0)}",
            }
        )
    return found


def _discover_connections(
    root: Path, files: list[dict[str, Any]], configuration_files: list[str], manifests: list[str]
) -> list[dict[str, Any]]:
    """Deterministic outbound dependencies with literal protocols and ports.

    Runs independently of the model so brokers, datastores, and downstream hosts
    are reported from the repository itself rather than from recall.
    """
    connections: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    max_connections = int(os.getenv("APPLICATION_ANALYSIS_MAX_CONNECTIONS", "200"))

    def add(item: dict[str, Any]) -> None:
        key = (item["destination"], item.get("port"), item.get("endpoint"))
        if key in seen or len(connections) >= max_connections:
            return
        seen.add(key)
        connections.append(
            {
                **item,
                "direction": "Outbound",
                "confidence": "Confirmed",
                "evidence_state": "Configuration Declared",
                "required": True,
                "source": "deterministic",
            }
        )

    for relative in configuration_files:
        path = root / relative
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for item in _connections_in_content(content, relative):
            add(item)

    declared = {item["destination"] for item in connections}

    # Integration clients used in source. Only fills gaps: a dependency already
    # found in configuration keeps its literal address and port.
    scanned = 0
    max_scanned = int(os.getenv("APPLICATION_ANALYSIS_MAX_SOURCE_SCAN", "2000"))
    for entry in files:
        if scanned >= max_scanned or len(SOURCE_INTEGRATION_MARKERS) == len(declared):
            break
        relative = str(entry.get("path") or "")
        if not entry.get("eligible") or Path(relative).suffix.lower() not in SOURCE_SCAN_SUFFIXES:
            continue
        try:
            content = (root / relative).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        for match in FEIGN_CLIENT_PATTERN.finditer(content):
            attributes = dict(
                (found.group("key"), found.group("val"))
                for found in CLIENT_ATTRIBUTE_PATTERN.finditer(match.group("body"))
            )
            bare = re.match(r"\s*[\"']([^\"']+)[\"']", match.group("body"))
            service = attributes.get("name") or attributes.get("serviceId") or (
                attributes.get("value") if "url" not in attributes else None
            ) or (bare.group(1) if bare else "")
            url = attributes.get("url") or ""
            placeholder = PLACEHOLDER_PATTERN.search(url)
            literal_host = CONNECTION_URI_PATTERN.search(url) if url else None
            # Prefer the declared client name, then a literal host, then the
            # property that supplies the address. A client with none of those
            # names no destination we can honestly draw. A property key is a
            # path, not a name: `app.feign.config.url.issuing` identifies the
            # "issuing" service, and using its last segment keeps the graph
            # readable and lets it merge with the model's own naming.
            destination = service or (literal_host.group("host") if literal_host else "")
            if not destination and placeholder:
                destination = placeholder.group(1).rsplit(".", 1)[-1].replace("-", " ").strip()
            if not destination:
                continue
            line = _line_of(content, match.start())
            add(
                {
                    "destination": destination,
                    "destination_type": "HTTP service",
                    "protocol": (
                        literal_host.group("scheme").upper() if literal_host else None
                    ),
                    "port": (
                        int(literal_host.group("port"))
                        if literal_host and literal_host.group("port")
                        else None
                    ),
                    "endpoint": url or None,
                    "configuration_key": placeholder.group(1) if placeholder else None,
                    "file": relative,
                    "line": line,
                    "evidence": (
                        f"{relative}:{line} declares {match.group(0)[:160]}"
                        + (
                            f". Its address comes from the '{placeholder.group(1)}' property, "
                            "resolved outside the repository."
                            if placeholder
                            else ""
                        )
                    ),
                }
            )
        for pattern, (name, kind, protocol) in SOURCE_INTEGRATION_MARKERS:
            match = pattern.search(content)
            if not match or name in declared:
                continue
            declared.add(name)
            line = _line_of(content, match.start())
            topics = sorted({topic for topic in TOPIC_NAME_PATTERN.findall(content)})[:8]
            evidence = f"{relative}:{line} uses {match.group(0)}"
            if topics:
                evidence += f"; declares {', '.join(topics)}"
            add(
                {
                    "destination": name,
                    "destination_type": kind,
                    "protocol": protocol,
                    "port": None,
                    "endpoint": ", ".join(topics) if topics else None,
                    "file": relative,
                    "line": line,
                    "evidence": (
                        f"{evidence}. No address is present in the repository; it is "
                        "supplied at deploy time."
                    ),
                }
            )

    # A client library in the build manifest proves the integration exists even
    # when its address is injected at deploy time. Reported without a port,
    # because none was observed.
    for relative in manifests:
        path = root / relative
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern, (name, kind, protocol) in DEPENDENCY_TECHNOLOGY_MARKERS:
            match = pattern.search(content)
            if not match or name in declared:
                continue
            declared.add(name)
            line = _line_of(content, match.start())
            add(
                {
                    "destination": name,
                    "destination_type": kind,
                    "protocol": protocol,
                    "port": None,
                    "endpoint": None,
                    "file": relative,
                    "line": line,
                    "evidence": (
                        f"{relative}:{line} declares the {match.group(0)} client library; "
                        "no address is present in the repository."
                    ),
                }
            )
    return connections


def _discover_api_inventory(root: Path, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract bounded, deterministic HTTP route evidence without executing code."""
    inventory: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    max_routes = int(os.getenv("APPLICATION_ANALYSIS_MAX_API_ROUTES", "1000"))
    source_suffixes = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".cs", ".go"}
    for item in files:
        relative = str(item.get("path") or "")
        path = root / relative
        if (
            not item.get("eligible")
            or path.suffix.lower() not in source_suffixes
            or len(inventory) >= max_routes
        ):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        direction = _route_direction(content)
        for framework, pattern in API_ROUTE_PATTERNS:
            for match in pattern.finditer(content):
                route = match.group("path")
                method = (
                    match.groupdict().get("method") or "ANY"
                ).replace("Mapping", "").upper()
                key = (method, route, relative)
                if key in seen:
                    continue
                seen.add(key)
                inventory.append(
                    {
                        "method": method,
                        "path": route,
                        "direction": direction,
                        "framework": framework,
                        "file": relative,
                        "line": content.count("\n", 0, match.start()) + 1,
                        "confidence": "Confirmed",
                        "evidence_state": "Source Inferred",
                        "source": "deterministic",
                    }
                )
                if len(inventory) >= max_routes:
                    break
            if len(inventory) >= max_routes:
                break
    return inventory


def discover_repository(root: Path) -> dict[str, Any]:
    root = root.resolve()
    files: list[dict[str, Any]] = []
    total_bytes = 0
    detected: list[dict[str, Any]] = []
    special = {key: [] for key in SPECIAL_FILES}
    manifests: list[str] = []
    kubernetes_files: list[str] = []
    helm_charts: list[str] = []
    configuration_files: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if name not in EXCLUDED_DIRECTORIES and not name.startswith(".terraform")
        ]
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            try:
                if path.is_symlink():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            total_bytes += size
            if total_bytes > MAX_REPOSITORY_BYTES:
                raise ValueError("Repository is larger than the configured analysis limit.")
            relative = path.relative_to(root).as_posix()
            if len(files) >= MAX_FILES:
                raise ValueError("Repository contains more files than the configured analysis limit.")
            files.append({"path": relative, "size": size, "eligible": size <= MAX_FILE_BYTES})

            for marker, technology in ECOSYSTEM_MARKERS.items():
                matched = filename.endswith(".csproj") if marker == "*.csproj" else filename == marker
                if matched:
                    manifests.append(relative)
                    detected.append(
                        {
                            "language": technology[0],
                            "buildSystem": technology[1],
                            "frameworkHint": technology[2],
                            "evidence": relative,
                        }
                    )
            for key, names in SPECIAL_FILES.items():
                if filename in names or (key == "dockerfiles" and filename.startswith("Dockerfile")):
                    special[key].append(relative)
            if size <= MAX_FILE_BYTES and _is_configuration_file(filename):
                configuration_files.append(relative)
            lower = relative.lower()
            if filename == "Chart.yaml":
                helm_charts.append(str(Path(relative).parent.as_posix()))
            if (
                lower.startswith(("k8s/", "kubernetes/", "manifests/", "deploy/"))
                and path.suffix.lower() in {".yaml", ".yml"}
            ):
                kubernetes_files.append(relative)

    return {
        "file_tree": files,
        "repository_size_bytes": total_bytes,
        "detected_technology": detected,
        "dependency_manifests": manifests,
        "kubernetes_manifests": kubernetes_files,
        "helm_charts": helm_charts,
        "configuration_files": configuration_files,
        "api_inventory": _discover_api_inventory(root, files),
        "connections": _discover_connections(root, files, configuration_files, manifests),
        **special,
    }


class ScannerAdapter:
    name = "base"
    executable = ""

    def command(self, root: Path) -> list[str]:
        raise NotImplementedError

    def normalize(self, output: str) -> list[dict[str, Any]]:
        return []

    def run(self, root: Path, timeout_seconds: int = 600) -> dict[str, Any]:
        started = _iso_now()
        if not shutil.which(self.executable):
            return {
                "name": self.name,
                "version": None,
                "startedAt": started,
                "completedAt": _iso_now(),
                "exitStatus": "unavailable",
                "warning": f"{self.name} is not installed in the analysis image.",
                "findings": [],
            }
        try:
            version = subprocess.run(
                [self.executable, "--version"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            completed = subprocess.run(
                self.command(root),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            output = completed.stdout or "{}"
            output_bytes = len(output.encode("utf-8", errors="replace"))
            if output_bytes > int(
                os.getenv("APPLICATION_ANALYSIS_SCANNER_OUTPUT_MAX_BYTES", "20000000")
            ):
                raise ValueError(f"{self.name} output exceeded the configured limit.")
            findings = self.normalize(output)
            return {
                "name": self.name,
                "version": (version.stdout or version.stderr or "").strip()[:200],
                "startedAt": started,
                "completedAt": _iso_now(),
                "exitStatus": completed.returncode,
                "warning": safe_error(RuntimeError(completed.stderr)) if completed.returncode > 1 else None,
                "findings": redact_structure(findings),
            }
        except Exception as exc:
            return {
                "name": self.name,
                "version": None,
                "startedAt": started,
                "completedAt": _iso_now(),
                "exitStatus": "failed",
                "warning": safe_error(exc, f"{self.name} failed safely."),
                "findings": [],
            }


class SemgrepAdapter(ScannerAdapter):
    name = "Semgrep"
    executable = "semgrep"

    def command(self, root: Path) -> list[str]:
        return ["semgrep", "scan", "--config", "auto", "--json", "--metrics=off", str(root)]

    def normalize(self, output: str) -> list[dict[str, Any]]:
        data = json.loads(output or "{}")
        return [
            {
                "scanner": self.name,
                "ruleId": item.get("check_id"),
                "severity": str(item.get("extra", {}).get("severity") or "WARNING").title(),
                "title": item.get("extra", {}).get("message") or item.get("check_id"),
                "file": item.get("path"),
                "startLine": item.get("start", {}).get("line"),
                "endLine": item.get("end", {}).get("line"),
            }
            for item in data.get("results", [])
        ]


class TrivyAdapter(ScannerAdapter):
    name = "Trivy"
    executable = "trivy"

    def command(self, root: Path) -> list[str]:
        return ["trivy", "fs", "--format", "json", "--scanners", "vuln,secret,misconfig", str(root)]

    def normalize(self, output: str) -> list[dict[str, Any]]:
        data = json.loads(output or "{}")
        items = []
        for result in data.get("Results", []):
            for vuln in result.get("Vulnerabilities") or []:
                items.append(
                    {
                        "scanner": self.name,
                        "ruleId": vuln.get("VulnerabilityID"),
                        "severity": str(vuln.get("Severity") or "UNKNOWN").title(),
                        "title": vuln.get("Title") or vuln.get("VulnerabilityID"),
                        "file": result.get("Target"),
                        "package": vuln.get("PkgName"),
                        "version": vuln.get("InstalledVersion"),
                    }
                )
            for secret in result.get("Secrets") or []:
                items.append(
                    {
                        "scanner": self.name,
                        "ruleId": secret.get("RuleID"),
                        "severity": str(secret.get("Severity") or "HIGH").title(),
                        "title": secret.get("Title") or "Potential secret",
                        "file": result.get("Target"),
                        "startLine": secret.get("StartLine"),
                        "endLine": secret.get("EndLine"),
                    }
                )
        return items


class HadolintAdapter(ScannerAdapter):
    name = "Hadolint"
    executable = "hadolint"

    def command(self, root: Path) -> list[str]:
        dockerfiles = [path for path in root.rglob("Dockerfile*") if path.is_file()]
        return ["hadolint", "--format", "json", *[str(path) for path in dockerfiles]]

    def normalize(self, output: str) -> list[dict[str, Any]]:
        return [
            {
                "scanner": self.name,
                "ruleId": item.get("code"),
                "severity": str(item.get("level") or "warning").title(),
                "title": item.get("message"),
                "file": item.get("file"),
                "startLine": item.get("line"),
            }
            for item in json.loads(output or "[]")
        ]


class SyftAdapter(ScannerAdapter):
    name = "Syft"
    executable = "syft"

    def command(self, root: Path) -> list[str]:
        return ["syft", f"dir:{root}", "-o", "json"]

    def normalize(self, output: str) -> list[dict[str, Any]]:
        data = json.loads(output or "{}")
        return [
            {
                "scanner": self.name,
                "type": "dependency",
                "name": item.get("name"),
                "version": item.get("version"),
                "ecosystem": item.get("type"),
                "licenses": [
                    license_item.get("value")
                    for license_item in item.get("licenses") or []
                    if isinstance(license_item, dict)
                ],
                "source": (item.get("locations") or [{}])[0].get("path"),
            }
            for item in data.get("artifacts", [])
        ]


SCANNERS = (SemgrepAdapter(), TrivyAdapter(), SyftAdapter(), HadolintAdapter())
