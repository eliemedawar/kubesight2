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
# no address in the repository at all, but the client usage is still right there
# in the code — this is what "it talks to Redis" looks like on disk. Message
# brokers are handled separately below, because their edge carries a direction.
SOURCE_INTEGRATION_MARKERS = (
    (re.compile(r"RedisTemplate|StringRedisTemplate|JedisPool|LettuceConnectionFactory"),
     ("Redis", "Cache", "Redis")),
    (re.compile(r"MongoTemplate|MongoRepository|ReactiveMongoTemplate"),
     ("MongoDB", "Database", "MongoDB")),
    (re.compile(r"ElasticsearchRepository|RestHighLevelClient|ElasticsearchClient"),
     ("Elasticsearch", "Search engine", "HTTP")),
)
SOURCE_SCAN_SUFFIXES = {".java", ".kt", ".py", ".js", ".ts", ".cs", ".go", ".rb", ".php"}

# ------------------------------------------------------------------- messaging
# A broker edge carries a direction that wire facts do not. "This service talks
# to Kafka" leaves a reviewer unable to tell a pipeline's producer from its
# consumer, and it hides the one identifier that makes the edge actionable: the
# topic. Both are read from the client usage itself, per role, and aggregated
# across the whole scan — publishing and listening rarely live in one file.
KAFKA_TECHNOLOGY = ("Apache Kafka", "Message broker", "Kafka")
AMQP_TECHNOLOGY = ("RabbitMQ/AMQP broker", "Message broker", "AMQP")
JMS_TECHNOLOGY = ("ActiveMQ/JMS broker", "Message broker", "JMS")
MAX_MESSAGING_TOPICS = int(os.getenv("APPLICATION_ANALYSIS_MAX_TOPICS", "12"))

# One topic expression as it is written where it is used. Call arguments and
# annotation attributes take different shapes, and keeping them apart is what
# stops an unrelated quoted value inside a client's options object (`messages:
# [{ value: "…" }]`) from being read as a topic.
_QUOTED = r"[\"'][^\"'\r\n]{1,200}[\"']"
_IDENTIFIER = r"[A-Za-z_$][\w.$]{0,80}"
_QUOTED_LIST = rf"{_QUOTED}(?:\s*,\s*{_QUOTED})*"
_ARGUMENT_VALUE = (
    rf"(?P<value>{_QUOTED_LIST}"
    rf"|\[[^\[\]]{{0,300}}\]"
    rf"|{_IDENTIFIER}\s*\([^()]{{0,300}}\)"
    rf"|{_IDENTIFIER})"
)
# `topics = {"a", "b"}` — a Java annotation array.
_ANNOTATION_VALUE = (
    rf"(?P<value>{_QUOTED_LIST}"
    rf"|\{{[^{{}}]{{0,300}}\}}"
    rf"|{_IDENTIFIER}\s*\([^()]{{0,300}}\)"
    rf"|{_IDENTIFIER})"
)
# An annotation body nests one level (`@KafkaListener(topicPartitions =
# @TopicPartition(...))`), where a plain `[^)]*` would stop at the inner
# bracket and miss the attribute that follows it.
_ANNOTATION_BODY = r"(?:[^()]|\([^()]*\))*?"
# A client options object nests too: `{ topic: t, messages: [{ value: v }] }`.
_OBJECT_BODY = r"(?:[^{}]|\{[^{}]{0,300}\}){0,400}?"

