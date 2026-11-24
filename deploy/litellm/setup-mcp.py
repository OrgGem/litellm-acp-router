"""Provision a dedicated local proxy-admin key without printing credentials."""
import json
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent


def read_env(path):
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def main():
    env = read_env(ROOT / ".env")
    base = "http://127.0.0.1:" + env.get("LITELLM_PORT", "14000")

    def api(path, key, body=None):
        request = Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=60) as response:
            return json.load(response)

    target = ROOT / ".env.mcp"
    if target.exists():
        credential = read_env(target)["LITELLM_API_KEY"]
        print("Existing MCP credential retained.")
    else:
        # A failed/uncertain write must be inspected, not automatically retried.
        result = api("/user/new", env["LITELLM_MASTER_KEY"], {
            "user_id": "local-mcp-admin",
            "user_alias": "Local LiteAdmin MCP",
            "user_role": "proxy_admin",
            "auto_create_key": True,
            "send_invite_email": False,
            "key_alias": "litellm-admin-mcp",
            "models": [],
        })
        credential = result["key"]
        with target.open("x", encoding="utf-8") as output:
            output.write(f"LITELLM_BASE_URL={base}\nLITELLM_API_KEY={credential}\n")
        print("Dedicated MCP admin credential saved privately in .env.mcp.")
    info = api("/user/info?admin_mcp=true", credential)
    assert info["user_info"]["user_role"] == "proxy_admin", "MCP identity is not a proxy_admin"
    permissions = info.get("admin_mcp_permissions")
    assert isinstance(permissions, dict) and permissions.get("tools"), "Gateway lacks MCP permissions"
    print(f"MCP identity verified; {len(permissions['tools'])} permitted operations.")


if __name__ == "__main__":
    main()
