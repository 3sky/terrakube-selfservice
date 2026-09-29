import httpx

from app.openbao import OpenBaoClient


async def test_read_caches_login_relogs_on_403_and_maps_404(tmp_path):
    jwt_file = tmp_path / "token"
    jwt_file.write_text("sa-jwt")
    logins, reads = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/kubernetes/login":
            logins.append(request.content)
            return httpx.Response(200, json={"auth": {"client_token": f"t{len(logins)}", "lease_duration": 900}})
        reads.append((request.url.path, request.headers["X-Vault-Token"]))
        if request.url.path.endswith("/missing"):
            return httpx.Response(404)
        if request.headers["X-Vault-Token"] == "t1" and len(reads) == 3:
            return httpx.Response(403)  # token revoked mid-way
        return httpx.Response(200, json={"data": {"data": {"kubeconfig": "k"}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        bao = OpenBaoClient(http, "http://bao:8200/", "role-x", jwt_path=jwt_file)
        assert await bao.read("secret/data/labs/a") == {"kubeconfig": "k"}
        assert await bao.read("secret/data/labs/missing") is None
        assert await bao.read("secret/data/labs/b") == {"kubeconfig": "k"}

    assert len(logins) == 2 and b'"role":"role-x"' in logins[0].replace(b" ", b"")
    assert [token for _, token in reads] == ["t1", "t1", "t1", "t2"]