KAFKA_TOPIC_PATTERNS = {
    "Consumer": (
        re.compile(
            rf"@KafkaListener\s*\({_ANNOTATION_BODY}\b(?:topics|topicPattern)\s*=\s*{_ANNOTATION_VALUE}",
            re.DOTALL,
        ),
        re.compile(
            rf"@TopicPartition\s*\({_ANNOTATION_BODY}\btopic\s*=\s*{_ANNOTATION_VALUE}", re.DOTALL
        ),
        # kafkajs: `consumer.subscribe({ topic: "orders", fromBeginning: true })`.
        re.compile(
            rf"\.\s*subscribe\s*\(\s*\{{{_OBJECT_BODY}\btopics?\s*:\s*{_ARGUMENT_VALUE}", re.DOTALL
        ),
        # `consumer.subscribe(Arrays.asList(TOPIC))`, `.subscribe(["orders"])`.
        re.compile(rf"\.\s*subscribe\s*\(\s*{_ARGUMENT_VALUE}", re.DOTALL),
        # kafka-python takes its topics as the leading positional arguments.
        re.compile(rf"KafkaConsumer\s*\(\s*(?P<value>{_QUOTED_LIST})"),
    ),
    "Producer": (
        re.compile(
            rf"\.\s*send(?:Batch)?\s*\(\s*\{{{_OBJECT_BODY}\btopics?\s*:\s*{_ARGUMENT_VALUE}",
            re.DOTALL,
        ),
        re.compile(
            rf"[\w.]*(?:[Kk]afka|[Tt]emplate|[Pp]roducer)[\w.]*\s*\.\s*send\s*\(\s*{_ARGUMENT_VALUE}",
            re.DOTALL,
        ),
        re.compile(rf"new\s+ProducerRecord\s*(?:<[^<>]*>)?\s*\(\s*{_ARGUMENT_VALUE}", re.DOTALL),
        re.compile(rf"@SendTo\s*\(\s*{_ANNOTATION_VALUE}"),
    ),
}
AMQP_TOPIC_PATTERNS = {
    "Consumer": (
        re.compile(
            rf"@RabbitListener\s*\({_ANNOTATION_BODY}\b(?:queues|queuesToDeclare)\s*=\s*{_ANNOTATION_VALUE}",
            re.DOTALL,
        ),
    ),
    "Producer": (
        re.compile(rf"convertAndSend\s*\(\s*{_ARGUMENT_VALUE}", re.DOTALL),
        re.compile(rf"basicPublish\s*\(\s*{_ARGUMENT_VALUE}", re.DOTALL),
    ),
}
JMS_TOPIC_PATTERNS = {
    "Consumer": (
        re.compile(
            rf"@JmsListener\s*\({_ANNOTATION_BODY}\b(?:destination|destinations)\s*=\s*{_ANNOTATION_VALUE}",
            re.DOTALL,
        ),
    ),
    "Producer": (
        re.compile(rf"convertAndSend\s*\(\s*{_ARGUMENT_VALUE}", re.DOTALL),
        re.compile(rf"createProducer\s*\(\s*{_ARGUMENT_VALUE}", re.DOTALL),
    ),
}
# `client` proves the dependency even where the role is unclear; `roles` is what
# separates a producer from a consumer. Topic patterns run only for a role the
# same file already proved, so a generic `.subscribe(` or `.send(` from an
# unrelated library can never invent a topic.
MESSAGING_CLIENTS = (
    {
        "technology": KAFKA_TECHNOLOGY,
        "channel": "topic",
        "client": re.compile(
            r"@Kafka(?:Listener|Listeners|Handler)\b|@EnableKafka\b"
            r"|Kafka(?:Template|Producer|Consumer|Admin)\b"
            r"|(?:Producer|Consumer)Factory\b|ProducerRecord\b|new\s+Kafka\s*\("
            r"|[\w.]*[Kk]afka[\w.]*\s*\.\s*(?:producer|consumer)\s*\("
        ),
        "roles": (
            (
                "Consumer",
                re.compile(
                    r"@Kafka(?:Listener|Listeners|Handler)\b|@EnableKafka\b|KafkaConsumer\b"
                    r"|ConsumerFactory\b|[\w.]*[Kk]afka[\w.]*\s*\.\s*consumer\s*\("
                ),
            ),
            (
                "Producer",
                re.compile(
                    r"Kafka(?:Template|Producer)\b|ProducerFactory\b|ProducerRecord\b|@SendTo\b"
                    r"|[\w.]*[Kk]afka[\w.]*\s*\.\s*producer\s*\("
                ),
            ),
        ),
        "topics": KAFKA_TOPIC_PATTERNS,
    },
    {
        "technology": AMQP_TECHNOLOGY,
        "channel": "queue",
        "client": re.compile(
            r"@RabbitListener\b|RabbitTemplate\b|AmqpTemplate\b|basicPublish\b|basicConsume\b"
            r"|ConnectionFactory\s*\(\s*[\"']amqp"
        ),
        "roles": (
            ("Consumer", re.compile(r"@RabbitListener\b|basicConsume\b")),
            ("Producer", re.compile(r"RabbitTemplate\b|AmqpTemplate\b|basicPublish\b")),
        ),
        "topics": AMQP_TOPIC_PATTERNS,
    },
    {
        "technology": JMS_TECHNOLOGY,
        "channel": "destination",
        "client": re.compile(
            r"@JmsListener\b|JmsTemplate\b|ActiveMQConnectionFactory\b"
            r"|create(?:Producer|Consumer)\s*\("
        ),
        "roles": (
            ("Consumer", re.compile(r"@JmsListener\b|createConsumer\s*\(")),
            ("Producer", re.compile(r"JmsTemplate\b|createProducer\s*\(")),
        ),
        "topics": JMS_TOPIC_PATTERNS,
    },
)
# `send(topic, …)` names a field, not a topic. Resolving the identifier where it
# is declared is what turns the call site into a name a reviewer can search for.
SYMBOL_LITERAL_PATTERNS = (
    re.compile(
        r"@Value\s*\(\s*[\"'](?P<value>[^\"'\r\n]+)[\"']\s*\)"
        r"(?:\s*(?:private|protected|public|static|final|@\w+))*"
        r"\s+[\w<>,.\[\]]+\s+(?P<name>\w+)"
    ),
    re.compile(
        r"(?:const|let|var|val|final|String)\s+(?P<name>\w+)\s*(?::\s*[\w<>\[\].]+)?"
        r"\s*=\s*[\"'](?P<value>[^\"'\r\n]+)[\"']"
    ),
    re.compile(r"(?m)^\s*(?P<name>[A-Z][A-Z0-9_]{1,80})\s*=\s*[\"'](?P<value>[^\"'\r\n]+)[\"']"),
)
# An identifier read out of the environment is externalized exactly like a
# Spring placeholder, so it is reported in the same `${…}` form.
SYMBOL_ENVIRONMENT_PATTERNS = (
    re.compile(r"(?:const|let|var)\s+(?P<name>\w+)\s*=\s*process\.env\.(?P<value>\w+)"),
    re.compile(
        r"(?P<name>\w+)\s*=\s*os\.(?:getenv|environ\.get)\s*\(\s*[\"'](?P<value>\w+)[\"']"
    ),
)
TOPIC_PLACEHOLDER_PATTERN = re.compile(r"^\$\{(?P<key>[^:}]+)(?::(?P<default>[^}]*))?\}$")
# `spring.kafka.template.default-topic` is where a producer's topic lives when
# the send call does not name one. Attributed to Kafka only when the same file
# mentions Kafka, so an unrelated `default-topic` key is never claimed.
DEFAULT_TOPIC_PATTERN = re.compile(
    r"(?P<key>[\w.\-]*default[._-]topic)\s*[:=]\s*[\"']?(?P<value>[\w.\-${}:]+)", re.IGNORECASE
)
# Values that are syntax, not topic names.
NON_TOPIC_TOKENS = {"true", "false", "null", "none", "undefined", "this", "self"}
# An identifier whose value is not in the repository is only reported when the
# name itself says what it holds. A callback (`subscribe(handler)`) is not a
# topic, and publishing it as one would put an invented name on the diagram.
TOPIC_IDENTIFIER_HINT = re.compile(r"topic|queue|destination|channel|stream|subject", re.IGNORECASE)

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


