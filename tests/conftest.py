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
granted = ["web.read", "drive.*", "notes.*", "code.run", "email.*", "github:me/notes.*"]

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
