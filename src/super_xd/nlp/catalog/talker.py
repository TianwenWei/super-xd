"""Fixed Talker schema surface used for structural policy validation."""
from copy import deepcopy
from super_xd.nlp.catalog.vehicle import TOOLS, HOT_TOOL_NAMES
from super_xd.nlp.catalog.control import CONTROL_TOOLS
from super_xd.nlp.catalog.cockpit import COCKPIT_TOOLS
from super_xd.nlp.catalog.maps import MAPS_TOOLS
from super_xd.nlp.catalog.manual import MANUAL_TOOLS
from super_xd.nlp.catalog.web import WEB_TOOLS


def talker_tools():
    return deepcopy([TOOLS[name].openai_tool() for name in HOT_TOOL_NAMES]
                    + CONTROL_TOOLS + MAPS_TOOLS + WEB_TOOLS + MANUAL_TOOLS + COCKPIT_TOOLS)