def _configuration_properties(text: str) -> dict[str, str]:
    """Flatten `key: value` configuration into dotted keys.

    Indentation-based rather than a YAML parse: repository configuration is
    frequently templated and would not parse at all, and only scalar leaves are
    needed here — resolving `${kafka.notif.topic}` to the value the repository
    declares for it.
    """
    properties: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for raw_line in text.splitlines()[:4000]:
        line = raw_line.split("#", 1)[0].rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("-"):
            continue
        indent = len(line) - len(line.lstrip())
        key, separator, value = stripped.partition(":")
        if not separator:
            key, separator, value = stripped.partition("=")
            if not separator:
                continue
            # A `.properties` or `.env` line carries the whole dotted path, so it
            # never inherits a parent from indentation.
            indent = -1
        key = key.strip().strip("\"'")
        if not re.fullmatch(r"[\w.\-]{1,200}", key or ""):
            continue
        value = value.strip().rstrip(",").strip().strip("\"'")
        if indent < 0:
            path = key
        else:
            while stack and stack[-1][0] >= indent:
                stack.pop()
            path = ".".join([item[1] for item in stack] + [key])
            if not value:
                stack.append((indent, key))
                continue
        if len(properties) < 2000:
            properties[path] = value
    return properties


def _file_symbols(content: str) -> dict[str, str]:
    """Values for identifiers declared in one file, literal or externalized."""
    symbols: dict[str, str] = {}
    for pattern in SYMBOL_LITERAL_PATTERNS:
        for match in pattern.finditer(content):
            symbols.setdefault(match.group("name"), match.group("value").strip())
    for pattern in SYMBOL_ENVIRONMENT_PATTERNS:
        for match in pattern.finditer(content):
            symbols.setdefault(match.group("name"), "${" + match.group("value") + "}")
    return symbols


