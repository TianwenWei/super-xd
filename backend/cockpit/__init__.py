"""Media content and route queries are separate from device execution authority."""
from .media import CockpitError, LocalMediaCatalog, MediaCatalog, TencentMusicGateway, DeviceMediaCatalog, media_item
from .catalogs import MediaServices, HTTPMediaGateway


class CockpitServices:
    def __init__(self, media_catalog, routes=None):
        self.media = media_catalog
        self.routes = routes

    def info(self):
        return {'media': self.media.info(), 'routes': self.routes.info() if self.routes else {'configured': False, 'provider': None},
                'navigation_device': {'mode': 'simulated'}, 'media_device': {'mode': 'browser'}}

    async def route(self, origin, destination, waypoints):
        if self.routes is None or self.routes.info().get('configured') is False:
            raise CockpitError('route_not_configured', 'Route provider is not configured')
        return await self.routes.route(origin, destination, waypoints)

    async def aclose(self):
        await self.media.aclose()


def initial_cockpit_state():
    return {'media': {'version': 0, 'status': 'closed', 'desired_status': 'closed', 'content_id': None,
                      'kind': None, 'title': '', 'position_seconds': 0, 'duration_seconds': None, 'volume': 30,
                      'queue': [], 'provider': None},
            'navigation': {'version': 0, 'mode': 'simulated', 'active': False, 'destination': None,
                           'waypoints': [], 'route': None, 'origin': None}}
