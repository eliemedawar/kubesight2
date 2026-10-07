"""Check kube_direct over TLS with client-certificate auth, like a kubeadm cluster.

Creates a throwaway CA, a server certificate for 127.0.0.1 and a client
certificate (needs ``openssl`` on PATH), serves a tiny HTTPS API that REQUIRES
the client certificate, writes a kubeconfig with the *-data fields embedded,
then reads the same lists through kubectl and through kube_direct.

    python tls_parity.py <work dir>
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ["KUBESIGHT_DIRECT_API_READS"] = "true"

from api import kube_direct  # noqa: E402

PODS = {"kind": "PodList", "apiVersion": "v1", "metadata": {"resourceVersion": "7"},
        "items": [{"metadata": {"name": f"p{i}", "namespace": "ns1"}, "spec": {}, "status": {"phase": "Running"}} for i in range(3)]}


def sh(*args: str, cwd: str) -> None:
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True)


def main() -> int:
    work = os.path.abspath(sys.argv[1])
    os.makedirs(work, exist_ok=True)
    sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.crt", "-days", "2", "-subj", "/CN=stress-ca", cwd=work)
    with open(os.path.join(work, "san.cnf"), "w") as fh:
        fh.write("subjectAltName=IP:127.0.0.1\n")
    sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key", "-out", "server.csr", "-subj", "/CN=127.0.0.1", cwd=work)
    sh("openssl", "x509", "-req", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out", "server.crt", "-days", "2", "-extfile", "san.cnf", cwd=work)
    sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "client.key", "-out", "client.csr", "-subj", "/CN=kubesight/O=system:masters", cwd=work)
    sh("openssl", "x509", "-req", "-in", "client.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out", "client.crt", "-days", "2", cwd=work)

    seen_clients = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            seen_clients.append(self.connection.getpeercert().get("subject"))
            path = self.path.split("?")[0]
            if path in ("/api", "/apis"):
                body = {"kind": "APIVersions", "versions": ["v1"]} if path == "/api" else {"kind": "APIGroupList", "groups": []}
            elif path == "/api/v1":
                body = {"kind": "APIResourceList", "groupVersion": "v1", "resources": [{"name": "pods", "namespaced": True, "kind": "Pod", "verbs": ["get", "list"]}]}
            elif path == "/api/v1/namespaces/ns1/pods":
                body = PODS
            elif path == "/version":
                body = {"major": "1", "minor": "30", "gitVersion": "v1.30.4"}
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(work, "server.crt"), os.path.join(work, "server.key"))
    ctx.load_verify_locations(os.path.join(work, "ca.crt"))
    ctx.verify_mode = ssl.CERT_REQUIRED  # no client certificate, no answer
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def b64(name: str) -> str:
        return base64.b64encode(open(os.path.join(work, name), "rb").read()).decode()

    kubeconfig = os.path.join(work, "tls.yaml")
    with open(kubeconfig, "w") as fh:
        json.dump({
            "apiVersion": "v1", "kind": "Config", "current-context": "tls",
            "clusters": [{"name": "tls", "cluster": {"server": f"https://127.0.0.1:{server.server_port}", "certificate-authority-data": b64("ca.crt")}}],
            "contexts": [{"name": "tls", "context": {"cluster": "tls", "user": "admin"}}],
            "users": [{"name": "admin", "user": {"client-certificate-data": b64("client.crt"), "client-key-data": b64("client.key")}}],
        }, fh)

    ok = True
    for args in (["get", "pods", "-n", "ns1", "-o", "json"], ["version", "-o", "json"]):
        kubectl = json.loads(subprocess.run(["kubectl", "--kubeconfig", kubeconfig, *args], capture_output=True, text=True, check=True).stdout)
        direct = json.loads(kube_direct.try_read(args, kubeconfig, None, 10))
        if args[0] == "version":
            # kubectl pads fields this minimal server leaves out; compare the version itself.
            same = kubectl["serverVersion"]["gitVersion"] == direct["serverVersion"]["gitVersion"]
        else:
            same = [i["metadata"]["name"] for i in kubectl["items"]] == [i["metadata"]["name"] for i in direct["items"]] and \
                all(i["kind"] == "Pod" for i in direct["items"])
        print(f"{'OK  ' if same else 'DIFF'} TLS+client-cert {' '.join(args)}")
        ok = ok and same
    leftovers = [n for n in os.listdir(os.environ.get("TEMP", "/tmp")) if n.startswith("kubesight-direct-")]
    print(f"client certificate presented on every request: {all(seen_clients)} ({len(seen_clients)} requests)")
    print(f"decrypted key files left in temp: {len(leftovers)}")
    server.shutdown()
    return 0 if ok and all(seen_clients) and not leftovers else 1


if __name__ == "__main__":
    sys.exit(main())