def _topic_tokens(raw: str) -> list[tuple[str, str]]:
    """Split one captured topic expression into (kind, text) tokens.

    ``literal`` is a value quoted in the file; ``symbol`` is an identifier whose
    value is declared elsewhere — possibly outside the repository entirely.
    """
    text = str(raw or "").strip()
    # An escaped quote inside the string ends the capture on its backslash, and
    # no broker topic contains one, so it is never part of the name.
    literals = [
        value.strip().strip("\\").strip()
        for value in re.findall(r"[\"']([^\"'\r\n]{1,200})[\"']", text)
        if value.strip().strip("\\").strip()
    ]
    if literals:
        return [("literal", value) for value in literals]
    # `Arrays.asList(TOPIC)` / `List.of(TOPIC)` — keep the arguments, drop the
    # helper that wraps them.
    inner = re.sub(r"^[A-Za-z_$][\w.$]*\s*\(", "", text).rstrip(")")
    tokens: list[tuple[str, str]] = []
    for part in inner.split(","):
        # `topic: someVariable` in an options object.
        name = part.strip().strip("{}[]").split(":")[-1].strip()
        if re.fullmatch(r"[A-Za-z_$][\w.$]{0,80}", name or "") and name.lower() not in NON_TOPIC_TOKENS:
            tokens.append(("symbol", name))
    return tokens


def _topic_reference(
    kind: str, text: str, symbols: dict[str, str], properties: dict[str, str]
) -> dict[str, Any] | None:
    """Resolve one topic token as far as the repository honestly allows.

    ``name`` is what the repository writes — a literal, a `${property}`, or the
    variable itself when its value is assigned somewhere we cannot see. Nothing
    here invents a topic name.
    """
    variable = None
    value = text.strip()
    if kind == "symbol":
        variable = value
        resolved_symbol = symbols.get(value) or symbols.get(value.split(".")[-1]) or ""
        if not resolved_symbol:
            # The variable's value is not in this file. Reporting the identifier
            # is still actionable; guessing the topic it holds would not be.
            if not TOPIC_IDENTIFIER_HINT.search(value):
                return None
            return {"name": value, "variable": value, "configuration_key": None, "resolved": None}
        value = resolved_symbol.strip()
    if not value or value.lower() in NON_TOPIC_TOKENS:
        return None
    placeholder = TOPIC_PLACEHOLDER_PATTERN.match(value)
    if placeholder:
        key = placeholder.group("key").strip()
        declared = (placeholder.group("default") or "").strip() or properties.get(key) or None
        return {
            "name": value,
            "variable": variable,
            "configuration_key": key or None,
            "resolved": declared,
        }
    inner = PLACEHOLDER_PATTERN.search(value)
    return {
        "name": value,
        "variable": variable,
        "configuration_key": inner.group(1).strip() if inner else None,
        "resolved": None,
    }


