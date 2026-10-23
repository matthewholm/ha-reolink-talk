"""Proves the Lovelace resource URL carries a hash of the button's content.

Regression: the button is served with a month-long cache header from a fixed
URL, so after an update the companion app kept running the old button.
"""
import asyncio
import hashlib
import importlib.util
import pathlib
import sys
import types
import unittest


def _stub(name, **attrs):
    mod = sys.modules.setdefault(name, types.ModuleType(name))
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


_stub("homeassistant")
_stub("homeassistant.components")
_stub("homeassistant.components.http", StaticPathConfig=object)
_stub("homeassistant.config_entries", ConfigEntry=object)
_stub("homeassistant.core", HomeAssistant=object, callback=lambda f: f)

_pkg_dir = pathlib.Path(__file__).parent.parent / "custom_components/reolink_talk"
_pkg = types.ModuleType("reolink_talk_init_under_test")
_pkg.__path__ = [str(_pkg_dir)]
sys.modules["reolink_talk_init_under_test"] = _pkg
_stub("reolink_talk_init_under_test.talk_live_view", async_register_views=None)
_spec = importlib.util.spec_from_file_location(
    "reolink_talk_init_under_test.__init__", _pkg_dir / "__init__.py",
    submodule_search_locations=[str(_pkg_dir)],
)
init = importlib.util.module_from_spec(_spec)
init.__package__ = "reolink_talk_init_under_test"
_spec.loader.exec_module(init)

BUTTON = _pkg_dir / "frontend/reolink-talk-button.js"
EXPECTED = f"{init.FRONTEND_URL}?v={hashlib.sha256(BUTTON.read_bytes()).hexdigest()[:12]}"


class FakeResources:
    loaded = True

    def __init__(self, items):
        self.items = items
        self.created = []
        self.updated = []

    def async_items(self):
        return self.items

    async def async_create_item(self, data):
        self.created.append(data)

    async def async_update_item(self, item_id, updates):
        self.updated.append((item_id, updates))


class FakeHass:
    def __init__(self, resources):
        self.data = {"lovelace": types.SimpleNamespace(resources=resources)}
        self.config = types.SimpleNamespace(
            path=lambda rel: str(_pkg_dir.parent.parent / rel)
        )

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _run(items):
    resources = FakeResources(items)
    asyncio.run(init._async_register_lovelace_resource(FakeHass(resources)))
    return resources


class FrontendResourceTest(unittest.TestCase):
    def test_new_install_registers_hashed_url(self):
        r = _run([])
        self.assertEqual(r.created, [{"res_type": "module", "url": EXPECTED}])

    def test_unversioned_url_is_updated(self):
        r = _run([{"id": "abc", "url": init.FRONTEND_URL, "type": "module"}])
        self.assertEqual(r.updated, [("abc", {"url": EXPECTED})])
        self.assertEqual(r.created, [])

    def test_current_url_is_left_alone(self):
        r = _run([{"id": "abc", "url": EXPECTED, "type": "module"}])
        self.assertEqual((r.created, r.updated), ([], []))

    def test_other_resources_are_untouched(self):
        r = _run([{"id": "x", "url": "/hacsfiles/other.js", "type": "module"}])
        self.assertEqual(r.updated, [])
        self.assertEqual(r.created, [{"res_type": "module", "url": EXPECTED}])


if __name__ == "__main__":
    unittest.main()
