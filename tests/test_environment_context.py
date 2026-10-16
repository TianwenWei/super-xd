from copy import deepcopy
from zoneinfo import ZoneInfoNotFoundError

import pytest

from backend.catalog import initial_vehicle_state
from backend.environment_context import environment_context
from backend.settings import environment_defaults
from backend.vehicle_simulator import VehicleSimulator


def test_default_location_is_user_configuration_not_a_fixed_city(monkeypatch):
    monkeypatch.setenv('DEFAULT_LOCATION', '  Tokyo  ')
    monkeypatch.setenv('DEFAULT_LOCATION_COUNTRY', 'Japan')
    monkeypatch.setenv('DEFAULT_TIMEZONE', 'Asia/Tokyo')
    state = initial_vehicle_state()
    assert state['_meta']['location'] == 'Tokyo'
    assert state['_meta']['location_country'] == 'Japan'
    assert state['_meta']['timezone'] == 'Asia/Tokyo'


@pytest.mark.parametrize('utc,local,day', [
    ('2026-10-15T22:30:00+00:00', '2026-10-16T00:30:00+02:00', '2026-10-16'),
    ('2026-10-25T00:30:00+00:00', '2026-10-25T02:30:00+02:00', '2026-10-25'),
    ('2026-10-25T01:30:00+00:00', '2026-10-25T02:30:00+01:00', '2026-10-25'),
])
def test_live_local_clock_handles_midnight_and_daylight_saving(utc, local, day):
    vehicle = {'_meta': {'location': 'Paris', 'location_country': 'France', 'timezone': 'Europe/Paris'}}
    before = deepcopy(vehicle)
    context = environment_context(vehicle, utc)
    assert context == {'current_location': {'name': 'Paris', 'country': 'France', 'source': 'user_setting'},
                       'current_utc': utc, 'current_local': local, 'current_date': day, 'timezone': 'Europe/Paris'}
    assert vehicle == before


def test_missing_location_stays_unknown(monkeypatch):
    for name in ('DEFAULT_LOCATION', 'DEFAULT_LOCATION_COUNTRY', 'DEFAULT_TIMEZONE'):
        monkeypatch.delenv(name, raising=False)
    assert environment_defaults() == {'location': '', 'location_country': '', 'timezone': 'UTC'}
    assert environment_context({'_meta': environment_defaults()}, '2026-10-15T12:00:00+00:00')['current_location'] is None


def test_clock_and_timezone_require_valid_explicit_data():
    with pytest.raises(ValueError):
        environment_context({'_meta': {'timezone': 'UTC'}}, '2026-10-15T12:00:00')
    with pytest.raises(ZoneInfoNotFoundError):
        environment_context({'_meta': {'timezone': 'Invalid/Zone'}}, '2026-10-15T12:00:00+00:00')


def test_location_configuration_is_atomic_and_does_not_set_navigation():
    simulator = VehicleSimulator(initial_vehicle_state())
    before = deepcopy(simulator.state)
    after = simulator.configure({'location': '  Lyon  ', 'location_country': 'France', 'timezone': 'Europe/Paris'})
    assert after['_meta']['location'] == 'Lyon'
    assert after['navigation'] == before['navigation']
    with pytest.raises(ValueError):
        simulator.configure({'location': 'Changed', 'location_country': ['invalid']})
    assert simulator.state == after