def _messaging_state(
    messaging: dict[str, dict[str, Any]], technology: tuple[str, str, str], channel: str
) -> dict[str, Any]:
    return messaging.setdefault(
        technology[0],
        {
            "technology": technology,
            "channel": channel,
            "roles": {},
            "topics": {},
            "dropped": set(),
            "usage": None,
        },
    )


def _record_topic(
    state: dict[str, Any], role: str, reference: dict[str, Any], relative: str, line: int
) -> None:
    key = (role, reference["name"])
    if key in state["topics"] or key in state["dropped"]:
        return
    if len(state["topics"]) >= MAX_MESSAGING_TOPICS:
        # A truncated list must say so rather than read as the whole picture.
        state["dropped"].add(key)
        return
    state["topics"][key] = {
        **reference,
        "role": role,
        "kind": state["channel"],
        "file": relative,
        "line": line,
    }


def _messaging_role(roles: dict[str, Any]) -> str:
    if "Producer" in roles and "Consumer" in roles:
        return "Producer and Consumer"
    if "Producer" in roles:
        return "Producer"
    return "Consumer" if "Consumer" in roles else ""


def _messaging_evidence(state: dict[str, Any], topics: list[dict[str, Any]]) -> str:
    """One sentence a reviewer can check line by line."""
    parts: list[str] = []
    usages = [
        f"{item[0]}:{item[1]} uses {item[2]} ({verb})"
        for wanted, verb in (("Producer", "publishes"), ("Consumer", "consumes"))
        for item in [state["roles"].get(wanted)]
        if item
    ]
    if usages:
        parts.append(" and ".join(usages))
    elif state["usage"]:
        usage_file, usage_line, marker = state["usage"]
        parts.append(
            f"{usage_file}:{usage_line} uses {marker}, which does not state whether the "
            "service publishes or consumes"
        )
    channel = state["channel"]
    for verb, wanted in (("publishes to", "Producer"), ("consumes", "Consumer")):
        named = [item["name"] for item in topics if item["role"] == wanted]
        if named:
            label = channel if len(named) == 1 else f"{channel}s"
            parts.append(f"{verb} {label} {', '.join(named)}")
    externalized = sorted(
        {item["configuration_key"] for item in topics if item.get("configuration_key")}
    )
    if externalized:
        noun = "property" if len(externalized) == 1 else "properties"
        parts.append(
            f"the {channel} name is resolved at deploy time from the "
            f"{', '.join(externalized)} {noun}"
        )
    if state["dropped"]:
        parts.append(
            f"{len(state['dropped'])} further {channel} reference(s) are not listed"
        )
    return ("; ".join(parts) + ".") if parts else ""


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


def _marker_text(match: re.Match) -> str:
    """The client symbol a marker matched, without the call syntax around it."""
    return match.group(0).strip().split("(")[0].strip() or match.group(0).strip()


