import asyncio
import json

import pytest

pytest.importorskip("textual")

from textual.widgets import Input, Select, Static, Switch

from litellm_agent_rpc.config import load_config
from litellm_agent_rpc.tui import RouterApp


def capability():
    feature = {"state": "blocked", "live_verified": False, "reason": "pending"}
    return {
        "ready": False,
        "manifest": {
            "features": {
                name: feature
                for name in (
                    "text",
                    "client_tools",
                    "reasoning_output",
                    "usage",
                    "profile_isolation",
                )
            }
        },
    }


class Manager:
    def __init__(self, *, running=False):
        self.running = running
        self.calls = []
        self.pending = running

    def status(self):
        self.calls.append("status")
        result = {
            "state": "running" if self.running else "stopped",
            "owned": self.running,
            "active_requests": 0,
            "usage": None,
        }
        if self.running:
            result["v2"] = {
                "available": True,
                "counts": {
                    "native_running": 0,
                    "prepared": 0,
                    "awaiting_client": int(self.pending),
                    "cleaning": 0,
                    "generation_permits": int(self.pending),
                    "quarantined": 0,
                },
                "pending": [
                    {
                        "id": "chain_abc",
                        "model": "coding",
                        "state": "DELIVERED",
                        "expires_in_seconds": 25.5,
                    }
                ]
                if self.pending
                else [],
                "capabilities": {"coding": capability()},
                "usage": {
                    "available": True,
                    "degraded": False,
                    "persistence": "memory",
                    "totals": {
                        "complete": 0,
                        "partial": 1,
                        "unknown": 1,
                        "input_tokens": 9,
                        "output_tokens": None,
                        "cache_read_tokens": None,
                        "cache_write_tokens": None,
                        "reasoning_tokens": None,
                    },
                    "invocations": [
                        {
                            "model": "coding",
                            "backend": "codex",
                            "key_scope": "scope-a",
                            "usage_completeness": "partial",
                            "source": ["native-test"],
                            "input_tokens": 9,
                            "output_tokens": None,
                        }
                    ],
                },
            }
        return result

    def inspect_capabilities(self):
        self.calls.append("inspect_capabilities")
        return {"scope": "local_saved", "capabilities": {"coding": capability()}}

    def cancel_pending(self, pending_id):
        self.calls.append(("cancel", pending_id))
        self.pending = False
        return self.status()

    def stop(self):
        self.running = False
        return self.status()

    def close(self):
        self.calls.append("close")


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "keys": {"router": "sk-keep-key-1234567890123456789"},
                "enabled": {"codex": True},
                "extra": {"preserve": True},
            }
        ),
        encoding="utf-8",
    )
    return path


async def settled(app):
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not app.busy:
            return
    pytest.fail("UI operation did not finish")


async def test_stage_v2_route_preserves_key_and_inspects_only_when_requested(
    config_file,
):
    manager = Manager()
    app = RouterApp(config_file, manager=manager)
    async with app.run_test(size=(130, 45)):
        await settled(app)
        assert manager.calls == ["status"]
        app._inspect_capabilities()
        await settled(app)
        assert manager.calls == ["status", "inspect_capabilities"]
        assert "client_tools=blocked" in str(
            app.query_one("#v2-capabilities", Static).content
        )
        app.query_one("#route-alias", Input).value = "coding"
        app.query_one("#route-backend", Select).value = "codex"
        app.query_one("#route-model", Input).value = "model-a"
        app.query_one("#v2-enabled", Switch).value = True
        app.query_one("#route-v2", Switch).value = True
        app.query_one("#v2-profile-id", Input).value = "local"
        app.query_one("#v2-profile-revision", Input).value = "rev-1"
        app.query_one("#v2-transport", Input).value = "stdio"
        app.query_one("#v2-limits", Input).value = '{"max_active": 2}'
        app._stage_route()
        assert app.dirty
        app._save_config()
        await settled(app)
        saved = load_config(config_file)
        assert saved["keys"]["router"] == "sk-keep-key-1234567890123456789"
        assert saved["extra"] == {"preserve": True}
        assert saved["v2"]["auth_mode"] == "owner"
        assert saved["v2"]["routes"]["coding"] == {
            "profile_id": "local",
            "profile_revision": "rev-1",
            "transport": "stdio",
        }
        assert saved["v2"]["limits"] == {"max_active": 2}


async def test_v2_status_usage_and_confirmed_cancel(config_file):
    manager = Manager(running=True)
    app = RouterApp(config_file, manager=manager)
    async with app.run_test(size=(130, 45)) as pilot:
        await settled(app)
        assert "awaiting_client: 1" in str(app.query_one("#v2-status", Static).content)
        assert "client_tools=blocked" in str(
            app.query_one("#v2-capabilities", Static).content
        )
        usage = str(app.query_one("#usage-info", Static).content)
        assert (
            "partial: 1" in usage
            and "native-test" in usage
            and "output: unknown" in usage
        )
        app.selected_pending = "chain_abc"
        app._cancel_pending()
        await pilot.pause()
        await pilot.click("#confirm-yes")
        await settled(app)
        assert ("cancel", "chain_abc") in manager.calls
        assert "awaiting_client: 0" in str(app.query_one("#v2-status", Static).content)


async def test_virtual_key_route_rename_preserves_profile_and_auth_mode(config_file):
    data = json.loads(config_file.read_text(encoding="utf-8"))
    data["routes"] = {"coding": {"backend": "codex", "model": "model-a"}}
    data["v2"] = {
        "enabled": True,
        "auth_mode": "virtual_keys",
        "routes": {
            "coding": {
                "profile_id": "profile",
                "profile_revision": "rev-1",
                "transport": "stdio",
            }
        },
    }
    config_file.write_text(json.dumps(data), encoding="utf-8")
    app = RouterApp(config_file, manager=Manager())
    async with app.run_test(size=(130, 45)):
        await settled(app)
        app._select_route("coding")
        app.query_one("#route-alias", Input).value = "renamed"
        app._stage_route()
        assert app.config["v2"]["auth_mode"] == "virtual_keys"
        assert "coding" not in app.config["v2"]["routes"]
        assert app.config["v2"]["routes"]["renamed"]["profile_id"] == "profile"
        app._delete_route()
        # Deleting the only V2 route cannot silently fall back to owner auth.
        assert app.config["v2"]["auth_mode"] == "virtual_keys"
        assert "renamed" in app.config["v2"]["routes"]
