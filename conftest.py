"""Share the API test fixtures with the cross-cutting tests/ tree.

`pytest_plugins` is only honored from the root conftest, so the API test
fixtures (`client`, `settings`, `visitor_session`, `mint_credential`) are
registered here and remain defined once, in the API's own conftest. Local
conftests closer to a test tree always win over these base fixtures.
"""

pytest_plugins = ["services.api.tests.conftest"]
