"""The AWS adapter: SigV4 signing, the EKS token, inventory parsing from
recorded AWS answers, sizes, and the main.tf.json it writes.

The signer was also compared with botocore 1.43 (header signing for EC2/STS/
EKS, presigned STS URLs with and without a session token, the EKS token) —
identical. botocore is not a dependency; the published AWS example below
pins the algorithm instead.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from api.services.cluster_build.provisioning.clouds import aws, base
from api.services.cluster_build.provisioning.clouds.base import Account, CloudError, ManagedContext, VmContext


def _account(**secrets):
    return Account(
        provider="aws", name="prod", region="eu-west-1",
        settings={"accessKeyId": "AKIAIOSFODNN7EXAMPLE"},
        secrets={"secretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", **secrets},
    )


@pytest.fixture
def fake_aws():
    """Answer AWS calls from a dict of Action → XML (or path → JSON)."""
    answers = {}
    calls = []

    def transport(method, url, headers, body, timeout_s, ca_pem):
        params = parse_qs((body or b"").decode()) if body else parse_qs(urlsplit(url).query)
        calls.append({"method": method, "url": url, "headers": headers, "params": params})
        action = (params.get("Action") or [urlsplit(url).path])[0]
        answer = answers.get(action)
        if answer is None:
            return 400, {}, b"<Response><Errors><Error><Code>InvalidAction</Code><Message>no</Message></Error></Errors></Response>"
        status, text = answer if isinstance(answer, tuple) else (200, answer)
        return status, {}, text.encode()

    base.set_transport(transport)
    yield answers, calls
    base.set_transport(None)


EC2_NS = 'xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"'


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def test_sigv4_matches_the_published_aws_example():
    headers = aws.sign(
        "GET", "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
        {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}, None,
        access_key="AKIDEXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        region="us-east-1", service="iam", amz_date="20150830T123600Z",
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )


def test_session_token_is_sent_and_signed():
    headers = aws.sign("POST", "https://sts.eu-west-1.amazonaws.com/", {}, b"Action=GetCallerIdentity",
                       access_key="ASIAEXAMPLEEXAMPLE", secret_key="s", session_token="tok",
                       region="eu-west-1", service="sts", amz_date="20261103T120000Z")
    assert headers["X-Amz-Security-Token"] == "tok"
    assert "x-amz-security-token" in headers["Authorization"]


def test_eks_token_is_a_presigned_sts_url_naming_the_cluster():
    token = aws.eks_token(_account(sessionToken="tok"), "kubesight-lab", amz_date="20261103T120000Z")
    assert token.startswith("k8s-aws-v1.")
    assert "=" not in token
    url = base64.urlsafe_b64decode(token[len("k8s-aws-v1."):] + "==").decode()
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.netloc == "sts.eu-west-1.amazonaws.com"
    assert query["Action"] == ["GetCallerIdentity"]
    assert query["X-Amz-Expires"] == ["60"]
    assert query["X-Amz-SignedHeaders"] == ["host;x-k8s-aws-id"]
    assert query["X-Amz-Security-Token"] == ["tok"]
    assert len(query["X-Amz-Signature"][0]) == 64


# ---------------------------------------------------------------------------
# The account
# ---------------------------------------------------------------------------

def test_normalize_account_keeps_the_secret_on_edit_but_not_for_a_new_key():
    settings, secrets = aws.normalize_account(
        {"accessKeyId": "AKIAIOSFODNN7EXAMPLE"}, {"secretAccessKey": " abc/def "})
    assert settings == {"accessKeyId": "AKIAIOSFODNN7EXAMPLE"}
    assert secrets == {"secretAccessKey": "abc/def"}

    _, kept = aws.normalize_account({"accessKeyId": "AKIAIOSFODNN7EXAMPLE"}, {"secretAccessKey": ""},
                                    settings, secrets)
    assert kept["secretAccessKey"] == "abc/def"

    with pytest.raises(ValueError, match="Secret access key"):
        aws.normalize_account({"accessKeyId": "AKIAOTHERKEY0000000"}, {}, settings, secrets)
    with pytest.raises(ValueError, match="Access key ID"):
        aws.normalize_account({"accessKeyId": "not a key"}, {"secretAccessKey": "x"})


def test_test_returns_the_arn(fake_aws):
    answers, calls = fake_aws
    answers["GetCallerIdentity"] = (
        '<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
        "<GetCallerIdentityResult><Arn>arn:aws:iam::123456789012:user/kubesight</Arn>"
        "<UserId>AIDA</UserId><Account>123456789012</Account></GetCallerIdentityResult>"
        "</GetCallerIdentityResponse>"
    )
    assert aws.test(_account()) == "arn:aws:iam::123456789012:user/kubesight"
    assert calls[0]["url"] == "https://sts.eu-west-1.amazonaws.com/"
    assert calls[0]["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/")


def test_refused_keys_become_a_readable_error_without_the_secret(fake_aws):
    answers, _ = fake_aws
    answers["GetCallerIdentity"] = (403, (
        '<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><Error><Type>Sender</Type>'
        "<Code>InvalidClientTokenId</Code><Message>The security token included in the request is invalid.</Message>"
        "</Error></ErrorResponse>"
    ))
    with pytest.raises(CloudError) as caught:
        aws.test(_account())
    assert "did not accept the keys (InvalidClientTokenId)" in str(caught.value)
    assert "wJalr" not in str(caught.value)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def test_inventory_reads_zones_networks_images(fake_aws):
    answers, _ = fake_aws
    answers["DescribeAvailabilityZones"] = (
        f"<DescribeAvailabilityZonesResponse {EC2_NS}><availabilityZoneInfo>"
        "<item><zoneName>eu-west-1b</zoneName></item><item><zoneName>eu-west-1a</zoneName></item>"
        "</availabilityZoneInfo></DescribeAvailabilityZonesResponse>"
    )
    answers["DescribeVpcs"] = (
        f"<DescribeVpcsResponse {EC2_NS}><vpcSet><item><vpcId>vpc-1</vpcId><cidrBlock>10.20.0.0/16</cidrBlock>"
        "<tagSet><item><key>Name</key><value>prod</value></item></tagSet></item></vpcSet></DescribeVpcsResponse>"
    )
    answers["DescribeSubnets"] = (
        f"<DescribeSubnetsResponse {EC2_NS}><subnetSet>"
        "<item><subnetId>subnet-0aaaaaaaaaaaaaaaa</subnetId><vpcId>vpc-1</vpcId><cidrBlock>10.20.1.0/24</cidrBlock>"
        "<availabilityZone>eu-west-1a</availabilityZone></item>"
        "<item><subnetId>subnet-0bbbbbbbbbbbbbbbb</subnetId><vpcId>vpc-other</vpcId><cidrBlock>10.9.0.0/24</cidrBlock>"
        "<availabilityZone>eu-west-1b</availabilityZone></item>"
        "</subnetSet></DescribeSubnetsResponse>"
    )
    answers["DescribeImages"] = (
        f"<DescribeImagesResponse {EC2_NS}><imagesSet>"
        "<item><imageId>ami-1</imageId><name>ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-20261001</name>"
        "<creationDate>2026-10-01T00:00:00.000Z</creationDate></item>"
        "<item><imageId>ami-2</imageId><name>ubuntu/images/hvm-ssd-gp3/ubuntu-plucky-25.04-amd64-server-20261002</name>"
        "<creationDate>2026-10-02T00:00:00.000Z</creationDate></item>"
        "</imagesSet></DescribeImagesResponse>"
    )
    answers["/cluster-versions"] = json.dumps({"clusterVersions": [
        {"clusterVersion": "1.33", "status": "STANDARD_SUPPORT"},
        {"clusterVersion": "1.34", "status": "STANDARD_SUPPORT"},
    ]})
    result = aws.inventory(_account(), "eu-west-1", {})
    assert [z["id"] for z in result["zones"]] == ["eu-west-1a", "eu-west-1b"]
    assert result["networks"] == [{"id": "vpc-1", "name": "prod", "cidr": "10.20.0.0/16", "subnets": [
        {"id": "subnet-0aaaaaaaaaaaaaaaa", "name": "subnet-0aaaaaaaaaaaaaaaa", "cidr": "10.20.1.0/24",
         "zone": "eu-west-1a"},
    ]}]
    ubuntu = [i for i in result["images"] if i["osFamily"] == "debian"]
    # Interim releases (25.04) are not offered; the same answer serves every
    # family in this fake, so only the Ubuntu line is checked.
    assert [i["id"] for i in ubuntu] == ["ami-1"]
    assert ubuntu[0]["family"] == "Ubuntu 24.04 LTS"
    assert result["kubernetesVersions"] == ["1.34", "1.33"]
    assert any(t["id"] == "m6i.xlarge" for t in result["machineTypes"])
    assert set(result) == set(base.empty_inventory("x"))


def test_images_ask_only_the_publishers_own_accounts(fake_aws):
    answers, calls = fake_aws
    answers["DescribeImages"] = f"<DescribeImagesResponse {EC2_NS}><imagesSet/></DescribeImagesResponse>"
    aws._images(_account(), "eu-west-1")
    owners = [c["params"]["Owner.1"][0] for c in calls]
    assert owners == ["099720109477", "309956199498", "792107900819", "137112412989"]


def test_simulated_inventory_has_every_key():
    inv = aws.simulated_inventory("eu-west-1", {})
    assert set(inv) == set(base.empty_inventory("x"))
    assert inv["networks"][0]["subnets"][0]["zone"] == "eu-west-1a"
    assert aws.simulated_regions()[0]["id"] == "eu-west-1"


# ---------------------------------------------------------------------------
# Placement and sizes
# ---------------------------------------------------------------------------

def test_placement_rules():
    assert aws.normalize_placement({"subnetZone": "eu-west-1a"}, "vms") == {
        "diskType": "gp3", "zone": "eu-west-1a"}
    with pytest.raises(ValueError, match="one zone"):
        aws.normalize_placement({"zone": "eu-west-1b", "subnetZone": "eu-west-1a"}, "vms")
    with pytest.raises(ValueError, match="two subnets"):
        aws.normalize_placement({"subnetIds": ["subnet-0aaaaaaaaaaaaaaaa"]}, "managed")
    with pytest.raises(ValueError, match="two availability zones"):
        aws.normalize_placement({
            "subnetIds": ["subnet-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb"],
            "subnetZones": {"subnet-0aaaaaaaaaaaaaaaa": "eu-west-1a", "subnet-0bbbbbbbbbbbbbbbb": "eu-west-1a"},
        }, "managed")
    out = aws.normalize_placement({"subnetIds": "subnet-0aaaaaaaaaaaaaaaa, subnet-0bbbbbbbbbbbbbbbb"}, "managed")
    assert out["subnetIds"] == ["subnet-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb"]
    with pytest.raises(ValueError, match="diskType"):
        aws.normalize_placement({"diskType": "st1"}, "vms")


def test_machine_types_fit_the_size_and_honour_a_role_override():
    assert aws.resolve_machine_type({"cpu": 4, "memoryGb": 16, "role": "worker"}, {}, [])["type"] == "m6i.xlarge"
    assert aws.resolve_machine_type({"cpu": 2, "memoryGb": 4, "role": "worker"}, {}, [])["type"] == "t3.medium"
    picked = aws.resolve_machine_type({"cpu": 2, "memoryGb": 4, "role": "controlPlane"},
                                      {"machineTypes": {"controlPlane": "c6i.xlarge"}})
    assert picked == {"type": "c6i.xlarge", "cpu": 4, "memoryGb": 8, "note": "compute optimised"}
    unknown = aws.resolve_machine_type({"cpu": 8, "memoryGb": 32, "role": "worker"},
                                       {"machineTypes": {"worker": "m8g.2xlarge"}})
    assert unknown["type"] == "m8g.2xlarge" and "not in KubeSight's table" in unknown["note"]
    with pytest.raises(ValueError):
        aws.resolve_machine_type({"cpu": 2, "memoryGb": 4, "role": "worker"}, {"machineTypes": {"worker": "big one"}})
    with pytest.raises(ValueError):
        aws.resolve_machine_type({"cpu": 128, "memoryGb": 4, "role": "worker"}, {})
    assert aws.resolve_machine_type({"cpu": 2, "memoryGb": 4, "role": "worker"},
                                    {"instanceFamily": "r6i"})["type"] == "r6i.large"


def test_plan_checks_stop_on_a_vanished_subnet(fake_aws):
    answers, _ = fake_aws
    answers["DescribeSubnets"] = f"<DescribeSubnetsResponse {EC2_NS}><subnetSet/></DescribeSubnetsResponse>"
    answers["DescribeImages"] = (
        f"<DescribeImagesResponse {EC2_NS}><imagesSet><item><imageId>ami-1</imageId>"
        "<imageState>available</imageState><name>ubuntu</name></item></imagesSet></DescribeImagesResponse>"
    )
    checks = aws.plan_checks(_account(), {
        "region": "eu-west-1", "kind": "vms", "networkId": "vpc-1",
        "subnetId": "subnet-0aaaaaaaaaaaaaaaa", "image": {"id": "ami-1"},
    })
    assert [c["status"] for c in checks] == ["fail", "ok"]


# ---------------------------------------------------------------------------
# main.tf.json
# ---------------------------------------------------------------------------

def _vm_ctx(load_balancer=True):
    machine = {"type": "m6i.xlarge", "cpu": 4, "memoryGb": 16}
    nodes = [{"name": f"lab-cp-{i}", "role": "controlPlane", "cpu": 4, "memoryGb": 16, "diskGb": 80,
              "machineType": machine} for i in (1, 2, 3)]
    nodes += [{"name": f"lab-wk-{i}", "role": "worker", "cpu": 4, "memoryGb": 16, "diskGb": 100,
               "machineType": machine} for i in (1, 2)]
    return VmContext(
        cluster_name="lab", build_id=7, region="eu-west-1",
        spec={"networkId": "vpc-1", "networkCidr": "10.20.0.0/16", "subnetId": "subnet-0aaaaaaaaaaaaaaaa",
              "image": {"id": "ami-1"}, "diskType": "gp3"},
        nodes=nodes, load_balancer=load_balancer,
        ssh={"username": "kubesight", "publicKey": "ssh-ed25519 AAAA kubesight", "password": None},
        allowed_cidrs=["10.0.0.0/8"],
    )


def test_render_vms():
    config = aws.render_vms(_vm_ctx())
    resources = config["resource"]
    node = resources["aws_instance"]["node"]
    assert sorted(node["for_each"]) == ["lab-cp-1", "lab-cp-2", "lab-cp-3", "lab-wk-1", "lab-wk-2"]
    assert node["for_each"]["lab-cp-1"]["placement_group"] == "ks7-lab-cp"
    assert node["for_each"]["lab-wk-1"]["placement_group"] is None
    assert node["associate_public_ip_address"] is False
    assert node["source_dest_check"] is False
    # The hostname is OpenTofu's interpolation, not an escaped literal.
    assert "hostname: ${each.key}\n" in node["user_data"]
    assert "$${each.key}" not in node["user_data"]
    assert {"ami", "user_data"} <= set(node["lifecycle"][0]["ignore_changes"])
    attach = resources["aws_lb_target_group_attachment"]["api"]
    assert sorted(attach["for_each"]) == ["lab-cp-1", "lab-cp-2", "lab-cp-3"]
    assert resources["aws_lb"]["api"]["internal"] is True
    assert resources["aws_lb_target_group"]["api"]["preserve_client_ip"] == "false"
    rules = resources["aws_vpc_security_group_ingress_rule"]["admin"]["for_each"]
    assert set(rules) == {"ssh 10.0.0.0/8", "api 10.0.0.0/8", "api 10.20.0.0/16"}
    assert config["terraform"]["required_providers"]["aws"]["version"] == "= 6.71.0"
    # Nothing secret ends up in the file.
    assert "wJalr" not in json.dumps(config)


def test_render_vms_without_balancer_or_spread():
    ctx = _vm_ctx(load_balancer=False)
    ctx.nodes = [n for n in ctx.nodes if n["name"] != "lab-cp-2" and n["name"] != "lab-cp-3"]
    resources = aws.render_vms(ctx)["resource"]
    assert "aws_lb" not in resources and "aws_placement_group" not in resources
    assert "placement_group" not in resources["aws_instance"]["node"]


def test_render_managed():
    ctx = ManagedContext(
        cluster_name="lab", build_id=7, region="eu-west-1",
        spec={"networkId": "vpc-1", "subnetIds": ["subnet-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb"]},
        kubernetes_version="1.33.4",
        node_pool={"count": 3, "cpu": 4, "memoryGb": 16, "diskGb": 100,
                   "machineType": {"type": "m6i.xlarge", "cpu": 4, "memoryGb": 16}},
        ssh={"username": "kubesight", "publicKey": None, "password": None},
        allowed_cidrs=["10.0.0.0/8"],
    )
    resources = aws.render_managed(ctx)["resource"]
    cluster = resources["aws_eks_cluster"]["cluster"]
    assert cluster["version"] == "1.33"
    assert cluster["vpc_config"][0]["endpoint_public_access"] is False
    assert cluster["access_config"][0]["bootstrap_cluster_creator_admin_permissions"] is True
    pool = resources["aws_eks_node_group"]["workers"]
    assert pool["scaling_config"] == [{"desired_size": 3, "min_size": 3, "max_size": 3}]
    assert pool["instance_types"] == ["m6i.xlarge"]
    policy = resources["aws_iam_role_policy_attachment"]["nodes"]["for_each"]["AmazonEKS_CNI_Policy"]
    assert policy == "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"


# ---------------------------------------------------------------------------
# Running OpenTofu and reading the result
# ---------------------------------------------------------------------------

def test_environment_hides_secrets_and_writes_the_ca(tmp_path):
    account = _account(sessionToken="tok")
    account.ca_pem = "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----"
    env, hide = aws.environment(account, str(tmp_path))
    assert env["AWS_ACCESS_KEY_ID"] == "AKIAIOSFODNN7EXAMPLE"
    assert env["AWS_SESSION_TOKEN"] == "tok"
    assert set(hide) == {"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "tok"}
    assert (tmp_path / "aws-ca-bundle.pem").read_text().startswith("-----BEGIN")


def test_reading_state():
    resources = [
        {"type": "aws_lb", "name": "api", "key": None, "attributes": {"dns_name": "internal-x.elb.amazonaws.com"}},
        {"type": "aws_eks_cluster", "name": "cluster", "key": None, "attributes": {
            "name": "kubesight-lab", "endpoint": "https://ABC.gr7.eu-west-1.eks.amazonaws.com",
            "certificate_authority": [{"data": "Q0E="}]}},
    ]
    assert aws.endpoint_from_state(resources) == "internal-x.elb.amazonaws.com"
    access = aws.managed_access(_account(), resources, {"region": "eu-west-1"})
    assert access["server"].startswith("https://ABC")
    assert access["caData"] == "Q0E="
    assert access["token"].startswith("k8s-aws-v1.")
    assert aws.vm_facts({"id": "i-1", "private_ip": "10.0.0.5", "availability_zone": "eu-west-1a",
                         "instance_type": "t3.large"}) == {
        "id": "i-1", "address": "10.0.0.5", "zone": "eu-west-1a", "machineType": "t3.large"}


def test_describe_and_simulate():
    assert aws.describe_resource("aws_instance", "node", "lab-cp-1",
                                 {"instance_type": "m6i.large", "private_ip": "10.0.0.4"}) == (
        "vm", "lab-cp-1", "m6i.large · 10.0.0.4")
    assert aws.describe_resource("aws_lb", "api", None, {})[0] == "lb"
    assert aws.describe_resource("aws_eks_node_group", "workers", None, {})[0] == "nodepool"
    assert aws.describe_resource("random_thing", "x", None, {}) is None
    vm = aws.simulate_attributes("aws_instance", "node", "lab-cp-1",
                                 {"instance_type": "t3.large", "subnetCidr": "10.20.1.0/24"}, 2)
    assert vm["private_ip"] == "10.20.1.12" and vm["id"].startswith("i-")
    assert aws.simulate_attributes("aws_lb", "api", None, {"name": "ks7-lab-api"}, 0)["dns_name"].startswith(
        "internal-ks7-lab-api-")
    eks = aws.simulate_attributes("aws_eks_cluster", "cluster", None, {"name": "kubesight-lab"}, 0)
    assert eks["endpoint"].startswith("https://") and eks["certificate_authority"][0]["data"]
