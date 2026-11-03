import pytest

from decapod.config import parse_charter
from decapod.config.settings import Settings
from decapod.logs import EventBus
from decapod.storage import Database, EventLog

CHARTER_TEXT = """
[identity]
name = "Testpod"
mission = "Research and publish notes about test topics."

[values]
ranked = ["accuracy", "frugality"]

[scopes]
granted = ["web.read", "drive.*", "notes.*", "code.run", "email.*", "github:me/notes:*"]

[prohibitions]
rules = ["Never share secrets."]
patterns = ["(?i)api[_-]?key"]
"""


@pytest.fixture
def charter():
    return parse_charter(CHARTER_TEXT, "test-charter")


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "decapod.db")
    yield database
    database.close()


@pytest.fixture
def events(db):
    return EventLog(db, EventBus())


@pytest.fixture
def make_ctx(tmp_path, db, events, settings, charter):
    """Build a ToolContext; pass an httpx handler to fake the network."""
    import httpx

    from decapod.config import InstancePaths, Secrets
    from decapod.control import Budget
    from decapod.memory import AgendaStore, MemoryStore
    from decapod.tools import ToolContext

    clients = []

    def build(handler=None, secrets=None, cycle_id="c1"):
        paths = InstancePaths(tmp_path / "instance")
        paths.ensure()
        transport = httpx.MockTransport(handler or (lambda request: httpx.Response(404)))
        client = httpx.AsyncClient(transport=transport)
        clients.append(client)
        return ToolContext(
            paths=paths,
            settings=settings,
            charter=charter,
            secrets=Secrets(secrets or {}),
            db=db,
            events=events,
            agenda=AgendaStore(db, events),
            memory=MemoryStore(db, settings.memory, events),
            budget=Budget(db, settings.budget),
            http=client,
            cycle_id=cycle_id,
        )

    return build