def _scan_messaging(
    content: str,
    relative: str,
    messaging: dict[str, dict[str, Any]],
    properties: dict[str, str],
) -> None:
    """Classify one source file's broker usage by role and collect its topics."""
    for client in MESSAGING_CLIENTS:
        marker = client["client"].search(content)
        if not marker:
            continue
        technology = client["technology"]
        state = _messaging_state(messaging, technology, client["channel"])
        if state["usage"] is None:
            state["usage"] = (relative, _line_of(content, marker.start()), _marker_text(marker))
        roles_here: set[str] = set()
        for role, pattern in client["roles"]:
            hit = pattern.search(content)
            if not hit:
                continue
            roles_here.add(role)
            state["roles"].setdefault(
                role, (relative, _line_of(content, hit.start()), _marker_text(hit))
            )
        if not roles_here:
            continue
        symbols = _file_symbols(content)
        for role, patterns in client["topics"].items():
            if role not in roles_here:
                # Without the role proved in this file, a `.send(` or
                # `.subscribe(` from another library would invent a topic.
                continue
            for pattern in patterns:
                for match in pattern.finditer(content):
                    line = _line_of(content, match.start())
                    for kind, token in _topic_tokens(match.group("value")):
                        reference = _topic_reference(kind, token, symbols, properties)
                        if reference:
                            _record_topic(state, role, reference, relative, line)


def _apply_messaging(
    messaging: dict[str, dict[str, Any]],
    connections: list[dict[str, Any]],
    declared: set[str],
    add: Any,
) -> None:
    """Attach the aggregated role and topics to their broker dependency.

    A broker whose address was already read from configuration keeps that
    address; only the direction of flow and the topic names are added to it.
    """
    for state in messaging.values():
        name, kind, protocol = state["technology"]
        topics = list(state["topics"].values())
        role = _messaging_role(state["roles"])
        evidence = _messaging_evidence(state, topics)
        existing = next(
            (entry for entry in connections if entry.get("destination") == name), None
        )
        if existing is not None:
            if role:
                existing["messaging_role"] = role
            if topics:
                existing["topics"] = topics
            if evidence:
                address = str(existing.get("evidence") or "").strip()
                if address and not address.endswith("."):
                    address += "."
                existing["evidence"] = f"{address} {evidence}".strip()
            declared.add(name)
            continue
        if state["usage"] is None and not topics:
            continue
        declared.add(name)
        relative, line, _marker = state["usage"] or (
            topics[0]["file"],
            topics[0]["line"],
            "",
        )
        add(
            {
                "destination": name,
                "destination_type": kind,
                "protocol": protocol,
                "port": None,
                "endpoint": ", ".join(item["name"] for item in topics) or None,
                "messaging_role": role or None,
                "topics": topics or None,
                "file": relative,
                "line": line,
                "evidence": (
                    f"{evidence} No address is present in the repository; it is "
                    "supplied at deploy time."
                ).strip(),
            }
        )


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

    # Broker roles and topics are aggregated across the whole scan: a service
    # publishes in one class and listens in another, so the first file that
    # mentions Kafka almost never holds both halves of the story.
    messaging: dict[str, dict[str, Any]] = {}
    properties: dict[str, str] = {}

    for relative in configuration_files:
        path = root / relative
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        properties.update(_configuration_properties(content))
        for item in _connections_in_content(content, relative):
            add(item)
        if "kafka" not in content.lower():
            continue
        for match in DEFAULT_TOPIC_PATTERN.finditer(content):
            reference = _topic_reference("literal", match.group("value"), {}, properties)
            if not reference:
                continue
            line = _line_of(content, match.start())
            state = _messaging_state(messaging, KAFKA_TECHNOLOGY, "topic")
            state["roles"].setdefault("Producer", (relative, line, match.group("key")))
            _record_topic(state, "Producer", reference, relative, line)

    declared = {item["destination"] for item in connections}

    # Integration clients used in source. Only fills gaps: a dependency already
    # found in configuration keeps its literal address and port.
    scanned = 0
    max_scanned = int(os.getenv("APPLICATION_ANALYSIS_MAX_SOURCE_SCAN", "2000"))
    for entry in files:
        if scanned >= max_scanned:
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
                        f"{relative}:{line} uses {match.group(0)}. No address is present "
                        "in the repository; it is supplied at deploy time."
                    ),
                }
            )
        _scan_messaging(content, relative, messaging, properties)

    _apply_messaging(messaging, connections, declared, add)

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
